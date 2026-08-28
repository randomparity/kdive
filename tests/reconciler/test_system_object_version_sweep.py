"""Durable cleanup for rowless local and remote System object versions (ADR-0524, #1751)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import ObjectVersion, VersionBatch, VersionPage
from kdive.db.locks import LockScope, _lock_key, advisory_xact_lock
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.console_hosting import CollectorRegistry, ConsoleHostingLoop
from kdive.reconciler.cleanup import system_object_versions as sweep
from kdive.reconciler.cleanup.provider_resources.console_reaping import reap_console_collectors
from tests.reconciler.conftest import connect, run_repair, seed_system

_NOW = datetime(2026, 7, 31, tzinfo=UTC)


def _version(
    key: str,
    version_id: str = "v1",
    *,
    latest: bool = True,
) -> ObjectVersion:
    return ObjectVersion(
        key=key,
        version_id=version_id,
        last_modified=_NOW,
        etag=f"etag-{version_id}",
        is_latest=latest,
        is_delete_marker=False,
    )


def _history(key: str, count: int) -> list[ObjectVersion]:
    return [_version(key, f"v{index}", latest=index == count - 1) for index in range(count)]


class _Store:
    """Version inventory with call recording and exact-key delete faults."""

    def __init__(self, histories: dict[str, list[ObjectVersion]]) -> None:
        self.histories = {key: list(versions) for key, versions in histories.items()}
        self.page_calls: list[tuple[str, str | None, str | None, int]] = []
        self.capture_calls: list[tuple[str, int]] = []
        self.deleted: list[tuple[str, str]] = []
        self.fail_delete_once: set[str] = set()
        self.fail_listing = False
        self.before_delete: Callable[[], None] | None = None

    def list_version_page(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
        max_keys: int = 1000,
    ) -> VersionPage:
        self.page_calls.append((prefix, key_marker, version_id_marker, max_keys))
        if self.fail_listing:
            raise CategorizedError(
                f"list failed for {prefix}", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        entries = sorted(
            (
                version
                for key, versions in self.histories.items()
                if key.startswith(prefix) and (key_marker is None or key > key_marker)
                for version in versions
            ),
            key=lambda item: (item.key, item.version_id),
        )
        selected = entries[:max_keys]
        truncated = len(entries) > max_keys
        last = selected[-1] if selected else None
        return VersionPage(
            tuple(selected),
            truncated,
            last.key if truncated and last is not None else None,
            last.version_id if truncated and last is not None else None,
        )

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch:
        self.capture_calls.append((key, limit))
        versions = self.histories.get(key, [])
        complete = len(versions) <= limit
        if not complete:
            versions = [item for item in versions if item.is_latest] + [
                item for item in versions if not item.is_latest
            ]
        return VersionBatch(key, tuple(versions[:limit]), complete)

    def delete_batch(self, batch: VersionBatch) -> bool:
        if self.before_delete is not None:
            self.before_delete()
        if batch.key in self.fail_delete_once:
            self.fail_delete_once.remove(batch.key)
            raise CategorizedError(
                f"delete failed for {batch.key}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        deletable = (
            batch.targets
            if batch.history_complete
            else tuple(target for target in batch.targets if not target.is_latest)
        )
        deleted_ids = {target.version_id for target in deletable}
        self.histories[batch.key] = [
            item for item in self.histories.get(batch.key, []) if item.version_id not in deleted_ids
        ]
        if not self.histories[batch.key]:
            self.histories.pop(batch.key)
        self.deleted.extend((target.key, target.version_id) for target in deletable)
        return batch.history_complete


@dataclass
class _Gate:
    leader: bool
    registry: CollectorRegistry

    @property
    def is_leader(self) -> bool:
        return self.leader

    @asynccontextmanager
    async def reserve_system_object_cleanup(self, system_id: UUID) -> AsyncIterator[bool]:
        yield self.leader and not self.registry.has(system_id)


def _local_key(system_id: UUID) -> str:
    return f"local/systems/{system_id}/console-rotation-state.json"


def _remote_key(system_id: UUID, index: int = 0) -> str:
    return f"remote-libvirt/systems/{system_id}/console-parts-{index}"


async def _cursor(url: str, lane: str) -> str | None:
    async with await connect(url) as conn:
        row = await (
            await conn.execute(
                "SELECT after_key FROM system_object_sweep_cursors WHERE lane = %s", (lane,)
            )
        ).fetchone()
    assert row is not None
    return row[0]


async def _run_local(url: str, store: _Store) -> int:
    async with AsyncConnectionPool(url, min_size=1, max_size=4) as pool:
        return await run_repair(
            pool, lambda conn: sweep.sweep_local_system_object_versions(conn, store)
        )


async def _run_remote(url: str, store: _Store, gate: _Gate | None) -> int:
    async with AsyncConnectionPool(url, min_size=1, max_size=4) as pool:
        return await run_repair(
            pool, lambda conn: sweep.sweep_remote_system_object_versions(conn, store, gate)
        )


@pytest.mark.parametrize(
    "key",
    [
        "local/systems/12345678-1234-1234-1234-123456789abc/console-rotation-state.json",
    ],
)
def test_local_parser_accepts_only_the_rotation_sidecar(key: str) -> None:
    assert sweep.parse_local_system_object_key(key) == UUID("12345678-1234-1234-1234-123456789abc")


@pytest.mark.parametrize(
    "key",
    [
        "local/systems/not-a-uuid/console-rotation-state.json",
        "local/systems/12345678-1234-1234-1234-123456789abc/console",
        "local/systems/12345678-1234-1234-1234-123456789abc/console-part-0-0",
        "local/systems/12345678-1234-1234-1234-123456789abc/diagnostic-sysrq-1",
        "local/systems/12345678-1234-1234-1234-123456789abc/console-rotation-state.json/extra",
        "other/systems/12345678-1234-1234-1234-123456789abc/console-rotation-state.json",
        "local/runs/12345678-1234-1234-1234-123456789abc/console-rotation-state.json",
        "local/systems/12345678123412341234123456789abc/console-rotation-state.json",
        "local/systems/12345678-1234-1234-1234-123456789ABC/console-rotation-state.json",
    ],
)
def test_local_parser_rejects_every_near_miss(key: str) -> None:
    assert sweep.parse_local_system_object_key(key) is None


@pytest.mark.parametrize("index", ["0", "1", "27"])
def test_remote_parser_accepts_canonical_nonnegative_indices(index: str) -> None:
    key = f"remote-libvirt/systems/12345678-1234-1234-1234-123456789abc/console-parts-{index}"
    assert sweep.parse_remote_system_object_key(key) == UUID("12345678-1234-1234-1234-123456789abc")


@pytest.mark.parametrize(
    "name",
    [
        "console",
        "console-part-0-0",
        "console-parts",
        "console-parts-",
        "console-parts--1",
        "console-parts-01",
        "console-parts-+1",
        "console-parts-1.0",
        "diagnostic-sysrq-1",
    ],
)
def test_remote_parser_rejects_artifacts_and_noncanonical_indices(name: str) -> None:
    key = f"remote-libvirt/systems/12345678-1234-1234-1234-123456789abc/{name}"
    assert sweep.parse_remote_system_object_key(key) is None


@pytest.mark.parametrize(
    "key",
    [
        "remote-libvirt/systems/not-a-uuid/console-parts-0",
        "remote-libvirt/systems/12345678-1234-1234-1234-123456789abc/console-parts-0/extra",
        "local/systems/12345678-1234-1234-1234-123456789abc/console-parts-0",
        "remote-libvirt/runs/12345678-1234-1234-1234-123456789abc/console-parts-0",
    ],
)
def test_remote_parser_rejects_wrong_shape_tenant_and_owner(key: str) -> None:
    assert sweep.parse_remote_system_object_key(key) is None


def test_lane_lists_one_broad_page_and_caps_each_exact_capture(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        key = _local_key(system_id)
        store = _Store({key: _history(key, 21)})

        count = await _run_local(migrated_url, store)

        assert count == 19
        assert store.page_calls == [(sweep.LOCAL_SYSTEM_ROOT, None, None, 1000)]
        assert store.capture_calls == [(key, 20)]
        assert await _cursor(migrated_url, "local") is None
        assert {version.version_id for version in store.histories[key]} == {"v19", "v20"}

    asyncio.run(go())


def test_cursor_crosses_more_than_one_page_of_ineligible_history(migrated_url: str) -> None:
    async def go() -> None:
        ineligible = {
            f"local/systems/{UUID(int=index + 1)}/console": [
                _version(f"local/systems/{UUID(int=index + 1)}/console")
            ]
            for index in range(2001)
        }
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.FAILED)
        eligible = _local_key(system_id)
        store = _Store({**ineligible, eligible: [_version(eligible)]})

        assert await _run_local(migrated_url, store) == 0
        first_cursor = await _cursor(migrated_url, "local")
        ordered_ineligible = sorted(ineligible)
        assert first_cursor == ordered_ineligible[999]
        assert first_cursor is not None
        assert ordered_ineligible[1000] > first_cursor
        assert store.capture_calls == []

        assert await _run_local(migrated_url, store) == 0
        second_cursor = await _cursor(migrated_url, "local")
        assert second_cursor == ordered_ineligible[1999]
        assert second_cursor is not None
        assert second_cursor > ordered_ineligible[1000]
        assert store.capture_calls == []

        assert await _run_local(migrated_url, store) == 1
        assert await _cursor(migrated_url, "local") is None
        assert store.page_calls == [
            (sweep.LOCAL_SYSTEM_ROOT, None, None, 1000),
            (sweep.LOCAL_SYSTEM_ROOT, first_cursor, None, 1000),
            (sweep.LOCAL_SYSTEM_ROOT, second_cursor, None, 1000),
        ]
        assert store.capture_calls == [(eligible, 20)]
        assert eligible not in store.histories

    asyncio.run(go())


def test_terminal_page_partial_work_persists_last_considered_key(migrated_url: str) -> None:
    async def go() -> None:
        keys: list[str] = []
        store = _Store({})
        async with await connect(migrated_url) as seed:
            for _ in range(11):
                key = _local_key(await seed_system(seed, system_state=SystemState.TORN_DOWN))
                keys.append(key)
                store.histories[key] = _history(key, 20)
        ordered = sorted(keys)

        assert await _run_local(migrated_url, store) == 200

        assert await _cursor(migrated_url, "local") == ordered[9]
        assert len(store.capture_calls) == 10
        assert ordered[10] in store.histories

    asyncio.run(go())


def test_failed_early_key_wraps_and_retries_after_sibling_progress(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            first_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
            second_id = await seed_system(seed, system_state=SystemState.FAILED)
        first, second = sorted((_local_key(first_id), _local_key(second_id)))
        store = _Store({first: [_version(first)], second: [_version(second)]})
        store.fail_delete_once.add(first)

        with pytest.raises(CategorizedError, match="1 failed"):
            await _run_local(migrated_url, store)
        assert first in store.histories
        assert second not in store.histories
        assert await _cursor(migrated_url, "local") is None

        assert await _run_local(migrated_url, store) == 1
        assert store.histories == {}

    asyncio.run(go())


def test_stale_cursor_compare_and_set_cannot_regress_progress(migrated_url: str) -> None:
    async def go() -> None:
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            assert await sweep._compare_and_set_cursor(conn, "local", None, "later")
            assert not await sweep._compare_and_set_cursor(conn, "local", None, "earlier")
        assert await _cursor(migrated_url, "local") == "later"

    asyncio.run(go())


def test_listing_failure_does_not_advance_cursor(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            await seed.execute(
                "UPDATE system_object_sweep_cursors SET after_key = 'held' WHERE lane = 'local'"
            )
        store = _Store({})
        store.fail_listing = True

        with pytest.raises(CategorizedError, match="list failed"):
            await _run_local(migrated_url, store)

        assert await _cursor(migrated_url, "local") == "held"

    asyncio.run(go())


def test_cursor_update_failure_does_not_advance(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_update(*_args: object, **_kwargs: object) -> bool:
        raise psycopg.OperationalError("cursor database unavailable")

    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
            await seed.execute(
                "UPDATE system_object_sweep_cursors SET after_key = 'local/' WHERE lane = 'local'"
            )
        key = _local_key(system_id)
        store = _Store({key: [_version(key)]})
        monkeypatch.setattr(sweep, "_compare_and_set_cursor", fail_update)

        with pytest.raises(psycopg.OperationalError, match="cursor database unavailable"):
            await _run_local(migrated_url, store)

        assert await _cursor(migrated_url, "local") == "local/"

    asyncio.run(go())


@pytest.mark.parametrize("state", [SystemState.READY, SystemState.PROVISIONING])
def test_live_system_fence_retains_every_version(migrated_url: str, state: SystemState) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=state)
        key = _local_key(system_id)
        store = _Store({key: _history(key, 2)})

        assert await _run_local(migrated_url, store) == 0
        assert store.deleted == []

    asyncio.run(go())


def test_missing_system_fence_retains_every_version(migrated_url: str) -> None:
    key = _local_key(uuid4())
    store = _Store({key: _history(key, 2)})

    assert asyncio.run(_run_local(migrated_url, store)) == 0
    assert store.deleted == []


def test_exact_artifact_row_collision_retains_every_version(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
            key = _local_key(system_id)
            await seed.execute(
                "INSERT INTO artifacts "
                "(owner_kind, owner_id, object_key, etag, sensitivity, retention_class) "
                "VALUES ('systems', %s, %s, 'etag', 'redacted', 'console')",
                (system_id, key),
            )
        store = _Store({key: _history(key, 2)})

        assert await _run_local(migrated_url, store) == 0
        assert store.deleted == []

    asyncio.run(go())


def test_contended_system_lock_retains_every_version(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        key = _local_key(system_id)
        store = _Store({key: _history(key, 2)})
        holder = await psycopg.AsyncConnection.connect(migrated_url)
        try:
            async with (
                holder.transaction(),
                advisory_xact_lock(holder, LockScope.SYSTEM, system_id),
            ):
                assert await _run_local(migrated_url, store) == 0
        finally:
            await holder.close()
        assert store.deleted == []

    asyncio.run(go())


def test_delete_runs_after_system_transaction_and_lock_release(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        key = _local_key(system_id)
        store = _Store({key: [_version(key)]})
        lock_key = _lock_key(LockScope.SYSTEM, system_id)

        def contend_for_system_lock() -> None:
            with psycopg.connect(migrated_url) as observer:
                row = observer.execute(
                    "SELECT pg_try_advisory_xact_lock(%s)",
                    (lock_key,),
                ).fetchone()
            assert row == (True,)

        store.before_delete = contend_for_system_lock
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            assert await sweep.sweep_local_system_object_versions(conn, store) == 1

    asyncio.run(go())


def test_remote_sweep_skips_without_hosting_gate(migrated_url: str) -> None:
    key = _remote_key(uuid4())
    store = _Store({key: [_version(key)]})

    assert asyncio.run(_run_remote(migrated_url, store, None)) == 0
    assert store.page_calls == []


def test_remote_sweep_skips_nonleader_before_listing(migrated_url: str) -> None:
    store = _Store({})
    gate = _Gate(False, CollectorRegistry())

    assert asyncio.run(_run_remote(migrated_url, store, gate)) == 0
    assert store.page_calls == []


def test_remote_sweep_rechecks_leadership_after_capture(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        gate = _Gate(True, CollectorRegistry())

        class LoseLeadershipStore(_Store):
            def capture_exact_versions(self, key: str, limit: int) -> VersionBatch:
                gate.leader = False
                return super().capture_exact_versions(key, limit)

        key = _remote_key(system_id)
        store = LoseLeadershipStore({key: [_version(key)]})

        assert await _run_remote(migrated_url, store, gate) == 0
        assert store.deleted == []

    asyncio.run(go())


def test_remote_sweep_rechecks_leadership_after_database_fence(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        gate = _Gate(True, CollectorRegistry())
        key = _remote_key(system_id)
        store = _Store({key: [_version(key)]})
        original = sweep._system_key_is_retired

        async def lose_leadership_during_fence(
            conn: psycopg.AsyncConnection, fenced_system_id: UUID, fenced_key: str
        ) -> bool:
            retired = await original(conn, fenced_system_id, fenced_key)
            gate.leader = False
            return retired

        monkeypatch.setattr(sweep, "_system_key_is_retired", lose_leadership_during_fence)

        assert await _run_remote(migrated_url, store, gate) == 0
        assert store.deleted == []

    asyncio.run(go())


def test_remote_sweep_rechecks_registry_after_database_fence(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Collector:
        def __init__(self, system_id: UUID) -> None:
            self.system_id = system_id

        def pump_once(self) -> bool:
            return True

        def finalize(self) -> None:
            return None

        def close(self) -> None:
            return None

    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        gate = _Gate(True, CollectorRegistry())
        key = _remote_key(system_id)
        store = _Store({key: [_version(key)]})
        original = sweep._system_key_is_retired

        async def register_during_fence(
            conn: psycopg.AsyncConnection, fenced_system_id: UUID, fenced_key: str
        ) -> bool:
            retired = await original(conn, fenced_system_id, fenced_key)
            gate.registry.add(Collector(system_id))
            return retired

        monkeypatch.setattr(sweep, "_system_key_is_retired", register_during_fence)

        assert await _run_remote(migrated_url, store, gate) == 0
        assert store.deleted == []

    asyncio.run(go())


def test_registered_remote_collector_fences_internal_parts(migrated_url: str) -> None:
    class Collector:
        def __init__(self, system_id: UUID) -> None:
            self.system_id = system_id

        def pump_once(self) -> bool:
            return True

        def finalize(self) -> None:
            return None

        def close(self) -> None:
            return None

    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.FAILED)
        registry = CollectorRegistry()
        registry.add(Collector(system_id))
        gate = _Gate(True, registry)
        key = _remote_key(system_id)
        store = _Store({key: [_version(key)]})

        assert await _run_remote(migrated_url, store, gate) == 0
        assert store.deleted == []

    asyncio.run(go())


def test_leader_deletes_remote_parts_only_after_registry_absence(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        gate = _Gate(True, CollectorRegistry())
        key = _remote_key(system_id, 7)
        store = _Store({key: [_version(key)]})

        assert await _run_remote(migrated_url, store, gate) == 1
        assert store.deleted == [(key, "v1")]

    asyncio.run(go())


def test_cancelled_sweep_holds_reservation_until_remote_delete_finishes(
    migrated_url: str,
) -> None:
    class LeaderLock:
        def __init__(self) -> None:
            self.held = False

        async def try_acquire(self) -> bool:
            self.held = True
            return True

        async def is_held(self) -> bool:
            return self.held

        async def release(self) -> None:
            self.held = False

    class RunningSystems:
        def __init__(self) -> None:
            self.systems: set[UUID] = set()

        async def list_running(self) -> set[UUID]:
            return set(self.systems)

    @dataclass
    class LoopGate:
        loop: ConsoleHostingLoop
        registry: CollectorRegistry

        @property
        def is_leader(self) -> bool:
            return self.loop.is_leader

        def reserve_system_object_cleanup(
            self, system_id: UUID
        ) -> AbstractAsyncContextManager[bool]:
            return self.loop.reserve_system_object_cleanup(system_id)

    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        leader_lock = LeaderLock()
        running = RunningSystems()
        registry = CollectorRegistry()
        loop = ConsoleHostingLoop(
            leader_lock=leader_lock,
            running_systems=running,
            collector_factory=_FinalizingCollector,
            registry=registry,
        )
        await loop.tick()
        gate = LoopGate(loop, registry)
        key = _remote_key(system_id)
        store = _Store({key: [_version(key)]})
        delete_started = threading.Event()
        release_delete = threading.Event()

        def block_delete() -> None:
            delete_started.set()
            assert release_delete.wait(timeout=5)

        store.before_delete = block_delete
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            delete_task = asyncio.create_task(
                run_repair(
                    pool,
                    lambda conn: sweep.sweep_remote_system_object_versions(conn, store, gate),
                )
            )
            assert await asyncio.to_thread(delete_started.wait, 5)
            delete_task.cancel()
            await asyncio.sleep(0)
            cancellation_waited_for_delete = not delete_task.done()
            running.systems.add(system_id)
            transition = asyncio.create_task(loop.tick())
            await asyncio.sleep(0)
            transition_waited = not transition.done()
            collector_absent_during_delete = not registry.has(system_id)
            release_delete.set()
            with pytest.raises(asyncio.CancelledError):
                await delete_task
            await transition

        assert cancellation_waited_for_delete
        assert transition_waited
        assert collector_absent_during_delete
        assert registry.has(system_id)
        assert store.deleted == [(key, "v1")]

    asyncio.run(go())


class _FinalizingCollector:
    def __init__(self, system_id: UUID, *, fail: bool = False) -> None:
        self.system_id = system_id
        self.fail = fail

    def pump_once(self) -> bool:
        return True

    def finalize(self) -> None:
        if self.fail:
            raise RuntimeError("finalize failed")

    def close(self) -> None:
        return None


def test_successful_collector_reap_precedes_remote_part_deletion(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        registry = CollectorRegistry()
        registry.add(_FinalizingCollector(system_id))
        gate = _Gate(True, registry)
        key = _remote_key(system_id)
        store = _Store({key: [_version(key)]})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, lambda conn: reap_console_collectors(conn, registry)) == 1
            assert (
                await run_repair(
                    pool,
                    lambda conn: sweep.sweep_remote_system_object_versions(conn, store, gate),
                )
                == 1
            )

        assert not registry.has(system_id)
        assert store.deleted == [(key, "v1")]

    asyncio.run(go())


def test_finalization_failure_retains_registry_and_remote_parts(migrated_url: str) -> None:
    async def go() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.FAILED)
        registry = CollectorRegistry()
        registry.add(_FinalizingCollector(system_id, fail=True))
        gate = _Gate(True, registry)
        key = _remote_key(system_id)
        store = _Store({key: [_version(key)]})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(RuntimeError, match="finalize failed"):
                await run_repair(pool, lambda conn: reap_console_collectors(conn, registry))
            assert (
                await run_repair(
                    pool,
                    lambda conn: sweep.sweep_remote_system_object_versions(conn, store, gate),
                )
                == 0
            )

        assert registry.has(system_id)
        assert store.deleted == []

    asyncio.run(go())
