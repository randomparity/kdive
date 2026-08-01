"""The upload reap's phase-2 re-check under the owner lock (ADR-0509, #1557).

``_claim_abandoned_prefix`` commits the ``upload_manifests`` row delete and returns a key list that
stops being true at that commit. Phase 2 used to delete that list unconditionally, so any writer
reaching the owner prefix in between had its bytes destroyed: a re-mint (the documented recovery
from a reap), a ``control.capture_traffic`` retry, or the vmcore ``put_stream``/``finalize_capture``
pair. These tests drive each of those writers into the gap and assert the bytes survive.

**How the gap is reached.** ``_HookedStore.before_delete`` fires from inside ``store.delete``, on
the ``asyncio.to_thread`` worker, so a sync connection opened there sees only committed state. Each
test gives the window **two** keys and fires the hook once, before the first key's delete: the
writer lands after the first key's re-check and before the second's, which is exactly the interval
phase 2 leaves open. The first key is the sacrificial one and is expected to go; the second is the
one the writer touched and must survive.

The hook cannot deadlock against the guard even though the guard holds ``LockScope.RUN``: every
writer modelled here takes row locks only, and the advisory lock the guard holds blocks nothing but
another advisory acquisition of the same key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import ObjectVersion, VersionBatch, VersionPage
from kdive.artifacts.upload_manifest import (
    INVESTIGATION_UPLOAD_OWNER,
    RUN_UPLOAD_OWNER,
    UPLOAD_TENANT,
    lock_scope_for,
)
from kdive.db.locks import advisory_xact_lock
from kdive.domain.capacity.state import RunState
from kdive.reconciler.cleanup.uploads import (
    ReapOutcome,
    _sweep_uncommitted_objects,
    repair_abandoned_uploads,
)
from tests.reconciler.conftest import (
    connect,
    run_repair,
    seed_run,
    seed_running_job,
    seed_system,
)

_SACRIFICE = "sacrificial-chunk"
_CONTESTED = "kernel"


class _HookedStore:
    """A versioned store that runs ``before_delete`` once inside exact batch deletion."""

    def __init__(self, prefix: str, keys: list[str], before_delete: Callable[[], None]) -> None:
        self._objects: dict[str, list[str]] = {prefix: keys}
        self._before_delete = before_delete
        self._fired = False
        self._versions = {key: [self._version(key, "old", is_latest=True)] for key in keys}
        self.deleted: list[str] = []
        self.deleted_versions: list[tuple[str, str]] = []

    def list_prefix(self, prefix: str) -> list[str]:
        return list(self._objects.get(prefix, []))

    def iter_prefix_version_pages(self, prefix: str) -> Iterator[VersionPage]:
        entries = tuple(
            version
            for key in self._objects.get(prefix, [])
            for version in self._versions.get(key, [])
        )
        yield VersionPage(entries, False, None, None)

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch:
        versions = list(self._versions.get(key, ()))
        complete = len(versions) <= limit
        if not complete:
            latest = [version for version in versions if version.is_latest]
            versions = [*latest, *(version for version in versions if not version.is_latest)]
        return VersionBatch(key, tuple(versions[:limit]), complete)

    def delete_batch(self, batch: VersionBatch) -> bool:
        if not self._fired:
            self._fired = True
            self._before_delete()
        targets = [target for target in batch.targets if not target.is_latest]
        if batch.history_complete:
            targets.extend(target for target in batch.targets if target.is_latest)
        selected = {target.version_id for target in targets}
        self._versions[batch.key] = [
            version
            for version in self._versions.get(batch.key, ())
            if version.version_id not in selected
        ]
        self.deleted.extend(target.key for target in targets)
        self.deleted_versions.extend((target.key, target.version_id) for target in targets)
        return batch.history_complete

    def put(self, key: str, version_id: str = "peer") -> None:
        prior = [replace(version, is_latest=False) for version in self._versions.get(key, ())]
        self._versions[key] = [*prior, self._version(key, version_id, is_latest=True)]

    def versions(self, key: str) -> set[str]:
        return {version.version_id for version in self._versions.get(key, ())}

    @staticmethod
    def _version(key: str, version_id: str, *, is_latest: bool) -> ObjectVersion:
        return ObjectVersion(
            key=key,
            version_id=version_id,
            last_modified=datetime(2026, 7, 1, tzinfo=UTC),
            etag=f"etag-{version_id}",
            is_latest=is_latest,
            is_delete_marker=False,
        )


def _noop() -> None:
    return None


async def _seed_expired_run(url: str) -> tuple[UUID, str]:
    """A CREATED Run whose upload window is already past its deadline."""
    async with await connect(url) as seed:
        system_id = await seed_system(seed)
        run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
        prefix = f"{UPLOAD_TENANT}/{RUN_UPLOAD_OWNER}/{run_id}/"
        await _insert_manifest(seed, RUN_UPLOAD_OWNER, run_id, prefix, timedelta(seconds=-1))
    return run_id, prefix


async def _seed_expired_investigation(url: str) -> tuple[UUID, str]:
    """An open investigation whose rootfs upload window is already past its deadline."""
    inv_id = uuid4()
    prefix = f"{UPLOAD_TENANT}/{INVESTIGATION_UPLOAD_OWNER}/{inv_id}/"
    async with await connect(url) as seed:
        await seed.execute(
            "INSERT INTO investigations (id, principal, project, title, state) "
            "VALUES (%s, 'user-1', 'proj', 't', 'open')",
            (inv_id,),
        )
        await _insert_manifest(
            seed, INVESTIGATION_UPLOAD_OWNER, inv_id, prefix, timedelta(seconds=-1)
        )
    return inv_id, prefix


async def _insert_manifest(
    conn: psycopg.AsyncConnection, owner_kind: str, owner_id: UUID, prefix: str, ttl: timedelta
) -> None:
    await conn.execute(
        "INSERT INTO upload_manifests (owner_kind, owner_id, prefix, manifest, deadline) "
        "VALUES (%s, %s, %s, %s, now() + %s) "
        "ON CONFLICT (owner_kind, owner_id) DO UPDATE SET "
        "  prefix = EXCLUDED.prefix, manifest = EXCLUDED.manifest, deadline = EXCLUDED.deadline",
        (owner_kind, owner_id, prefix, Jsonb([]), ttl),
    )


def _remint(url: str, owner_kind: str, owner_id: UUID, prefix: str) -> Callable[[], None]:
    """A writer that re-mints the owner's upload window — ADR-0448's documented recovery."""

    def _fire() -> None:
        with psycopg.connect(url, autocommit=True) as writer:
            writer.execute(
                "INSERT INTO upload_manifests (owner_kind, owner_id, prefix, manifest, deadline) "
                "VALUES (%s, %s, %s, %s, now() + interval '1 hour')",
                (owner_kind, owner_id, prefix, Jsonb([])),
            )

    return _fire


def _commit_artifacts_row(
    url: str, owner_kind: str, owner_id: UUID, key: str
) -> Callable[[], None]:
    """A writer that re-PUTs and registers a key — the ``capture_traffic`` retry arm."""

    def _fire() -> None:
        with psycopg.connect(url, autocommit=True) as writer:
            writer.execute(
                "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                "    retention_class) VALUES (%s, %s, %s, 'etag-1', 'sensitive', 'pcap')",
                (owner_kind, owner_id, key),
            )

    return _fire


def _reap(store: _HookedStore):
    return lambda conn: repair_abandoned_uploads(conn, store)


def test_a_remint_landing_mid_sweep_keeps_its_bytes(migrated_url: str) -> None:
    """The issue's headline race: a re-mint between the row-delete commit and the key's delete.

    Upload keys are owner-addressed, so the re-minted window owns ``kernel`` — the very name the
    reaped window's key list carries. Before ADR-0509 phase 2 deleted it regardless.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _HookedStore(
            prefix,
            [f"{prefix}{_SACRIFICE}", f"{prefix}{_CONTESTED}"],
            _remint(migrated_url, RUN_UPLOAD_OWNER, run_id, prefix),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _reap(store)) == 1
        assert store.deleted == [f"{prefix}{_SACRIFICE}"]
        assert f"{prefix}{_CONTESTED}" not in store.deleted

    asyncio.run(_run())


def test_a_remint_landing_mid_sweep_keeps_its_bytes_on_the_investigation_lane(
    migrated_url: str,
) -> None:
    """The same, for ``investigations``.

    ADR-0453 recorded this lane as never exposed, because ``complete_rootfs_upload`` finalizes under
    the ``INVESTIGATION`` lock the reaper also took. The #1552 split ended that: phase 2 held no
    lock at all, so the lane was in scope for the residual until ADR-0509 put the lock back.
    """

    async def _run() -> None:
        inv_id, prefix = await _seed_expired_investigation(migrated_url)
        store = _HookedStore(
            prefix,
            [f"{prefix}{_SACRIFICE}", f"{prefix}rootfs"],
            _remint(migrated_url, INVESTIGATION_UPLOAD_OWNER, inv_id, prefix),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _reap(store)) == 1
        assert store.deleted == [f"{prefix}{_SACRIFICE}"]

    asyncio.run(_run())


def test_a_key_that_gains_an_artifacts_row_mid_sweep_is_spared(migrated_url: str) -> None:
    """The ``capture_traffic`` arm: a retry re-PUTs the pcap and commits its row mid-sweep.

    Replaces ``test_a_key_that_gains_an_artifacts_row_after_the_claim_is_still_deleted``, which
    pinned the unguarded behaviour and was written to fail the day this closed. No re-mint is
    involved — this arm never touches ``upload_manifests`` — so it is the fence that would have
    survived a manifest-only re-read.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        pcap = f"{prefix}pcap-late"
        store = _HookedStore(
            prefix,
            [f"{prefix}{_SACRIFICE}", pcap],
            _commit_artifacts_row(migrated_url, RUN_UPLOAD_OWNER, run_id, pcap),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _reap(store)) == 1
        assert store.deleted == [f"{prefix}{_SACRIFICE}"]
        async with await connect(migrated_url) as check:
            row = await (
                await check.execute("SELECT 1 FROM artifacts WHERE object_key = %s", (pcap,))
            ).fetchone()
        assert row is not None  # the row is committed and its bytes are still there

    asyncio.run(_run())


def _lease_hook(url: str, run_id: UUID, job_id: UUID) -> Callable[[], None]:
    """A writer that declares itself with an ADR-0502 write lease — the vmcore arm."""

    def _fire() -> None:
        with psycopg.connect(url, autocommit=True) as writer:
            writer.execute(
                "INSERT INTO object_write_leases (owner_kind, owner_id, job_id) "
                "VALUES (%s, %s, %s)",
                (RUN_UPLOAD_OWNER, run_id, job_id),
            )

    return _fire


@pytest.mark.parametrize(
    ("lease_seconds", "expect_spared"),
    [(300, True), (-300, False)],
    ids=["live-holder-spares", "lapsed-holder-does-not"],
)
def test_a_write_lease_taken_mid_sweep_spares_its_owners_keys(
    migrated_url: str, lease_seconds: int, expect_spared: bool
) -> None:
    """A lease fences the reaper only while its holding job is a live claim.

    This is what completes ADR-0502's guarantee: before ADR-0509 a leased ``capture_vmcore`` write
    was safe from ``repair_leaked_upload_objects`` and not from ``repair_abandoned_uploads``, so
    "a declared write is never destroyed" held against one deleter out of two. The lapsed arm is the
    control — it proves the sparing comes from ``LIVE_HOLDER_SQL`` and not from the row's mere
    existence.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        async with await connect(migrated_url) as seed:
            job_id = await seed_running_job(
                seed,
                f"{run_id}:capture_vmcore:host_dump",
                kind="capture_vmcore",
                lease_seconds=lease_seconds,
                attempt=1,
                max_attempts=3,
            )
        vmcore = f"{prefix}vmcore"
        store = _HookedStore(
            prefix,
            [f"{prefix}{_SACRIFICE}", vmcore],
            _lease_hook(migrated_url, run_id, job_id),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _reap(store)) == 1
        assert (vmcore not in store.deleted) is expect_spared

    asyncio.run(_run())


def test_a_held_owner_lock_leaves_the_key_for_the_orphan_sweep(migrated_url: str) -> None:
    """A writer holding the owner lock is not waited on: the key is declined, not failed.

    Phase 2 is driven directly because the lock has to be taken *after* phase 1 commits — phase 1
    takes the same lock, so a holder spanning the whole pass makes the claim defer (ADR-0510) and
    the sweep is never reached. A reconciler pass has no deadline, so waiting here would put
    allocation expiry and
    orphaned-System repair behind whatever the holder is doing (ADR-0455 §5); the key is left for
    ``repair_leaked_upload_objects``, which drains exactly this residue.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        key = f"{prefix}{_CONTESTED}"
        store = _HookedStore(prefix, [key], _noop)
        holder = await psycopg.AsyncConnection.connect(migrated_url)
        sweeper = await psycopg.AsyncConnection.connect(migrated_url)
        try:
            async with (
                holder.transaction(),
                advisory_xact_lock(holder, lock_scope_for(RUN_UPLOAD_OWNER), run_id),
            ):
                outcome = await _sweep_uncommitted_objects(
                    sweeper, store, RUN_UPLOAD_OWNER, run_id, [key]
                )
        finally:
            await holder.close()
            await sweeper.close()
        assert outcome.deleted == 0
        assert outcome.declined == 1
        assert outcome.undeleted == 0  # a decline is the guard working, not a store fault
        assert store.deleted == []

    asyncio.run(_run())


def test_the_same_key_is_deleted_once_the_owner_lock_is_free(migrated_url: str) -> None:
    """The mutation control for the test above: without a holder, that key goes.

    Without this, ``declined == 1`` would also be satisfied by a guard that declined every key
    unconditionally — which would pass while reaping nothing at all.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        key = f"{prefix}{_CONTESTED}"
        store = _HookedStore(prefix, [key], _noop)
        sweeper = await psycopg.AsyncConnection.connect(migrated_url)
        async with await connect(migrated_url) as seed:
            await seed.execute(
                "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
                (RUN_UPLOAD_OWNER, run_id),
            )
        try:
            outcome = await _sweep_uncommitted_objects(
                sweeper, store, RUN_UPLOAD_OWNER, run_id, [key]
            )
        finally:
            await sweeper.close()
        assert (outcome.deleted, outcome.declined, outcome.undeleted) == (1, 0, 0)
        assert store.deleted == [key]

    asyncio.run(_run())


def test_a_peer_put_after_the_same_key_s_fence_survives_exact_delete(
    migrated_url: str,
) -> None:
    """A version created after capture is not selected by the post-unlock batch delete."""

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        key = f"{prefix}{_CONTESTED}"
        store: _HookedStore
        store = _HookedStore(prefix, [key], lambda: store.put(key))
        async with await connect(migrated_url) as seed:
            await seed.execute(
                "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
                (RUN_UPLOAD_OWNER, run_id),
            )
        sweeper = await psycopg.AsyncConnection.connect(migrated_url)
        try:
            outcome = await _sweep_uncommitted_objects(
                sweeper, store, RUN_UPLOAD_OWNER, run_id, [key]
            )
        finally:
            await sweeper.close()
        assert (outcome.deleted, outcome.declined, outcome.undeleted) == (1, 0, 0)
        assert store.deleted_versions == [(key, "old")]
        assert store.versions(key) == {"peer"}

    asyncio.run(_run())


def test_an_incomplete_reap_batch_retains_latest_and_does_not_starve_a_sibling(
    migrated_url: str,
) -> None:
    """A hot key makes bounded progress; its survivors stay visible to orphan repair."""

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        hot, sibling = f"{prefix}a-hot", f"{prefix}b-sibling"
        store = _HookedStore(prefix, [hot, sibling], _noop)
        for number in range(2, 26):
            store.put(hot, f"v{number}")
        async with await connect(migrated_url) as seed:
            await seed.execute(
                "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
                (RUN_UPLOAD_OWNER, run_id),
            )
        sweeper = await psycopg.AsyncConnection.connect(migrated_url)
        try:
            outcome = await _sweep_uncommitted_objects(
                sweeper, store, RUN_UPLOAD_OWNER, run_id, [hot, sibling]
            )
        finally:
            await sweeper.close()
        assert (outcome.deleted, outcome.declined, outcome.undeleted) == (2, 0, 0)
        assert store.versions(hot) == {"v20", "v21", "v22", "v23", "v24", "v25"}
        assert store.versions(sibling) == set()

    asyncio.run(_run())


def test_declines_alone_never_look_like_a_refusing_store() -> None:
    """ADR-0453 §4's brake fires on a wholly *refused* sweep, and a decline is not a refusal.

    A pass that stopped claiming candidates because one owner's keys were all spared would let a
    single long-running writer stall the entire past-deadline backlog, every 30 seconds, forever.
    """
    spared = ReapOutcome(reaped=True, deferred=False, attempted=0, declined=3, undeleted=0)
    refused = ReapOutcome(reaped=True, deferred=False, attempted=3, declined=0, undeleted=3)
    partly = ReapOutcome(reaped=True, deferred=False, attempted=3, declined=2, undeleted=1)
    assert spared.store_refused_everything is False
    assert refused.store_refused_everything is True
    assert partly.store_refused_everything is False
