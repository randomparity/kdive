"""Tests for the reconciler upload reaper (ADR-0048 §6, ADR-0104 §7, ADR-0444, ADR-0453, #11).

The reaper commits the manifest-row delete of a past-deadline window under the owner's advisory
lock, then sweeps the window's uncommitted objects holding neither lock nor connection — row
first, so a failing sweep can never restore a window over deleted bytes (ADR-0453,
issue #1552). For ``runs`` it sweeps whether the Run is pre-finalize (a true abandon) or
finalized with leftover chunks (ADR-0104 §7); for ``investigations`` it sweeps on the deadline
alone, in every investigation state (ADR-0444, superseding ADR-0441 §6's terminal-state gate —
``complete_rootfs_upload`` now rejects a past-deadline finalize, so the reap races nothing
legitimate). It exempts any object with a committed ``artifacts`` row, and the per-owner locked
re-read declines a manifest whose deadline was renewed since the candidate select.

The ``_repair_abandoned_uploads`` tests run the repair through a real non-autocommit
``AsyncConnectionPool`` via ``run_repair`` (mirroring ``test_loop.py``), so the
candidate-select transaction-nesting hazard is exercised; seeding and assertions use
separate autocommit ``connect`` connections.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.uploads import ManifestEntry
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.uploads import ReapOutcome
from kdive.reconciler.cleanup.uploads import (
    lock_scope_for as _lock_scope_for,
)
from kdive.reconciler.cleanup.uploads import (
    reap_one_owner as _reap_one_owner,
)
from kdive.reconciler.cleanup.uploads import (
    repair_abandoned_uploads as _repair_abandoned_uploads,
)
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_system


class _FakeStore:
    def __init__(self, objects: dict[str, list[str]]) -> None:
        self._objects = objects  # prefix -> [keys]
        self.deleted: list[str] = []

    def list_prefix(self, prefix: str) -> list[str]:
        return list(self._objects.get(prefix, []))

    def delete(self, key: str) -> None:
        self.deleted.append(key)


class _FailingStore(_FakeStore):
    """A store whose ``delete`` raises for the named keys, as a real store outage would.

    ``ObjectStore.delete`` wraps every ``BotoCoreError``/``ClientError`` in a
    ``CategorizedError``, so that is the exception a mid-sweep failure actually presents.
    """

    def __init__(
        self,
        objects: dict[str, list[str]],
        *,
        fail_keys: set[str] | None = None,
        fail_list_prefixes: set[str] | None = None,
    ) -> None:
        super().__init__(objects)
        self._fail_keys = fail_keys or set()
        self._fail_list_prefixes = fail_list_prefixes or set()
        self.attempted: list[str] = []
        self.listed: list[str] = []

    def list_prefix(self, prefix: str) -> list[str]:
        self.listed.append(prefix)
        if prefix in self._fail_list_prefixes:
            raise CategorizedError(
                f"list_objects_v2 failed for {prefix}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        return super().list_prefix(prefix)

    def delete(self, key: str) -> None:
        self.attempted.append(key)
        if key in self._fail_keys:
            raise CategorizedError(
                f"delete_object failed for {key}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        super().delete(key)


class _RaceStore(_FakeStore):
    """A store that runs a callback once, just before its first delete.

    The hook lands in the gap phase 2 leaves open: after the manifest-row delete has committed and
    the owner lock has been released, before the object is actually gone.
    """

    def __init__(self, objects: dict[str, list[str]], *, before_delete: Callable[[], None]) -> None:
        super().__init__(objects)
        self._before_delete: Callable[[], None] | None = before_delete

    def delete(self, key: str) -> None:
        if self._before_delete is not None:
            hook, self._before_delete = self._before_delete, None
            hook()
        super().delete(key)


class _ManifestProbingStore(_FakeStore):
    """A store that records, at the instant of each delete, whether the manifest row is gone.

    The end-state assertions cannot distinguish "row deleted before the objects" from "row
    deleted after them" — both leave no row. This probes the ordering from inside the delete
    callback, on its own connection, so it observes only what has actually committed.
    """

    def __init__(
        self, objects: dict[str, list[str]], *, url: str, owner_kind: str, owner_id: UUID
    ) -> None:
        super().__init__(objects)
        self._url = url
        self._owner_kind = owner_kind
        self._owner_id = owner_id
        self.manifest_absent_at_delete: list[bool] = []

    def delete(self, key: str) -> None:
        self.manifest_absent_at_delete.append(self._manifest_is_gone())
        super().delete(key)

    def _manifest_is_gone(self) -> bool:
        with psycopg.connect(self._url, autocommit=True) as probe:
            row = probe.execute(
                "SELECT 1 FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
                (self._owner_kind, self._owner_id),
            ).fetchone()
        return row is None


async def _insert_artifact_row(
    conn: psycopg.AsyncConnection, *, owner_kind: str, owner_id: UUID, object_key: str
) -> None:
    """Insert a minimal committed ``artifacts`` row (id/timestamps defaulted)."""
    await conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "    retention_class) VALUES (%s, %s, %s, %s, %s, %s)",
        (owner_kind, owner_id, object_key, "etag-1", "sensitive", "default"),
    )


async def _seed_investigation(conn: psycopg.AsyncConnection, *, state: str) -> UUID:
    inv_id = uuid4()
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'user-1', 'proj', 't', %s)",
        (inv_id, state),
    )
    return inv_id


def _reap(store: _FakeStore):
    return lambda conn: _repair_abandoned_uploads(conn, store)


def _run_manifest(
    run_id: UUID, ttl: timedelta
) -> tuple[str, upload_manifest.UploadManifestReplaceRequest]:
    prefix = f"local/runs/{run_id}/"
    request = upload_manifest.UploadManifestReplaceRequest(
        owner_kind="runs",
        owner_id=run_id,
        prefix=prefix,
        entries=[ManifestEntry("kernel", "a", 1)],
        ttl=ttl,
    )
    return prefix, request


def _investigation_manifest(
    inv_id: UUID, ttl: timedelta
) -> tuple[str, upload_manifest.UploadManifestReplaceRequest]:
    prefix = f"local/investigations/{inv_id}/"
    request = upload_manifest.UploadManifestReplaceRequest(
        owner_kind="investigations",
        owner_id=inv_id,
        prefix=prefix,
        entries=[ManifestEntry("rootfs", "a", 1)],
        ttl=ttl,
    )
    return prefix, request


def test_reaps_uncommitted_objects_past_deadline_for_created_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}kernel", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}kernel", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reaps_multiple_abandoned_owners_counted(migrated_url: str) -> None:
    # Two independent past-deadline Run manifests must each increment the reaped tally: the
    # return is the number of owners reaped, not a fixed 1.
    async def _run() -> None:
        prefixes: list[str] = []
        run_ids: list[UUID] = []
        async with await connect(migrated_url) as seed:
            for _ in range(2):
                system_id = await seed_system(seed)
                run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
                prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
                await upload_manifest.replace_manifest(seed, request)
                prefixes.append(prefix)
                run_ids.append(run_id)
        store = _FakeStore({p: [f"{p}kernel"] for p in prefixes})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 2  # both owners reaped, not a fixed 1
        assert sorted(store.deleted) == sorted(f"{p}kernel" for p in prefixes)
        async with await connect(migrated_url) as check:
            for run_id in run_ids:
                assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reaps_uncommitted_objects_past_deadline_for_closed_investigation(
    migrated_url: str,
) -> None:
    # AC-10: a stale investigation upload window on a CLOSED investigation past deadline has its
    # uncommitted object + manifest reaped (the finalize will never land).
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="closed")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}rootfs", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_exempts_committed_object(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="closed")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await _insert_artifact_row(
                seed, owner_kind="investigations", owner_id=inv_id, object_key=f"{prefix}rootfs"
            )
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == []  # committed object exempt
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_skips_owner_not_past_deadline(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(hours=1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}kernel"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is not None

    asyncio.run(_run())


def test_succeeded_run_with_lingering_manifest_reaps_chunks_not_final(migrated_url: str) -> None:
    """A SUCCEEDED Run whose post-commit chunk cleanup failed: its leftover chunks (no row) are
    reaped but the reassembled final object (committed row) is exempt, then the manifest goes
    (ADR-0104 §7, runs-branch generalization)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.SUCCEEDED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await _insert_artifact_row(
                seed, owner_kind="runs", owner_id=run_id, object_key=f"{prefix}kernel"
            )
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}kernel", f"{prefix}kernel.part0001"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == [f"{prefix}kernel.part0001"]  # final object exempt (has a row)
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reaps_past_deadline_uncommitted_manifest_on_open_investigation(migrated_url: str) -> None:
    """AC-3 (ADR-0444 decision 2, superseding ADR-0441 §6): an OPEN investigation is no longer
    excluded. Its past-deadline uncommitted object + manifest are reaped on the deadline alone —
    a finalize arriving this late is rejected by `complete_rootfs_upload`, so nothing legitimate
    is raced."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="open")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}rootfs", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_open_investigation_committed_object_is_exempt(migrated_url: str) -> None:
    """AC-4: with the state gate gone, the per-key committed-object skip carries the whole safety
    burden for an OPEN investigation — a finalized rootfs (with an `artifacts` row) is never
    deleted, though its spent manifest still goes."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="open")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await _insert_artifact_row(
                seed, owner_kind="investigations", owner_id=inv_id, object_key=f"{prefix}rootfs"
            )
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == [f"{prefix}stray"]  # committed object exempt, stray bytes go
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_reaps_uncommitted_objects_past_deadline_for_abandoned_investigation(
    migrated_url: str,
) -> None:
    """An ``abandoned`` investigation past its manifest deadline has its uncommitted staged object +
    manifest reaped (ADR-0441 §6) — the terminal state means finalize will never land."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="abandoned")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}rootfs", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_closed_investigation_committed_object_is_exempt(migrated_url: str) -> None:
    """Even for a terminal investigation, a committed object (with an ``artifacts`` row) is never
    deleted — the per-key skip protects it, so only the truly uncommitted staged bytes go."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="closed")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await _insert_artifact_row(
                seed, owner_kind="investigations", owner_id=inv_id, object_key=f"{prefix}rootfs"
            )
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == []  # committed object exempt even for a closed investigation
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_reaps_past_deadline_uncommitted_manifest_on_active_investigation(
    migrated_url: str,
) -> None:
    """AC-3, the ``active`` half: the reap keys on the deadline, not on investigation state, so an
    in-flight investigation's lapsed upload window is collected too (ADR-0444 decision 2)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="active")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == [f"{prefix}rootfs"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_open_investigation_within_deadline_is_not_reaped(migrated_url: str) -> None:
    """AC-5, the investigations half: the deadline is the only gate, so a live window on an OPEN
    investigation is still untouched — the reap did not become unconditional."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="open")
            prefix, request = _investigation_manifest(inv_id, timedelta(hours=1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is not None

    asyncio.run(_run())


def test_reap_one_owner_declines_renewed_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            system_id = await seed_system(conn)
            run_id = await seed_run(conn, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(hours=1))
            await upload_manifest.replace_manifest(conn, request)
            store = _FakeStore({prefix: [f"{prefix}kernel"]})
            outcome = await _reap_one_owner(conn, store, "runs", run_id)
            assert outcome == ReapOutcome(reaped=False, attempted=0, undeleted=0)
            assert store.deleted == []

    asyncio.run(_run())


def test_mid_sweep_delete_failure_still_removes_the_manifest_row(migrated_url: str) -> None:
    """#1552 / ADR-0453 §1: the row delete commits *before* the objects are swept, so a delete
    that fails partway through can no longer restore a window over deleted bytes.

    Under the old object-first order this exact scenario rolled the transaction back, leaving an
    ``upload_manifests`` row byte-identical to the one finalize validated — same prefix, same
    deadline — over an object that was already gone. ``_require_unreaped_window`` compares deadline
    *identity* and a rollback re-stamps nothing, so the runs single-PUT finalize would sail past it
    and register ``artifacts`` rows against deleted keys.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        keys = [f"{prefix}a", f"{prefix}b", f"{prefix}c"]
        store = _FailingStore({prefix: keys}, fail_keys={f"{prefix}b"})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError):  # reported once the pass is complete
                await run_repair(pool, _reap(store))
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None
        # AC-4: one bad key strands neither the keys before it nor the keys after it.
        assert store.attempted == keys
        assert store.deleted == [f"{prefix}a", f"{prefix}c"]

    asyncio.run(_run())


def test_a_totally_failed_sweep_is_reported_as_a_failed_pass(migrated_url: str) -> None:
    """ADR-0453 §3: a store rejecting every delete must not read as healthy repair work.

    ``_run_repair_plan`` increments the ADR-0190 group-E error counter only for a repair that
    *raises*; a repair that returns normally has its count added to `kdive.reconciler.repairs` and
    emits no error. Swallowing the sweep failures outright would therefore make the one condition
    that leaks bytes permanently the best-looking pass on the dashboard. The pass forfeits its
    reaped count to say so — the count is a gauge, the error is the alert, and the rows are
    durably deleted either way.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FailingStore({prefix: [f"{prefix}a"]}, fail_keys={f"{prefix}a"})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _reap(store))
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert "1 object(s) across 1 reaped owner(s)" in str(caught.value)

    asyncio.run(_run())


def test_a_clean_sweep_returns_the_reaped_count_and_does_not_raise(migrated_url: str) -> None:
    """The counterweight: the end-of-pass raise is conditional on a real failure.

    Without this, the previous test would be satisfied by a reaper that raised unconditionally.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FailingStore({prefix: [f"{prefix}a"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _reap(store)) == 1
        assert store.deleted == [f"{prefix}a"]

    asyncio.run(_run())


def test_a_key_that_gains_an_artifacts_row_after_the_claim_is_still_deleted(
    migrated_url: str,
) -> None:
    """Pins ADR-0453's second residual (#1557) as *known* behaviour rather than a claim.

    The exemption is decided in phase 1 and phase 2 re-reads nothing, so a key that acquires a
    committed ``artifacts`` row between the two — which the reaped window's own finalizes cannot
    do, but a re-mint, a `control.capture_traffic` retry, or a vmcore finalize can, because
    ``local/runs/<id>/`` is the Run's *owner* prefix and phase 2 holds no `RUN` lock — has its
    object deleted anyway, leaving the row dangling.

    This test exists so the residual has a reproducer and so the day it is closed, it fails
    loudly rather than passing silently.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        pcap_key = f"{prefix}pcap-late"

        def _commit_the_row_mid_sweep() -> None:
            # Stands in for the concurrent writer taking the RUN lock phase 2 no longer holds.
            with psycopg.connect(migrated_url, autocommit=True) as writer:
                writer.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, "
                    "    sensitivity, retention_class) VALUES (%s, %s, %s, %s, %s, %s)",
                    ("runs", run_id, pcap_key, "etag-1", "sensitive", "pcap"),
                )

        store = _RaceStore({prefix: [pcap_key]}, before_delete=_commit_the_row_mid_sweep)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _reap(store)) == 1
        assert store.deleted == [pcap_key]  # the disclosed residual: row committed, object gone
        async with await connect(migrated_url) as check:
            row = await (
                await check.execute("SELECT 1 FROM artifacts WHERE object_key = %s", (pcap_key,))
            ).fetchone()
        assert row is not None

    asyncio.run(_run())


def test_undeleted_objects_are_reported_at_error(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0453 §3 and §Consequences: the leaked bytes must be *reported*, not just tolerated.

    Nothing in this tree sweeps the upload prefix — `gc_expired_build_artifacts` is row-driven
    over `artifacts` and the `images/` orphan scan is a different prefix — so once the manifest
    row is gone this log is the only trace the objects were ever there.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FailingStore(
            {prefix: [f"{prefix}a", f"{prefix}b"]}, fail_keys={f"{prefix}a", f"{prefix}b"}
        )
        with caplog.at_level(logging.WARNING, logger="kdive.reconciler.cleanup.uploads"):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                with pytest.raises(CategorizedError):
                    await run_repair(pool, _reap(store))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(warnings) == 2  # one per failed key, naming the key
        logged = [r.getMessage() for r in warnings]
        assert [k for k in (f"{prefix}a", f"{prefix}b") if any(k in m for m in logged)] == [
            f"{prefix}a",
            f"{prefix}b",
        ]
        assert len(errors) == 1  # one summary, naming the counts and the owner
        assert f"left 2 of 2 object(s) for owner runs/{run_id}" in errors[0].getMessage()

    asyncio.run(_run())


def test_the_prefix_is_logged_before_the_sweep_starts(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0453 §3: the prefix and the key count must be on the record *before* the first delete.

    An abort that never reaches the sweep's own reporting — cancellation at shutdown, a process
    kill — leaves this as the only record of when the claim happened and how much it doomed.
    Asserted by snapshotting the log from inside the first delete, so moving the call after the
    sweep fails this test rather than passing it.
    """
    seen_at_first_delete: list[str] = []

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        keys = [f"{prefix}a", f"{prefix}b"]

        def _snapshot_the_log() -> None:
            seen_at_first_delete.extend(r.getMessage() for r in caplog.records)

        store = _RaceStore({prefix: keys}, before_delete=_snapshot_the_log)
        with caplog.at_level(logging.INFO, logger="kdive.reconciler.cleanup.uploads"):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                assert await run_repair(pool, _reap(store)) == 1
        assert store.deleted == keys
        claimed = [m for m in seen_at_first_delete if "claimed; sweeping" in m]
        assert len(claimed) == 1  # already logged when the first delete ran
        assert prefix in claimed[0]
        assert "2 object(s)" in claimed[0]

    asyncio.run(_run())


def test_a_failing_list_prefix_aborts_the_pass_without_deleting_the_row(
    migrated_url: str,
) -> None:
    """A store outage during phase 1 is benign, and this pins that it stays benign.

    ``ObjectStore.list_prefix`` raises ``CategorizedError`` on any client or transport fault, and
    phase 1 does not catch it — so the pass ends and later candidates are dropped. That asymmetry
    with the sweep's per-key tolerance (ADR-0453 §3) is deliberate: phase 1 aborts *before* the
    row delete commits, so the transaction rolls back with nothing deleted and the next 30-second
    pass retries the same candidates. Nothing is lost, which is precisely what is not true once
    the row delete has committed.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FailingStore({prefix: [f"{prefix}kernel"]}, fail_list_prefixes={prefix})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            try:
                await run_repair(pool, _reap(store))
            except CategorizedError:
                pass
            else:
                raise AssertionError("a failing list_prefix must not be silently absorbed")
        assert store.attempted == []
        async with await connect(migrated_url) as check:
            # The window survives intact: nothing was deleted, so the retry loses nothing.
            assert await upload_manifest.get_manifest(check, "runs", run_id) is not None

    asyncio.run(_run())


def test_successful_sweep_logs_no_failure_summary(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The ERROR summary is conditional on a failure, not emitted on every reap."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}a"]})
        with caplog.at_level(logging.WARNING, logger="kdive.reconciler.cleanup.uploads"):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                assert await run_repair(pool, _reap(store)) == 1
        assert caplog.records == []

    asyncio.run(_run())


def test_mid_sweep_delete_failure_does_not_abandon_later_owners(migrated_url: str) -> None:
    """AC-4, the cross-owner half: one bad key must not cost a later owner its reap.

    ``repair_abandoned_uploads`` has no per-candidate ``try``, so a raise out of the object sweep
    would drop every later candidate on the floor for one unrelated object-store hiccup. Each
    owner here loses one key of two, which is a bad key rather than a refusing store — so the pass
    walks every candidate and only then reports.
    """

    async def _run() -> None:
        prefixes: list[str] = []
        run_ids: list[UUID] = []
        async with await connect(migrated_url) as seed:
            for _ in range(2):
                system_id = await seed_system(seed)
                run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
                prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
                await upload_manifest.replace_manifest(seed, request)
                prefixes.append(prefix)
                run_ids.append(run_id)
        objects = {p: [f"{p}kernel", f"{p}stray"] for p in prefixes}
        store = _FailingStore(objects, fail_keys={f"{p}kernel" for p in prefixes})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _reap(store))
        # Both owners reaped despite a failed key each; the report names both, none left unclaimed.
        assert "2 object(s) across 2 reaped owner(s)" in str(caught.value)
        assert "Left 0 candidate(s) unclaimed" in str(caught.value)
        assert sorted(store.deleted) == sorted(f"{p}stray" for p in prefixes)
        async with await connect(migrated_url) as check:
            for run_id in run_ids:
                assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_a_wholly_refused_sweep_stops_the_pass_claiming_more_owners(migrated_url: str) -> None:
    """ADR-0453 §3, the brake: a store that lists but refuses every delete must not orphan the
    whole backlog in one pass.

    One bad key is a bad key; a whole owner's sweep failing is the signature of a condition that
    will fail the next owner too — a bucket policy without ``s3:DeleteObject``, an endpoint
    rejecting DELETE. The candidate select is unbounded, and every candidate claimed under that
    fault costs an irreversible row delete over bytes nothing reclaims (#1556), so the pass stops.
    A failing ``list_prefix`` already ended the pass for the same reason; this makes the
    delete-side treatment consistent rather than the opposite.

    Order-independent: every owner's delete fails, so whichever the scan yields first is the one
    that gets reaped, and exactly one must survive.
    """

    async def _run() -> None:
        prefixes: list[str] = []
        run_ids: list[UUID] = []
        async with await connect(migrated_url) as seed:
            for _ in range(3):
                system_id = await seed_system(seed)
                run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
                prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
                await upload_manifest.replace_manifest(seed, request)
                prefixes.append(prefix)
                run_ids.append(run_id)
        objects = {p: [f"{p}kernel"] for p in prefixes}
        store = _FailingStore(objects, fail_keys={f"{p}kernel" for p in prefixes})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _reap(store))
        assert "1 object(s) across 1 reaped owner(s)" in str(caught.value)
        assert "Left 2 candidate(s) unclaimed" in str(caught.value)
        assert len(store.attempted) == 1  # the brake tripped after the first owner
        async with await connect(migrated_url) as check:
            surviving = [
                r
                for r in run_ids
                if await upload_manifest.get_manifest(check, "runs", r) is not None
            ]
        assert len(surviving) == 2  # two windows kept for the next pass, bytes and rows intact

    asyncio.run(_run())


def test_manifest_row_is_already_committed_gone_when_the_first_object_is_deleted(
    migrated_url: str,
) -> None:
    """AC-1: the ordering itself, observed rather than inferred from the end state.

    Both orders leave no row once the reap returns, so the end state cannot tell them apart. This
    probes from inside the delete callback on a separate connection, which sees only committed
    state — under the old order it saw the row still present.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        keys = [f"{prefix}a", f"{prefix}b"]
        store = _ManifestProbingStore(
            {prefix: keys}, url=migrated_url, owner_kind="runs", owner_id=run_id
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == keys
        assert store.manifest_absent_at_delete == [True, True]

    asyncio.run(_run())


def test_all_committed_objects_reaps_row_without_any_delete(migrated_url: str) -> None:
    """The empty doomed set: every listed key holds an ``artifacts`` row, so the set-valued
    exemption query leaves nothing to sweep and the sweep phase issues no delete at all."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.SUCCEEDED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            for name in ("kernel", "initrd"):
                await _insert_artifact_row(
                    seed, owner_kind="runs", owner_id=run_id, object_key=f"{prefix}{name}"
                )
            await upload_manifest.replace_manifest(seed, request)
        store = _FailingStore(
            {prefix: [f"{prefix}kernel", f"{prefix}initrd"]},
            fail_keys={f"{prefix}kernel", f"{prefix}initrd"},
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.attempted == []  # exempt keys are never even attempted
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reap_with_no_objects_under_the_prefix_still_removes_the_row(migrated_url: str) -> None:
    """An empty prefix listing: nothing to sweep, but the abandoned window is still collected."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            _prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_lock_scope_rejects_unknown_owner_kind() -> None:
    # AC-6: the state gate is gone, but its fail-loud arm survives on the lock-scope lookup — an
    # unrecognized owner kind must never be locked under a guessed scope (ADR-0444 decision 2).
    try:
        _lock_scope_for(cast(upload_manifest.UploadOwnerKind, "allocations"))
    except ValueError as exc:
        assert str(exc) == "unsupported upload owner kind: allocations"
    else:
        raise AssertionError("unknown owner kind should fail loud, not resolve to a scope")
