"""Credential ownership tests for reusable-build attempt fences."""

from __future__ import annotations

import asyncio
import hashlib
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.services.runs.build_use import acquire_build_use, release_build_use
from kdive.worker_lifecycle.authority_store import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    register_worker_incarnation,
)
from tests.reconciler.conftest import connect

_PROTOCOL = CURRENT_WORKER_FENCE_PROTOCOL


def _credential(value: str) -> SecretStr:
    return SecretStr(value)


def _hash(credential: SecretStr) -> bytes:
    return hashlib.sha256(credential.get_secret_value().encode()).digest()


async def _register(
    witness: AsyncConnection, holder: str, credential: SecretStr, marker: str
) -> None:
    await register_worker_incarnation(
        witness,
        holder,
        "docker",
        {"container_id": marker * 64},
        _hash(credential),
        _PROTOCOL,
    )


async def _worker_connection(url: str) -> AsyncConnection:
    conn = await connect(url)
    await conn.execute(sql.SQL("SET SESSION AUTHORIZATION kdive_worker"))
    return conn


async def _seed_claim(conn: AsyncConnection, holder: str, *, attempt: int = 1) -> tuple[UUID, UUID]:
    investigation_id, generation, run_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
    digest = "d" * 64
    build_ref = f"{digest}.{generation}"
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'principal', 'project-a', 'title', 'active')",
        (investigation_id,),
    )
    await conn.execute(
        "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
        "content_digest, canonical_document, build_result, artifacts, target_kind, "
        "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, "
        "'{}'::jsonb, 'local-libvirt', '{}'::jsonb, now() + interval '1 day')",
        (investigation_id, generation, build_ref, digest),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, target_kind, state, build_profile, principal, "
        "project, build_ref) VALUES (%s, %s, 'local-libvirt', 'running', '{}'::jsonb, "
        "'principal', 'project-a', %s)",
        (run_id, investigation_id, build_ref),
    )
    await conn.execute(
        "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, payload, authorizing, dedup_key) VALUES "
        "(%s, 'install', 'running', %s, 3, %s, now() + interval '5 min', %s, %s, %s)",
        (
            job_id,
            attempt,
            holder,
            Jsonb({"run_id": str(run_id)}),
            Jsonb({"principal": "principal", "project": "project-a"}),
            f"build-use-{job_id}",
        ),
    )
    return run_id, job_id


async def _use_exists(conn: AsyncConnection, use_id: UUID) -> bool:
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM investigation_build_uses WHERE use_id = %s)",
            (use_id,),
        )
    ).fetchone()
    assert row is not None
    return bool(row[0])


def test_release_absent_build_use_is_a_noop() -> None:
    assert asyncio.run(
        release_build_use(
            cast(AsyncConnection, None),
            None,
            incarnation_credential=_credential("unused-credential"),
        )
    )


def test_cross_worker_release_cannot_delete_another_workers_use(migrated_url: str) -> None:
    async def _run() -> None:
        admin_conn = await connect(migrated_url)
        witness = await connect(migrated_url)
        worker_a_conn = await _worker_connection(migrated_url)
        worker_a_credential = _credential("worker-a-credential")
        worker_b_credential = _credential("worker-b-credential")
        try:
            await _register(witness, "docker:worker-a", worker_a_credential, "a")
            await _register(witness, "docker:worker-b", worker_b_credential, "b")
            run_b, job_b = await _seed_claim(admin_conn, "docker:worker-b")
            worker_b_use_id = await acquire_build_use(
                admin_conn,
                run_b,
                job_id=job_b,
                attempt=1,
                incarnation_credential=worker_b_credential,
            )
            assert worker_b_use_id is not None

            released = await release_build_use(
                worker_a_conn,
                worker_b_use_id,
                incarnation_credential=worker_a_credential,
            )

            assert released is False
            assert await _use_exists(admin_conn, worker_b_use_id)
        finally:
            await worker_a_conn.close()
            await witness.close()
            await admin_conn.close()

    asyncio.run(_run())


def test_release_refuses_same_worker_after_claim_attempt_replacement(migrated_url: str) -> None:
    async def _run() -> None:
        admin_conn = await connect(migrated_url)
        witness = await connect(migrated_url)
        credential = _credential("attempt-owner-credential")
        try:
            await _register(witness, "docker:attempt-owner", credential, "c")
            run_id, job_id = await _seed_claim(admin_conn, "docker:attempt-owner")
            use_id = await acquire_build_use(
                admin_conn,
                run_id,
                job_id=job_id,
                attempt=1,
                incarnation_credential=credential,
            )
            assert use_id is not None
            await admin_conn.execute("UPDATE jobs SET attempt = 2 WHERE id = %s", (job_id,))

            assert not await release_build_use(
                admin_conn, use_id, incarnation_credential=credential
            )
            assert await _use_exists(admin_conn, use_id)
        finally:
            await witness.close()
            await admin_conn.close()

    asyncio.run(_run())


def test_acquisition_refuses_a_replaced_claim(migrated_url: str) -> None:
    async def _run() -> None:
        admin_conn = await connect(migrated_url)
        witness = await connect(migrated_url)
        stale_credential = _credential("stale-worker-credential")
        replacement_credential = _credential("replacement-worker-credential")
        try:
            await _register(witness, "docker:stale", stale_credential, "d")
            await _register(witness, "docker:replacement", replacement_credential, "e")
            run_id, job_id = await _seed_claim(admin_conn, "docker:stale")
            await admin_conn.execute(
                "UPDATE jobs SET worker_id = 'docker:replacement', attempt = 2 WHERE id = %s",
                (job_id,),
            )

            with pytest.raises(RuntimeError, match="credential-owned executing claim"):
                await acquire_build_use(
                    admin_conn,
                    run_id,
                    job_id=job_id,
                    attempt=1,
                    incarnation_credential=stale_credential,
                )
            count = await (
                await admin_conn.execute("SELECT count(*) FROM investigation_build_uses")
            ).fetchone()
            assert count == (0,)
        finally:
            await witness.close()
            await admin_conn.close()

    asyncio.run(_run())
