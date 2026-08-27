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
import base64
import hashlib
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.artifacts.uploads.content_address import rootfs_object_token
from kdive.domain.capacity.state import ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES
from kdive.domain.operations.jobs import JobKind
from kdive.profiles.provisioning import ProvisioningProfile, dump_profile
from kdive.reconciler.cleanup import investigation_rootfs
from kdive.reconciler.cleanup.investigation_rootfs import (
    sweep_expired_investigation_rootfs_reclaim,
    sweep_investigation_rootfs_reclaim,
    sweep_unowned_investigation_rootfs_staging,
)
from kdive.reconciler.repairs.jobs import repair_abandoned_jobs
from tests.reconciler.conftest import connect
from tests.support.worker_fence import register_worker

_TOKEN = "dGVzdC10b2tlbg"  # an arbitrary base64url content-address token

#: The JSON path `_UNOWNED_STAGING_INV_SQL` reaches into `systems.provisioning_profile`, named once
#: so the test below can walk a real serialized profile with it and assert the SQL spells it too.
_STAGING_LANE_JSON_PATH = ("provider", "local-libvirt", "rootfs", "kind")


def _b64_sha256(seed: bytes) -> str:
    return base64.b64encode(hashlib.sha256(seed).digest()).decode("ascii")


def _object_key(inv: UUID, token: str = _TOKEN) -> str:
    return f"local/investigations/{inv}/rootfs-{token}"


async def _seed_investigation(
    conn: psycopg.AsyncConnection,
    *,
    state: str,
    rootfs_marker_age: timedelta | None,
    age: timedelta = timedelta(0),
) -> UUID:
    """Seed one investigation `age` old, optionally carrying the close-driven rootfs marker.

    `age` drives `investigations.created_at`, which ADR-0501 makes the staging-drain lane's age
    gate, so a test that wants that lane to select has to age the investigation and not only its
    System. It defaults to zero, which is what every lane keyed on something else wants.
    """
    inv_id = uuid4()
    if rootfs_marker_age is None:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state, created_at) "
            "VALUES (%s, 'p', 'proj', 't', %s, now() - %s)",
            (inv_id, state, age),
        )
    else:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state, created_at, "
            "rootfs_cleanup_pending_at) VALUES (%s, 'p', 'proj', 't', %s, now() - %s, now() - %s)",
            (inv_id, state, age, rootfs_marker_age),
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
        "SELECT id, state, payload, dedup_key, created_at, max_attempts FROM jobs "
        "WHERE kind = %s "
        "AND payload->>'investigation_id' = %s",
        (JobKind.RECLAIM_INVESTIGATION_ROOTFS.value, str(inv)),
    )
    return [
        {
            "id": row[0],
            "state": row[1],
            "payload": row[2],
            "dedup_key": row[3],
            "created_at": row[4],
            "max_attempts": row[5],
        }
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
            # The sweep is the retry loop, so an in-job retry of a permission wall buys nothing.
            assert jobs[0]["max_attempts"] == 1
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


def test_close_driven_enqueues_an_empty_worklist_rather_than_short_circuiting(
    migrated_url: str,
) -> None:
    # A marker past grace with no rootfs rows still gets a job, carrying an empty worklist. The
    # handler's drain tail sweeps the staging dir (a crash-orphaned SENSITIVE *.partial no row owns)
    # and clears the marker. Short-circuiting here would strand the orphan or put a filesystem write
    # back in the reconciler, and would split one drain rule into two.
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
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 1
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1
            assert jobs[0]["payload"]["artifact_ids"] == []
            assert await _marker(conn, inv) is not None  # the handler clears it, not the sweep
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


def test_repeat_passes_hold_one_reclaim_slot(migrated_url: str) -> None:
    # ADR-0442 §6: while the job is in flight the sweep is a no-op — one row, same job.
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
            first = await _reclaim_jobs(conn, inv)
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 0
            second = await _reclaim_jobs(conn, inv)
            assert len(second) == 1
            assert second[0]["id"] == first[0]["id"]
        finally:
            await conn.close()

    asyncio.run(_run())


def test_a_settled_job_holds_its_slot_through_the_backoff(migrated_url: str) -> None:
    # A settled reclaim is not re-issued for ROOTFS_RECLAIM_RETRY_BACKOFF, so a faulting reclaim
    # retries on the order of minutes instead of twice a minute — and its failed row stays
    # inspectable for that window rather than being replaced within one pass.
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
            await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1))
            job_id = (await _reclaim_jobs(conn, inv))[0]["id"]
            await conn.execute(
                "UPDATE jobs SET state = 'failed', error_category = 'infrastructure_failure' "
                "WHERE id = %s",
                (job_id,),
            )
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 0
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1
            assert jobs[0]["id"] == job_id
            assert jobs[0]["state"] == "failed"  # the failure record survives the pass
        finally:
            await conn.close()

    asyncio.run(_run())


def test_a_settled_job_past_the_backoff_is_reissued_with_a_fresh_created_at(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sweep drops the settled row and inserts a fresh one, so the reclaim is re-dated to the
    # pass that decided it is due and carries that pass's due set. (Terminal recycling re-dates
    # `created_at` too since ADR-0447; what keeps the delete-and-insert here is the backoff.)
    monkeypatch.setattr(investigation_rootfs, "ROOTFS_RECLAIM_RETRY_BACKOFF", timedelta(0))

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
            first_row = await _seed_rootfs_object(seed, inv)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1))
            before = (await _reclaim_jobs(conn, inv))[0]
            await conn.execute("UPDATE jobs SET state = 'failed' WHERE id = %s", (before["id"],))
            second_row = await _seed_rootfs_object(conn, inv, token="c2Vjb25kLXRva2Vu")

            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 1
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1  # still exactly one slot, not a row per pass
            after = jobs[0]
            assert after["id"] != before["id"]
            assert after["created_at"] > before["created_at"]  # re-dated, so it cannot preempt
            assert after["state"] == "queued"
            assert set(after["payload"]["artifact_ids"]) == {str(first_row), str(second_row)}
        finally:
            await conn.close()

    asyncio.run(_run())


def test_a_canceled_job_does_not_wedge_the_slot(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The slot is reconciler-owned: an operator cancel stops the current attempt but must not
    # silently disable reclaim for the investigation forever (ADR-0442 §6 — cancel is advisory).
    monkeypatch.setattr(investigation_rootfs, "ROOTFS_RECLAIM_RETRY_BACKOFF", timedelta(0))

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
            await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1))
            job_id = (await _reclaim_jobs(conn, inv))[0]["id"]
            await conn.execute("UPDATE jobs SET state = 'canceled' WHERE id = %s", (job_id,))

            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 1
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1
            assert jobs[0]["state"] == "queued"
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


def test_a_dead_worker_recovers_via_the_abandoned_jobs_repair(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A worker that dies mid-reclaim leaves the job `running` with a lapsed lease, and the sweep's
    # admission gate treats `running` as in flight — so recovery is not the sweep's own doing. It
    # runs through `repair_abandoned_jobs`, which dead-letters the zombie (attempt >= max_attempts,
    # which max_attempts=1 makes true on the first claim); only then does the sweep re-issue. This
    # reddens if that coupling is ever broken.
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
            await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1))
            job_id = (await _reclaim_jobs(conn, inv))[0]["id"]
            # A worker claimed it, then the process died: running, lease lapsed, attempt spent.
            await register_worker(conn, "dead-worker")
            await conn.execute(
                "UPDATE jobs SET state = 'running', worker_id = 'dead-worker', attempt = 1, "
                "lease_expires_at = now() - interval '1 hour' WHERE id = %s",
                (job_id,),
            )

            monkeypatch.setattr(investigation_rootfs, "ROOTFS_RECLAIM_RETRY_BACKOFF", timedelta(0))
            # Without the abandoned-jobs repair the slot stays wedged: `running` is in flight.
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 0

            assert await repair_abandoned_jobs(conn) == 1
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 1
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1
            assert jobs[0]["state"] == "queued"
            assert jobs[0]["id"] != job_id  # a fresh, re-dated row
        finally:
            await conn.close()

    asyncio.run(_run())


_UPLOAD_PROFILE = (
    '{"provider": {"local-libvirt": {"rootfs": {"kind": "upload", "checksum_sha256": "c"}}}}'
)
_CATALOG_PROFILE = '{"provider": {"local-libvirt": {"rootfs": {"kind": "catalog", "name": "r"}}}}'

#: Older than the 30-day `investigation_rootfs_retention` every staging-drain test below passes, so
#: ADR-0501's `investigations.created_at` gate is satisfied and each test's assertion has exactly
#: one cause — the predicate it is actually about — rather than passing on a young investigation.
_PAST_RETENTION = timedelta(days=40)


async def _seed_upload_system(
    conn: psycopg.AsyncConnection,
    inv: UUID,
    *,
    state: str = "torn_down",
    profile: str = _UPLOAD_PROFILE,
    age: timedelta = timedelta(days=40),
) -> UUID:
    resource_id, alloc_id, system_id = uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'p', 'c', 'available', 'qemu:///system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (alloc_id, resource_id),
    )
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, investigation_id, state, provisioning_profile, "
        "principal, project, created_at) VALUES (%s, %s, %s, %s, %s::jsonb, 'p', 'proj', "
        "now() - %s)",
        (system_id, alloc_id, inv, state, profile, age),
    )
    return system_id


def test_neither_row_keyed_lane_reaches_a_never_closed_investigation_with_no_rows(
    migrated_url: str,
) -> None:
    # #1559's residual (a), pinned as the gap itself. This is the state a leaked base sits in: the
    # investigation was never closed, so `rootfs_cleanup_pending_at` is NULL and the close-driven
    # lane cannot see it; its rootfs rows have all drained, so the TTL lane's pure `artifacts` join
    # selects nothing either. Both lanes report zero, forever, and the base is reclaimed by nothing.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="open", rootfs_marker_age=None)
            await _seed_upload_system(seed, inv)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 0
            assert await sweep_expired_investigation_rootfs_reclaim(conn, timedelta(days=30)) == 0
            assert await _reclaim_jobs(conn, inv) == []
        finally:
            await conn.close()

    asyncio.run(_run())


def test_staging_drain_lane_enqueues_an_empty_worklist_for_that_investigation(
    migrated_url: str,
) -> None:
    # ADR-0494 section 2: the lane that closes the gap above. Keyed on the `systems` row that
    # referenced an uploaded base -- the causal record, which outlives every artifacts row the base
    # had -- and carrying an empty worklist, so the handler falls straight through to the drain tail
    # that sweeps the staging directory.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="open", rootfs_marker_age=None, age=_PAST_RETENTION
            )
            await _seed_upload_system(seed, inv)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_unowned_investigation_rootfs_staging(conn, timedelta(days=30)) == 1
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1
            assert jobs[0]["payload"]["artifact_ids"] == []
            assert jobs[0]["dedup_key"] == f"rootfs-reclaim:{inv}"
        finally:
            await conn.close()

    asyncio.run(_run())


def test_staging_drain_lane_is_disjoint_from_the_ttl_lane(migrated_url: str) -> None:
    # The two share the per-investigation dedup slot, so a worklist overlap would have them fight
    # over it every pass -- one deleting the other's settled job and re-issuing a different payload.
    # `NOT EXISTS` is what keeps them apart: a surviving rootfs row is the TTL lane's business.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="open", rootfs_marker_age=None, age=_PAST_RETENTION
            )
            await _seed_upload_system(seed, inv)
            await _seed_rootfs_object(seed, inv, created_age=timedelta(days=40))
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_unowned_investigation_rootfs_staging(conn, timedelta(days=30)) == 0
            assert await sweep_expired_investigation_rootfs_reclaim(conn, timedelta(days=30)) == 1
        finally:
            await conn.close()

    asyncio.run(_run())


def test_staging_drain_lane_is_disjoint_from_the_close_driven_lane(migrated_url: str) -> None:
    # The other half of the disjointness: a closed investigation is the close-driven lane's, keyed
    # on the marker only `investigations.close` sets, and is in neither `open` nor `active`.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed,
                state="closed",
                rootfs_marker_age=timedelta(days=2),
                age=_PAST_RETENTION,
            )
            await _seed_upload_system(seed, inv)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_unowned_investigation_rootfs_staging(conn, timedelta(days=30)) == 0
            assert await sweep_investigation_rootfs_reclaim(conn, timedelta(days=1)) == 1
        finally:
            await conn.close()

    asyncio.run(_run())


def test_staging_drain_lane_ignores_an_investigation_that_never_used_an_uploaded_rootfs(
    migrated_url: str,
) -> None:
    # The worklist bound. A base is only ever staged for a System whose profile names an `upload`
    # rootfs, so a catalog-only investigation has no staging directory to drain and must not draw a
    # job every pass for the rest of its life.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="open", rootfs_marker_age=None, age=_PAST_RETENTION
            )
            await _seed_upload_system(seed, inv, profile=_CATALOG_PROFILE)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_unowned_investigation_rootfs_staging(conn, timedelta(days=30)) == 0
            assert await _reclaim_jobs(conn, inv) == []
        finally:
            await conn.close()

    asyncio.run(_run())


def test_staging_drain_lane_retries_a_long_staged_investigation_whose_system_is_young(
    migrated_url: str,
) -> None:
    # #1686, and the whole point of ADR-0501. Under content-addressed reuse (ADR-0441) a System
    # minutes old attaches to a checksum staged in this investigation months ago, so keying the age
    # gate on `systems.created_at` stranded the drained half for up to the full 30-day retention
    # window -- 120x the `ROOTFS_STAGING_DRAIN_BACKOFF` cadence the lane is designed around. The
    # investigation is what has aged past retention, and it is what governs the bytes, so it is the
    # gate. RED before ADR-0501: the 1-minute-old `systems` row failed `s.created_at < now() - 30d`
    # and the sweep returned 0.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="open", rootfs_marker_age=None, age=_PAST_RETENTION
            )
            await _seed_upload_system(seed, inv, state="ready", age=timedelta(minutes=1))
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_unowned_investigation_rootfs_staging(conn, timedelta(days=30)) == 1
            jobs = await _reclaim_jobs(conn, inv)
            assert len(jobs) == 1
            assert jobs[0]["payload"]["artifact_ids"] == []
        finally:
            await conn.close()

    asyncio.run(_run())


@pytest.mark.parametrize("system_state", sorted(investigation_rootfs._MID_MATERIALIZE_STATE_VALUES))
def test_staging_drain_lane_leaves_an_investigation_with_a_mid_materialize_system_alone(
    migrated_url: str, system_state: str
) -> None:
    # ADR-0501 section 2. What the discarded `systems.created_at` gate was really proxying for: a
    # System between its staging `mkdir` and its artifacts-row resolution, which the drain tail's
    # `rmdir` would fail out from under. Replacing an age proxy with an explicit state predicate is
    # only an improvement if the predicate actually bites, so the investigation here is well past
    # retention -- the age gate cannot be what excludes it. Drop the anti-join from
    # `_UNOWNED_STAGING_INV_SQL` and every parametrization reddens.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="open", rootfs_marker_age=None, age=_PAST_RETENTION
            )
            await _seed_upload_system(seed, inv, state=system_state, age=timedelta(minutes=1))
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_unowned_investigation_rootfs_staging(conn, timedelta(days=30)) == 0
            assert await _reclaim_jobs(conn, inv) == []
        finally:
            await conn.close()

    asyncio.run(_run())


def test_staging_drain_lane_excludes_a_whole_investigation_not_just_the_busy_system(
    migrated_url: str,
) -> None:
    # The anti-join is investigation-scoped rather than per-System row, and that is load-bearing:
    # the job it issues carries an empty worklist, so the drain tail sweeps the ONE
    # per-investigation staging directory every one of these Systems shares. A per-row exclusion
    # would let a settled sibling re-admit the job and `rmdir` under the provisioning one -- so a
    # settled System alongside a mid-materialize one must still yield nothing.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="open", rootfs_marker_age=None, age=_PAST_RETENTION
            )
            await _seed_upload_system(seed, inv, state="torn_down", age=_PAST_RETENTION)
            await _seed_upload_system(seed, inv, state="provisioning", age=timedelta(minutes=1))
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_unowned_investigation_rootfs_staging(conn, timedelta(days=30)) == 0
            assert await _reclaim_jobs(conn, inv) == []
        finally:
            await conn.close()

    asyncio.run(_run())


def test_staging_drain_lane_leaves_a_young_investigation_alone(migrated_url: str) -> None:
    # The retention half survives ADR-0501, just re-keyed: an investigation created minutes ago has
    # not accumulated anything the retention policy governs, and its Systems may be staging right
    # now. Pinned in its own right because every other staging-drain test now ages its
    # investigation, which would otherwise leave the new gate asserted in one direction only.
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="open", rootfs_marker_age=None, age=timedelta(minutes=1)
            )
            await _seed_upload_system(seed, inv, state="torn_down", age=timedelta(minutes=1))
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_unowned_investigation_rootfs_staging(conn, timedelta(days=30)) == 0
            assert await _reclaim_jobs(conn, inv) == []
        finally:
            await conn.close()

    asyncio.run(_run())


def test_the_lanes_mid_materialize_states_are_the_curated_pre_overlay_set() -> None:
    # The anti-join must not drift from the reclaim's own pin gate on what "needs the base with no
    # overlay yet" means, so it reads ADR-0441 section 6's curated set rather than restating it.
    # That set is guarded by `test_reclaim_classification_is_exhaustive`, so a new non-terminal
    # SystemState added without being classified reddens there -- and this lane inherits that guard
    # instead of silently keeping a two-element list someone wrote by hand.
    curated = tuple(sorted(state.value for state in ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES))
    assert curated == investigation_rootfs._MID_MATERIALIZE_STATE_VALUES
    assert set(curated) == {"provisioning", "reprovisioning", "restoring"}


def test_staging_drain_lane_issues_one_job_for_an_investigation_with_several_systems(
    migrated_url: str,
) -> None:
    # The worklist is per-System while the dedup slot is per-investigation, so N sibling Systems
    # must still yield one job and one counted enqueue. (`DISTINCT` in the query is an efficiency
    # measure on top of this, not the guarantee: the in-flight dedup in `_enqueue_rootfs_reclaim`
    # would collapse the duplicates anyway, which is what this pins.)
    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(
                seed, state="open", rootfs_marker_age=None, age=_PAST_RETENTION
            )
            await _seed_upload_system(seed, inv)
            await _seed_upload_system(seed, inv)
        finally:
            await seed.close()
        conn = await connect(migrated_url)
        try:
            assert await sweep_unowned_investigation_rootfs_staging(conn, timedelta(days=30)) == 1
            assert len(await _reclaim_jobs(conn, inv)) == 1
        finally:
            await conn.close()

    asyncio.run(_run())


def test_the_lanes_json_path_matches_what_a_real_profile_actually_serializes_to() -> None:
    # `_UNOWNED_STAGING_INV_SQL` re-derives, in SQL, the path `_referenced_token` walks in Python:
    # `#>> '{provider,local-libvirt,rootfs,kind}'`. Nothing else ties either to the model, and the
    # guard's failure mode is emitting nothing -- so a renamed serialization alias would silently
    # select zero rows with every test still green. That is the duplicated-derivation trap
    # `runtime_paths.py` names (#1383/ADR-0412's ELF-magic check became dead exactly this way).
    profile = ProvisioningProfile.model_validate(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org#v6.9",
            "provider": {
                "local-libvirt": {
                    "rootfs": {"kind": "upload", "checksum_sha256": _b64_sha256(b"rootfs")}
                }
            },
        }
    )
    serialized: Any = dump_profile(profile)

    node: Any = serialized
    for key in _STAGING_LANE_JSON_PATH:
        assert isinstance(node, dict), f"the lane's JSON path breaks at {key!r}: {serialized}"
        assert key in node, f"the lane's JSON path breaks at {key!r}: {serialized}"
        node = node[key]
    assert node == "upload"
    assert (
        f"'{{{','.join(_STAGING_LANE_JSON_PATH)}}}'"
        in investigation_rootfs._UNOWNED_STAGING_INV_SQL
    )


def test_a_content_address_token_never_contains_a_dot() -> None:
    # The staging sweep tests each candidate by `Path.stem`, so a token containing a `.` would make
    # the stem drop a segment and leave EVERY base and marker unprotected -- a mass unlink of live
    # bases, not a benign miss. The property holds because `rootfs_object_token` emits unpadded
    # base64url, whose alphabet is [A-Za-z0-9_-]; it is asserted rather than assumed.
    for seed in (b"", b"rootfs", b"\x00" * 64, bytes(range(256)), b"\xff" * 32):
        token = rootfs_object_token(_b64_sha256(seed))
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token), token
        assert Path(f"{token}.qcow2").stem == token
        assert Path(f"{token}.ready").stem == token
