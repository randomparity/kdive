"""Durable worker-incarnation registration and termination fences."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from kdive.services.runs.worker_incarnations import (
    IncarnationConflict,
    register_worker_incarnation,
    terminate_worker_incarnation,
    verify_active_worker_incarnation,
)
from tests.reconciler.conftest import connect


def test_registration_and_termination_are_immutable_and_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        binding = {"pod_namespace": "kdive", "pod_name": "worker-0", "pod_uid": "uid-1"}
        try:
            first = await register_worker_incarnation(
                conn, "kubernetes:kdive:worker-0:uid-1", "kubernetes", binding
            )
            replay = await register_worker_incarnation(
                conn, "kubernetes:kdive:worker-0:uid-1", "kubernetes", binding
            )
            assert first == replay
            terminated = await terminate_worker_incarnation(
                conn, "kubernetes:kdive:worker-0:uid-1", "failed"
            )
            assert terminated.state == "terminated"
            assert terminated.terminated_at is not None
            assert (
                await terminate_worker_incarnation(
                    conn, "kubernetes:kdive:worker-0:uid-1", "failed"
                )
                == terminated
            )
            with pytest.raises(IncarnationConflict):
                await register_worker_incarnation(
                    conn, "kubernetes:kdive:worker-0:uid-1", "kubernetes", binding
                )
            with pytest.raises(IncarnationConflict):
                await terminate_worker_incarnation(
                    conn, "kubernetes:kdive:worker-0:uid-1", "succeeded"
                )
        finally:
            await conn.close()

    asyncio.run(_run())


def test_registration_rejects_conflicting_binding(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        try:
            await register_worker_incarnation(
                conn, "docker:nonce", "docker", {"container_id": "a" * 64}
            )
            with pytest.raises(IncarnationConflict):
                await register_worker_incarnation(
                    conn, "docker:nonce", "docker", {"container_id": "b" * 64}
                )
        finally:
            await conn.close()

    asyncio.run(_run())


def test_docker_worker_requires_gate_preregistered_active_binding(migrated_url: str) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        try:
            with pytest.raises(IncarnationConflict, match="pre-registered"):
                await verify_active_worker_incarnation(conn, "docker:nonce-missing", "docker")
            await register_worker_incarnation(
                conn, "docker:nonce-ready", "docker", {"container_id": "a" * 64}
            )
            row = await verify_active_worker_incarnation(conn, "docker:nonce-ready", "docker")
            assert row.authority_binding == {"container_id": "a" * 64}
            await terminate_worker_incarnation(conn, "docker:nonce-ready", "killed")
            with pytest.raises(IncarnationConflict, match="not active"):
                await verify_active_worker_incarnation(conn, "docker:nonce-ready", "docker")
        finally:
            await conn.close()

    asyncio.run(_run())


def test_post_termination_build_use_is_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        from kdive.services.runs.build_use import acquire_build_use

        investigation_id, generation, run_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
        holder = "docker:nonce"
        conn = await connect(migrated_url)
        try:
            await conn.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            await conn.execute(
                "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                "content_digest, canonical_document, build_result, artifacts, target_kind, "
                "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, "
                "'{}'::jsonb, 'local-libvirt', '{}'::jsonb, now() + interval '1 day')",
                (investigation_id, generation, f"{'d' * 64}.{generation}", "d" * 64),
            )
            await conn.execute(
                "INSERT INTO runs (id, investigation_id, target_kind, state, build_profile, "
                "principal, project, build_ref) VALUES (%s, %s, 'local-libvirt', 'created', "
                "'{}'::jsonb, 'p', 'proj', %s)",
                (run_id, investigation_id, f"{'d' * 64}.{generation}"),
            )
            await conn.execute(
                "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
                "lease_expires_at, authorizing, dedup_key) VALUES "
                "(%s, 'install', 'running', 1, 3, %s, now() + interval '5 min', "
                "'{}'::jsonb, %s)",
                (job_id, holder, f"use-{job_id}"),
            )
            await register_worker_incarnation(conn, holder, "docker", {"container_id": "a" * 64})
            await terminate_worker_incarnation(conn, holder, "killed")
            with pytest.raises(RuntimeError, match="terminated"):
                await acquire_build_use(conn, run_id, job_id=job_id, attempt=1)
        finally:
            await conn.close()

    asyncio.run(_run())


def test_recovery_copies_exact_durable_termination_evidence(migrated_url: str) -> None:
    async def _run() -> None:
        from kdive.services.runs.build_use import recover_build_use_after_confirmed_worker_death

        investigation_id, generation, job_id, use_id = uuid4(), uuid4(), uuid4(), uuid4()
        holder = "docker:nonce-copy"
        binding = {"container_id": "c" * 64}
        conn = await connect(migrated_url)
        try:
            await conn.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            await conn.execute(
                "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                "content_digest, canonical_document, build_result, artifacts, target_kind, "
                "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, "
                "'{}'::jsonb, 'local-libvirt', '{}'::jsonb, now() + interval '1 day')",
                (investigation_id, generation, f"{'e' * 64}.{generation}", "e" * 64),
            )
            await conn.execute(
                "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
                "lease_expires_at, authorizing, dedup_key) VALUES "
                "(%s, 'install', 'running', 1, 3, %s, now() + interval '5 min', "
                "'{}'::jsonb, %s)",
                (job_id, holder, f"recover-{job_id}"),
            )
            await conn.execute(
                "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, "
                "job_id, attempt, holder_worker_id, lease_expires_at) VALUES "
                "(%s, %s, %s, %s, 1, %s, now() + interval '5 min')",
                (use_id, investigation_id, generation, job_id, holder),
            )
            await register_worker_incarnation(conn, holder, "docker", binding)
            termination = await terminate_worker_incarnation(conn, holder, "killed")
            assert await recover_build_use_after_confirmed_worker_death(
                conn,
                use_id,
                confirmed_worker_id=holder,
                recovered_by="operator:test",
                evidence="untrusted caller text",
                reason="exact worker terminated",
            )
            row = await (
                await conn.execute(
                    "SELECT authority_kind, authority_binding, termination_outcome, terminated_at "
                    "FROM investigation_build_use_recoveries WHERE use_id = %s",
                    (use_id,),
                )
            ).fetchone()
            assert row == ("docker", binding, "killed", termination.terminated_at)
        finally:
            await conn.close()

    asyncio.run(_run())
