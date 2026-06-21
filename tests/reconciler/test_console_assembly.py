"""Assembly tests for reconciler-owned remote console hosting."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra import console_hosting
from kdive.providers.remote_libvirt import composition as remote_composition
from kdive.security.secrets.secret_registry import SecretRegistry


class _FakeLeaderConn:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakePool:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True


class _FakeRunningSystems:
    async def list_running(self) -> set[UUID]:
        return set()


class _FakeCollector:
    def __init__(self, system_id: UUID) -> None:
        self.system_id = system_id
        self.closed = False
        self.finalized = False
        self.pump_calls = 0

    def pump_once(self) -> bool:
        self.pump_calls += 1
        return True

    def finalize(self) -> None:
        self.finalized = True

    def close(self) -> None:
        self.closed = True


class _RecordingPumpRunner:
    def __init__(self) -> None:
        self.started: list[UUID] = []
        self.cancelled: list[UUID] = []
        self.cancel_all_calls = 0

    def start(self, collector: object) -> None:
        self.started.append(collector.system_id)  # ty: ignore[unresolved-attribute]

    def cancel(self, system_id: UUID) -> None:
        self.cancelled.append(system_id)

    def cancel_all(self) -> None:
        self.cancel_all_calls += 1


class _ListRunningSystems:
    def __init__(self, ids: set[UUID]) -> None:
        self._ids = ids

    async def list_running(self) -> set[UUID]:
        return set(self._ids)


class _FakeLeaderLock:
    def __init__(self, *, acquire: bool = True, held: bool = True) -> None:
        self._acquire = acquire
        self._held = held
        self.released = False

    async def try_acquire(self) -> bool:
        return self._acquire

    async def is_held(self) -> bool:
        return self._held

    async def release(self) -> None:
        self.released = True


_SYSTEM_ID = UUID("11111111-1111-1111-1111-111111111111")


def _make_loop(
    *,
    leader_lock: _FakeLeaderLock,
    running: set[UUID],
    collectors: dict[UUID, _FakeCollector],
    registry: console_hosting.CollectorRegistry,
    pump_runner: _RecordingPumpRunner | None = None,
) -> console_hosting.ConsoleHostingLoop:
    def _factory(system_id: UUID, /) -> _FakeCollector:
        return collectors[system_id]

    return console_hosting.ConsoleHostingLoop(
        leader_lock=leader_lock,  # ty: ignore[invalid-argument-type]
        running_systems=_ListRunningSystems(running),  # ty: ignore[invalid-argument-type]
        collector_factory=_factory,  # ty: ignore[invalid-argument-type]
        registry=registry,
        pump_runner=pump_runner,  # ty: ignore[invalid-argument-type]
    )


def test_collector_registry_tracks_added_collector() -> None:
    registry = console_hosting.CollectorRegistry()
    collector = _FakeCollector(_SYSTEM_ID)

    registry.add(collector)  # ty: ignore[invalid-argument-type]

    assert registry.has(_SYSTEM_ID) is True
    assert registry.get(_SYSTEM_ID) is collector
    assert registry.system_ids() == {_SYSTEM_ID}


def test_collector_registry_drop_cancels_pump_and_closes() -> None:
    pump_runner = _RecordingPumpRunner()
    registry = console_hosting.CollectorRegistry(pump_runner)  # ty: ignore[invalid-argument-type]
    collector = _FakeCollector(_SYSTEM_ID)
    registry.add(collector)  # ty: ignore[invalid-argument-type]

    registry.drop(_SYSTEM_ID)

    assert pump_runner.cancelled == [_SYSTEM_ID]
    assert collector.closed is True
    assert registry.has(_SYSTEM_ID) is False


def test_console_hosting_loop_starts_not_leader() -> None:
    loop = _make_loop(
        leader_lock=_FakeLeaderLock(),
        running=set(),
        collectors={},
        registry=console_hosting.CollectorRegistry(),
    )

    assert loop.is_leader is False


def test_console_hosting_loop_tick_acquires_leadership_and_opens_collectors() -> None:
    async def _run() -> None:
        lock = _FakeLeaderLock(acquire=True, held=True)
        collector = _FakeCollector(_SYSTEM_ID)
        pump_runner = _RecordingPumpRunner()
        registry = console_hosting.CollectorRegistry(pump_runner)  # ty: ignore[invalid-argument-type]
        loop = _make_loop(
            leader_lock=lock,
            running={_SYSTEM_ID},
            collectors={_SYSTEM_ID: collector},
            registry=registry,
            pump_runner=pump_runner,
        )

        await loop.tick()

        assert loop.is_leader is True
        assert registry.has(_SYSTEM_ID) is True
        assert pump_runner.started == [_SYSTEM_ID]

    asyncio.run(_run())


def test_console_hosting_loop_pumps_directly_without_pump_runner() -> None:
    async def _run() -> None:
        collector = _FakeCollector(_SYSTEM_ID)
        registry = console_hosting.CollectorRegistry()
        loop = _make_loop(
            leader_lock=_FakeLeaderLock(),
            running={_SYSTEM_ID},
            collectors={_SYSTEM_ID: collector},
            registry=registry,
            pump_runner=None,
        )

        await loop.tick()

        assert registry.has(_SYSTEM_ID) is True
        assert collector.pump_calls == 1

    asyncio.run(_run())


def test_console_hosting_loop_stop_releases_lock_when_leader() -> None:
    async def _run() -> None:
        lock = _FakeLeaderLock(acquire=True, held=True)
        pump_runner = _RecordingPumpRunner()
        registry = console_hosting.CollectorRegistry(pump_runner)  # ty: ignore[invalid-argument-type]
        loop = _make_loop(
            leader_lock=lock,
            running=set(),
            collectors={},
            registry=registry,
            pump_runner=pump_runner,
        )
        await loop.tick()
        assert loop.is_leader is True

        await loop.stop()

        assert lock.released is True
        assert loop.is_leader is False
        assert pump_runner.cancel_all_calls >= 1

    asyncio.run(_run())


def test_asyncio_pump_runner_tracks_and_cancels_task() -> None:
    async def _run() -> None:
        runner = console_hosting.AsyncioPumpRunner()
        collector = _FakeCollector(_SYSTEM_ID)

        runner.start(collector)  # ty: ignore[invalid-argument-type]
        first_task = runner._tasks[_SYSTEM_ID]

        # Starting the same collector again is a no-op (dedup keyed on _tasks):
        # exactly one task remains and it is the original one.
        runner.start(collector)  # ty: ignore[invalid-argument-type]
        assert len(runner._tasks) == 1
        assert runner._tasks[_SYSTEM_ID] is first_task

        # The running task actually pumps the collector off-loop.
        for _ in range(100):
            await asyncio.sleep(0)
            if collector.pump_calls > 0:
                break
        assert collector.pump_calls > 0
        pumped_before_cancel = collector.pump_calls

        # cancel() removes the entry and actually cancels the task. The bounded
        # wait_for pins that cancel() truly cancels: if it did not, the task
        # would pump forever and the await would never settle.
        runner.cancel(_SYSTEM_ID)
        assert _SYSTEM_ID not in runner._tasks
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first_task, timeout=2.0)
        assert first_task.cancelled()

        # Pumping has stopped: once the cancelled task is awaited, no further
        # pumps are dispatched (at most one in-flight off-loop call may settle).
        settled = collector.pump_calls
        for _ in range(20):
            await asyncio.sleep(0)
        assert collector.pump_calls == settled
        assert settled >= pumped_before_cancel

    asyncio.run(_run())


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    async def _spin() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_spin(), timeout=timeout)


def test_asyncio_pump_runner_throttles_idle_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        # An idle collector (pump_once -> False) must back off between pumps, not busy-loop.
        # Pin a measurable backoff and run for a few backoff windows: the pump count stays
        # small (throttled) yet nonzero (the loop keeps running). This kills both the
        # `if not got` -> `if got` inversion (which would make an idle collector spin with no
        # sleep, exploding the count) and the loop/sleep-removal mutants (count would be zero
        # or unbounded).
        backoff = 0.05
        monkeypatch.setattr(console_hosting, "_IDLE_PUMP_BACKOFF_SECONDS", backoff)
        runner = console_hosting.AsyncioPumpRunner()
        idle = _FakeCollector(_SYSTEM_ID)

        def _idle_pump() -> bool:
            idle.pump_calls += 1
            return False  # "no data": forces the idle-backoff branch every iteration

        idle.pump_once = _idle_pump  # type: ignore[method-assign]

        runner.start(idle)  # ty: ignore[invalid-argument-type]
        task = runner._tasks[_SYSTEM_ID]
        try:
            await _wait_until(lambda: idle.pump_calls >= 1)
            await asyncio.sleep(backoff * 6)
            # ~6 backoff windows elapsed: a throttled loop pumps a handful of times. A
            # busy-loop (no sleep on idle) would rack up hundreds in the same wall time.
            assert 1 <= idle.pump_calls <= 30
            assert task.done() is False
        finally:
            runner.cancel_all()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(_run())


def test_asyncio_pump_runner_isolates_pump_exception_and_continues() -> None:
    async def _run() -> None:
        # A pump_once that raises once must not kill the task: the except branch logs, backs
        # off, and continues. A mutant dropping the continue/sleep or the handler would let the
        # exception escape and the task would finish with an error instead of pumping again.
        runner = console_hosting.AsyncioPumpRunner()
        collector = _FakeCollector(_SYSTEM_ID)

        def _pump() -> bool:
            collector.pump_calls += 1
            if collector.pump_calls == 1:
                raise RuntimeError("transient pump failure")
            return True

        collector.pump_once = _pump  # type: ignore[method-assign]

        runner.start(collector)  # ty: ignore[invalid-argument-type]
        task = runner._tasks[_SYSTEM_ID]
        try:
            # The first call raises; the loop must recover and pump again. The error path
            # backs off 1s, so allow a generous bound before the recovery pump lands.
            await _wait_until(lambda: collector.pump_calls >= 2, timeout=4.0)
            assert collector.pump_calls >= 2
            assert task.done() is False
        finally:
            runner.cancel_all()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(_run())


def test_console_hosting_loop_drops_streams_when_leadership_lost() -> None:
    async def _run() -> None:
        # Once leader, a tick that finds the lock no longer held must close every stream:
        # stop the pumps, drain the registry, and step down. A mutant inverting is_held or
        # skipping drop_all would leave orphaned collectors streaming without leadership.
        lock = _FakeLeaderLock(acquire=True, held=True)
        collector = _FakeCollector(_SYSTEM_ID)
        pump_runner = _RecordingPumpRunner()
        registry = console_hosting.CollectorRegistry(pump_runner)  # ty: ignore[invalid-argument-type]
        loop = _make_loop(
            leader_lock=lock,
            running={_SYSTEM_ID},
            collectors={_SYSTEM_ID: collector},
            registry=registry,
            pump_runner=pump_runner,
        )
        await loop.tick()
        assert loop.is_leader is True
        assert registry.has(_SYSTEM_ID) is True

        lock._held = False
        await loop.tick()

        assert loop.is_leader is False
        assert registry.has(_SYSTEM_ID) is False  # drop_all drained the registry
        assert pump_runner.cancel_all_calls >= 1
        assert lock.released is False  # leadership loss is not a clean release

    asyncio.run(_run())


def test_console_hosting_loop_fails_closed_when_lock_check_raises() -> None:
    async def _run() -> None:
        # If the lock-held check raises, the loop must fail closed: treat it as a loss and
        # step down. A mutant swallowing the error as "still held" would keep streaming after
        # leadership is no longer verifiable.
        lock = _FakeLeaderLock(acquire=True, held=True)
        collector = _FakeCollector(_SYSTEM_ID)
        pump_runner = _RecordingPumpRunner()
        registry = console_hosting.CollectorRegistry(pump_runner)  # ty: ignore[invalid-argument-type]
        loop = _make_loop(
            leader_lock=lock,
            running={_SYSTEM_ID},
            collectors={_SYSTEM_ID: collector},
            registry=registry,
            pump_runner=pump_runner,
        )
        await loop.tick()
        assert loop.is_leader is True

        async def _boom() -> bool:
            raise RuntimeError("lock backend unreachable")

        lock.is_held = _boom  # type: ignore[method-assign]
        await loop.tick()

        assert loop.is_leader is False
        assert registry.has(_SYSTEM_ID) is False

    asyncio.run(_run())


def test_asyncio_pump_runner_cancel_unknown_id_is_noop() -> None:
    async def _run() -> None:
        runner = console_hosting.AsyncioPumpRunner()
        tracked = _FakeCollector(_SYSTEM_ID)
        runner.start(tracked)  # ty: ignore[invalid-argument-type]
        try:
            other = UUID("22222222-2222-2222-2222-222222222222")

            # Cancelling an id that was never started must not raise and must
            # leave the tracked task untouched.
            runner.cancel(other)

            assert _SYSTEM_ID in runner._tasks
            assert runner._tasks[_SYSTEM_ID].cancelled() is False
        finally:
            runner.cancel_all()
            await asyncio.sleep(0)

    asyncio.run(_run())


def test_console_hosting_run_ticks_loop_then_stops() -> None:
    async def _run() -> None:
        stop = asyncio.Event()

        class _FakeLoop:
            def __init__(self) -> None:
                self.tick_calls = 0
                self.stopped = False

            async def tick(self) -> None:
                self.tick_calls += 1
                stop.set()

            async def stop(self) -> None:
                self.stopped = True

        fake_loop = _FakeLoop()
        hosting = console_hosting.ConsoleHosting(
            loop=fake_loop,  # ty: ignore[invalid-argument-type]
            registry=object(),  # ty: ignore[invalid-argument-type]
            leader_conn=_FakeLeaderConn(),  # ty: ignore[invalid-argument-type]
            host_pool=_FakePool(),  # ty: ignore[invalid-argument-type]
        )

        await hosting.run(stop)

        assert fake_loop.tick_calls == 1
        assert fake_loop.stopped is True

    asyncio.run(_run())


def test_build_console_hosting_returns_none_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        monkeypatch.setattr(remote_composition, "database_url", lambda: "postgresql://db/kdive")
        monkeypatch.setattr(remote_composition, "object_store_from_env", lambda: object())
        # No declared remote instance → bootstrap degrades to None (no console hosting).
        monkeypatch.setattr(remote_composition, "is_remote_libvirt_configured", lambda: False)

        hosting = await remote_composition.build_console_hosting(
            secret_registry=SecretRegistry(),
            running_systems_factory=lambda _pool: _FakeRunningSystems(),
        )
        assert hosting is None

    asyncio.run(_run())


def test_build_console_hosting_preserves_object_store_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        error = CategorizedError(
            "object store endpoint missing",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )

        def _raise_store() -> object:
            raise error

        monkeypatch.setattr(remote_composition, "is_remote_libvirt_configured", lambda: True)
        monkeypatch.setattr(remote_composition, "database_url", lambda: "postgresql://db/kdive")
        monkeypatch.setattr(remote_composition, "object_store_from_env", _raise_store)

        with pytest.raises(CategorizedError) as caught:
            await remote_composition.build_console_hosting(
                secret_registry=SecretRegistry(),
                running_systems_factory=lambda _pool: _FakeRunningSystems(),
            )

        assert caught.value is error

    asyncio.run(_run())


def test_build_console_hosting_opens_host_pool_and_returns_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        leader_conn = _FakeLeaderConn()
        host_pool = _FakePool()
        monkeypatch.setattr(remote_composition, "database_url", lambda: "postgresql://db/kdive")
        monkeypatch.setattr(remote_composition, "object_store_from_env", lambda: object())
        monkeypatch.setattr(remote_composition, "is_remote_libvirt_configured", lambda: True)
        monkeypatch.setattr(remote_composition, "secret_backend_from_env", lambda **_: object())
        monkeypatch.setattr(remote_composition, "create_pool", lambda **_: host_pool)

        async def _connect(conninfo: str, *, autocommit: bool) -> _FakeLeaderConn:
            assert conninfo == "postgresql://db/kdive"
            assert autocommit is True
            return leader_conn

        monkeypatch.setattr(
            remote_composition.psycopg.AsyncConnection, "connect", staticmethod(_connect)
        )

        hosting = await remote_composition.build_console_hosting(
            secret_registry=SecretRegistry(),
            running_systems_factory=lambda _pool: _FakeRunningSystems(),
        )

        assert hosting is not None
        assert hosting.registry is not None
        assert host_pool.opened is True

    asyncio.run(_run())


def test_start_console_hosting_none_returns_none() -> None:
    assert console_hosting.start_console_hosting(None, asyncio.Event()) is None


def test_console_hosting_close_closes_leader_and_host_pool() -> None:
    async def _run() -> None:
        leader_conn = _FakeLeaderConn()
        host_pool = _FakePool()
        hosting = console_hosting.ConsoleHosting(
            loop=object(),  # ty: ignore[invalid-argument-type]
            registry=object(),  # ty: ignore[invalid-argument-type]
            leader_conn=leader_conn,  # ty: ignore[invalid-argument-type]
            host_pool=host_pool,  # ty: ignore[invalid-argument-type]
        )

        await hosting.close()

        assert leader_conn.closed is True
        assert host_pool.closed is True

    asyncio.run(_run())
