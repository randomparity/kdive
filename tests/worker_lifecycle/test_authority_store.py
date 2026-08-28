"""Credential-bound worker-incarnation authority services."""

from __future__ import annotations

import asyncio
import hashlib
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, errors, sql
from pydantic import SecretStr

import kdive.worker_lifecycle.authority_store as incarnations
from kdive.worker_lifecycle.authority_store import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    DockerAuthorityBinding,
    IncarnationConflict,
    KubernetesAuthorityBinding,
    register_worker_incarnation,
    terminate_worker_incarnation,
)
from tests.reconciler.conftest import connect

_PROTOCOL = CURRENT_WORKER_FENCE_PROTOCOL


def _credential(value: str) -> SecretStr:
    return SecretStr(value)


def _credential_hash(credential: SecretStr) -> bytes:
    return hashlib.sha256(credential.get_secret_value().encode()).digest()


async def _role_connection(url: str, role: str) -> AsyncConnection:
    conn = await connect(url)
    await conn.execute(sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(role)))
    return conn


def test_authority_registration_identical_replay_returns_public_facts(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        witness = await _role_connection(migrated_url, "kdive_lifecycle_witness")
        binding = KubernetesAuthorityBinding(namespace="kdive", name="worker-0", uid="uid-1")
        credential_hash = _credential_hash(_credential("credential-one"))
        try:
            first = await register_worker_incarnation(
                witness,
                "kubernetes:kdive:worker-0:uid-1",
                "kubernetes",
                binding,
                credential_hash,
                _PROTOCOL,
            )
            replay = await register_worker_incarnation(
                witness,
                "kubernetes:kdive:worker-0:uid-1",
                "kubernetes",
                binding,
                credential_hash,
                _PROTOCOL,
            )
            assert first == replay
            assert first.incarnation == "kubernetes:kdive:worker-0:uid-1"
            assert first.authority_binding == binding
            assert first.fence_protocol == _PROTOCOL
        finally:
            await witness.close()

    asyncio.run(_run())


@pytest.mark.parametrize("conflict", ["binding", "credential_hash", "fence_protocol"])
def test_authority_registration_rejects_conflicting_immutable_fact(
    migrated_url: str, conflict: str
) -> None:
    async def _run() -> None:
        witness = await _role_connection(migrated_url, "kdive_lifecycle_witness")
        binding = DockerAuthorityBinding(container_id="a" * 64)
        credential_hash = _credential_hash(_credential("credential-two"))
        try:
            await register_worker_incarnation(
                witness,
                "docker:immutable",
                "docker",
                binding,
                credential_hash,
                _PROTOCOL,
            )
            replay_binding = (
                DockerAuthorityBinding(container_id="b" * 64) if conflict == "binding" else binding
            )
            replay_hash = (
                _credential_hash(_credential("different-credential"))
                if conflict == "credential_hash"
                else credential_hash
            )
            replay_protocol = _PROTOCOL + 1 if conflict == "fence_protocol" else _PROTOCOL
            with pytest.raises(IncarnationConflict):
                await register_worker_incarnation(
                    witness,
                    "docker:immutable",
                    "docker",
                    replay_binding,
                    replay_hash,
                    replay_protocol,
                )
        finally:
            await witness.close()

    asyncio.run(_run())


def test_worker_role_cannot_register_an_incarnation(migrated_url: str) -> None:
    async def _run() -> None:
        worker = await _role_connection(migrated_url, "kdive_worker")
        try:
            with pytest.raises(errors.InsufficientPrivilege):
                await register_worker_incarnation(
                    worker,
                    "docker:worker-forged",
                    "docker",
                    {"container_id": "f" * 64},
                    _credential_hash(_credential("forged-credential")),
                    _PROTOCOL,
                )
        finally:
            await worker.close()

    asyncio.run(_run())


def test_authentication_derives_holder_and_rejects_wrong_credential(migrated_url: str) -> None:
    async def _run() -> None:
        witness = await _role_connection(migrated_url, "kdive_lifecycle_witness")
        worker = await _role_connection(migrated_url, "kdive_worker")
        credential = _credential("authority-delivered-credential")
        binding = DockerAuthorityBinding(container_id="c" * 64)
        try:
            await register_worker_incarnation(
                witness,
                "docker:authenticated",
                "docker",
                binding,
                _credential_hash(credential),
                _PROTOCOL,
            )
            authenticated = await incarnations.authenticate_worker_incarnation(worker, credential)
            assert authenticated.incarnation == "docker:authenticated"
            assert authenticated.authority_binding == binding
            with pytest.raises(incarnations.IncarnationAuthenticationError):
                await incarnations.authenticate_worker_incarnation(
                    worker, _credential("wrong-credential")
                )
        finally:
            await worker.close()
            await witness.close()

    asyncio.run(_run())


def test_terminated_incarnation_cannot_authenticate(migrated_url: str) -> None:
    async def _run() -> None:
        witness = await _role_connection(migrated_url, "kdive_lifecycle_witness")
        worker = await _role_connection(migrated_url, "kdive_worker")
        credential = _credential("terminated-credential")
        try:
            await register_worker_incarnation(
                witness,
                "docker:terminated",
                "docker",
                {"container_id": "d" * 64},
                _credential_hash(credential),
                _PROTOCOL,
            )
            assert await terminate_worker_incarnation(
                witness,
                "docker:terminated",
                "docker",
                {"container_id": "d" * 64},
                "killed",
            )
            with pytest.raises(incarnations.IncarnationAuthenticationError):
                await incarnations.authenticate_worker_incarnation(worker, credential)
        finally:
            await worker.close()
            await witness.close()

    asyncio.run(_run())


def test_recovery_copies_exact_durable_termination_evidence(migrated_url: str) -> None:
    async def _run() -> None:
        from kdive.services.runs.build_use import recover_build_use_after_confirmed_worker_death

        investigation_id, generation, job_id, use_id = uuid4(), uuid4(), uuid4(), uuid4()
        holder = "docker:nonce-copy"
        binding = DockerAuthorityBinding(container_id="c" * 64)
        credential_hash = _credential_hash(_credential("recovery-credential"))
        conn = await connect(migrated_url)
        witness = await _role_connection(migrated_url, "kdive_lifecycle_witness")
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
            await register_worker_incarnation(
                witness, holder, "docker", binding, credential_hash, _PROTOCOL
            )
            await conn.execute(
                "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, "
                "job_id, attempt, holder_worker_id, lease_expires_at) VALUES "
                "(%s, %s, %s, %s, 1, %s, now() + interval '5 min')",
                (use_id, investigation_id, generation, job_id, holder),
            )
            assert await terminate_worker_incarnation(witness, holder, "docker", binding, "killed")
            assert await recover_build_use_after_confirmed_worker_death(
                conn,
                use_id,
                authorized_projects=("proj",),
                confirmed_worker_id=holder,
                recovered_by="operator:test",
                evidence="untrusted caller text",
                reason="exact worker terminated",
            )
            row = await (
                await conn.execute(
                    "SELECT authority_kind, authority_binding, termination_outcome, "
                    "terminated_at IS NOT NULL FROM investigation_build_use_recoveries "
                    "WHERE use_id = %s",
                    (use_id,),
                )
            ).fetchone()
            assert row == ("docker", binding, "killed", True)
        finally:
            await witness.close()
            await conn.close()

    asyncio.run(_run())
