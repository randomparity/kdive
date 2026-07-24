"""The two investigation-rootfs reclaim sweeps, now enqueue-only (ADR-0442, #1522).

``sweep_investigation_rootfs_reclaim`` (close-driven, keyed on ``rootfs_cleanup_pending_at``) and
``sweep_expired_investigation_rootfs_reclaim`` (TTL backstop) hand the reclaim worklist to a worker
job instead of doing it themselves: the reconciler may run as a different user than the worker that
created the root-owned staging tree, so a reconciler-side unlink fails after the object is already
gone. These cover that the sweeps touch neither the filesystem nor the object store, the disjoint
worklists, and the stable per-investigation dedup slot.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg

from kdive.domain.operations.jobs import JobKind
from kdive.reconciler.cleanup.gc import (
    sweep_expired_investigation_rootfs_reclaim,
    sweep_investigation_rootfs_reclaim,
)
from tests.reconciler.conftest import connect

_TOKEN = "dGVzdC10b2tlbg"  # an arbitrary base64url content-address token


def _object_key(inv: UUID, token: str = _TOKEN) -> str:
    return f"local/investigations/{inv}/rootfs-{token}"


async def _seed_investigation(
    conn: psycopg.AsyncConnection, *, state: str, rootfs_marker_age: timedelta | None
) -> UUID:
    inv_id = uuid4()
    if rootfs_marker_age is None:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state) "
            "VALUES (%s, 'p', 'proj', 't', %s)",
            (inv_id, state),
        )
    else:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state, "
            "rootfs_cleanup_pending_at) VALUES (%s, 'p', 'proj', 't', %s, now() - %s)",
            (inv_id, state, rootfs_marker_age),
        )
    return inv_id


async def _seed_rootfs_object(
    conn: psycopg.AsyncConnection,
    inv: UUID,
    *,
    created_age: timedelta | None = None,
    token: str = _TOKEN,
) -> UUID:
    artifact_id = uuid4()
    if created_age is None:
        await conn.execute(
            "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES (%s, 'investigations', %s, %s, 'e', 'redacted', 'rootfs')",
            (artifact_id, inv, _object_key(inv, token)),
        )
    else:
        await conn.execute(
            "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class, created_at) VALUES (%s, 'investigations', %s, %s, 'e', 'redacted', "
            "'rootfs', now() - %s)",
            (artifact_id, inv, _object_key(inv, token), created_age),
        )
    return artifact_id


async def _reclaim_jobs(conn: psycopg.AsyncConnection, inv: UUID) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT id, state, payload, dedup_key FROM jobs WHERE kind = %s "
        "AND payload->>'investigation_id' = %s",
        (JobKind.RECLAIM_INVESTIGATION_ROOTFS.value, str(inv)),
    )
    return [
        {"id": row[0], "state": row[1], "payload": row[2], "dedup_key": row[3]}
        for row in await cur.fetchall()
    ]


async def _marker(conn: psycopg.AsyncConnection, inv: UUID) -> object:
    cur = await conn.execute(
        "SELECT rootfs_cleanup_pending_at FROM investigations WHERE id = %s", (inv,)
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


def test_close_driven_enqueues_the_due_rows_past_grace(migrated_url: str) -> None:
    # The sweep's whole job is now to hand the worklist over: one job, carrying every committed
    # rootfs row of the investigation, on the stable per-investigation dedup key.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
            artifact_id = await _seed_rootfs_object(seed, inv)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 1
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1
            assert jobs[0]["payload"]["artifact_ids"] == [str(artifact_id)]
            assert jobs[0]["dedup_key"] == f"rootfs-reclaim:{inv}"
            assert await _marker(conn, inv) is not None  # the worker clears it, not the sweep
        finally:
            await conn.close()

    asyncio.run(_run())


def test_close_driven_skips_an_investigation_inside_grace(migrated_url: str) -> None:
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(hours=1)
            )
            await _seed_rootfs_object(seed, inv)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 0
            assert await _reclaim_jobs(conn, inv) == []
        finally:
            await conn.close()

    asyncio.run(_run())


def test_close_driven_clears_a_marker_with_nothing_to_reclaim(migrated_url: str) -> None:
    # A marker past grace with no rootfs rows has no work for a worker, so enqueuing one would
    # leave the investigation on the worklist forever. The sweep clears it directly (a DB write).
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 0
            assert await _reclaim_jobs(conn, inv) == []
            assert await _marker(conn, inv) is None
        finally:
            await conn.close()

    asyncio.run(_run())


def test_marker_independence_from_build_sweep(migrated_url: str) -> None:
    # AC-8d, preserved: cleanup_pending_at NULL (the build sweep drained it) but
    # rootfs_cleanup_pending_at set — the rootfs sweep still fires, keyed on its OWN marker.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
            await _seed_rootfs_object(seed, inv)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 1
        finally:
            await conn.close()

    asyncio.run(_run())


def test_repeat_passes_reuse_the_one_reclaim_slot(migrated_url: str) -> None:
    # ADR-0442 §6: a stable dedup key holds exactly one job row per investigation. A second pass
    # while the job is queued is a no-op; a pass after it reached a terminal state recycles the
    # SAME row back to queued with the fresh payload, rather than piling up a row every ~30 s.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
            first = await _seed_rootfs_object(seed, inv)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1))
            await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1))
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1  # in-flight dedup
            job_id = jobs[0]["id"]

            await conn.execute("UPDATE jobs SET state = 'succeeded' WHERE id = %s", (job_id,))
            second = await _seed_rootfs_object(conn, inv, token="c2Vjb25kLXRva2Vu")
            await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1))
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1
            assert jobs[0]["id"] == job_id  # the same slot, recycled
            assert jobs[0]["state"] == "queued"
            assert set(jobs[0]["payload"]["artifact_ids"]) == {str(first), str(second)}
        finally:
            await conn.close()

    asyncio.run(_run())


def test_ttl_backstop_enqueues_only_past_retention_rows(migrated_url: str) -> None:
    # AC-8b/AC-13: a never-closed (open) investigation's past-retention object is handed over; a
    # fresh one in the same investigation is not, so the TTL policy stays in the reconciler.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="open", rootfs_marker_age=None)
            expired = await _seed_rootfs_object(seed, inv, created_age=timedelta(days=40))
            await _seed_rootfs_object(seed, inv, token="ZnJlc2gtdG9rZW4")  # inside retention
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_expired_investigation_rootfs_reclaim(conn, timedelta(days=30)) == 1
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1
            assert jobs[0]["payload"]["artifact_ids"] == [str(expired)]
        finally:
            await conn.close()

    asyncio.run(_run())


def test_ttl_backstop_skips_a_closed_investigation(migrated_url: str) -> None:
    # The two worklists are disjoint by construction, so they never contend for the shared slot:
    # a closed investigation is the close-driven sweep's, and is in neither `open` nor `active`.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
            await _seed_rootfs_object(seed, inv, created_age=timedelta(days=40))
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_expired_investigation_rootfs_reclaim(conn, timedelta(days=30)) == 0
            assert await _reclaim_jobs(conn, inv) == []
        finally:
            await conn.close()

    asyncio.run(_run())
