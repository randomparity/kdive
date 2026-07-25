"""Shared process runtime cleanup and background-task cancellation tests."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable

import pytest
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from kdive.config.core_settings import DATABASE_URL
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.processes import runtime

# A loopback port nothing listens on: connect is refused immediately, so the pool retries
# until its open budget expires rather than hanging on an unroutable address.
_UNREACHABLE_DB = "postgresql://kdive@127.0.0.1:1/kdive?connect_timeout=1"

# The warm-up the runtime must perform (ADR-0449): open, then take one connection under the
# startup budget. Spelled out so a revert to a bare `open()` reddens every fake-pool test
# here, not only the PG-backed one — which skips silently on a runner without Docker
# (`KDIVE_REQUIRE_DOCKER` is set in CI, not locally) and would leave the decision unguarded.
_WARM_OPEN = ["open", f"acquire(timeout={runtime.POOL_OPEN_TIMEOUT_SECONDS})"]

# Teardown overrides psycopg_pool's 5s default so a worker stuck mid-connect cannot add it
# to every time-to-diagnostic on the fail-fast path, nor to every crash-loop cycle.
_CLOSE = f"close(timeout={runtime.POOL_CLOSE_TIMEOUT_SECONDS})"


class FakePool:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def open(self, wait: bool = False, timeout: float = 30.0) -> None:
        # Record the arguments, not just the call: the runtime must not use `wait=True`
        # (which would wait for the full `min_size` — see the min_size test below).
        self.events.append("open" if not wait else f"open(wait=True, timeout={timeout})")

    @contextlib.asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncIterator[object]:
        self.events.append(f"acquire(timeout={timeout})")
        yield object()

    async def close(self, timeout: float = 5.0) -> None:
        self.events.append(f"close(timeout={timeout})")


class FakeSecretRegistry:
    def __init__(self) -> None:
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True


class FakeTelemetry:
    scrape_reader = object()


class AuxHarness:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.app: dict[str, object] | None = None

    def build_aux_app(self, *, heartbeat: object, probe: object, metric_reader: object) -> object:
        self.app = {"heartbeat": heartbeat, "probe": probe, "metric_reader": metric_reader}
        return self.app

    async def serve_aux(self, app: object, *, host: str, port: int) -> None:
        assert app is self.app
        assert host == "127.0.0.1"
        assert port == 18080
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _install_aux_harness(monkeypatch: pytest.MonkeyPatch, harness: AuxHarness) -> None:
    import kdive.health.aux_bind as aux_bind
    import kdive.health.aux_listener as aux_listener

    monkeypatch.setattr(aux_bind, "resolve_health_bind", lambda _process: ("127.0.0.1", 18080))
    monkeypatch.setattr(aux_listener, "build_aux_app", harness.build_aux_app)
    monkeypatch.setattr(aux_listener, "serve_aux", harness.serve_aux)


def _probe_builder(expected_pool: FakePool) -> tuple[object, Callable[[object], object]]:
    probe = object()

    def build(pool: object) -> object:
        assert pool is expected_pool
        return probe

    return probe, build


def test_runtime_body_receives_a_pool_with_a_live_connection(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body must never be handed a pool whose first connect is still pending (#1535).

    ``AsyncConnectionPool.open()`` defaults to ``wait=False``: it returns before any
    connection exists and leaves the first connect to a background task. The body it hands
    that pool to builds the MCP app, whose ``UsageTrackingMiddleware`` acquires with a
    1-second budget and swallows every failure (ADR-0148) — so a first connect that lands
    inside that budget drops a ``tool_invocation`` row with only a WARNING.

    Asserting on ``pool_available`` rather than replaying that race: it is deterministically
    0 after a cold open and >= 1 after a warm one (#1527's evidence), so this pins the
    condition itself instead of a timing window that only reproduces under load.
    """

    async def run() -> None:
        harness = AuxHarness()
        _install_aux_harness(monkeypatch, harness)
        pool = AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False)
        available: list[int] = []

        async def body(body_pool: object, _heartbeat: object, _probe: object) -> None:
            assert isinstance(body_pool, AsyncConnectionPool)
            available.append(body_pool.get_stats()["pool_available"])

        await runtime.run_process_runtime(
            process="worker",
            pool=pool,
            secret_registry=FakeSecretRegistry(),  # ty: ignore[invalid-argument-type]
            telemetry=FakeTelemetry(),  # ty: ignore[invalid-argument-type]
            heartbeat_stale_after=5.0,
            probe_builder=lambda _pool: object(),  # ty: ignore[invalid-argument-type]
            body=body,
        )

        assert available and available[0] >= 1, "runtime opened the pool cold — see #1535"

    asyncio.run(run())


def test_runtime_unreachable_database_fails_before_the_body_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database that never answers must end the process, not start a body it cannot serve.

    This is the cost side of ADR-0449's fail-fast choice, so pin all of it: the open raises
    rather than degrading, the body never runs, and the ``finally`` still clears the secret
    registry — which only holds because the open moved *inside* the ``try``.

    The failure is a ``CategorizedError``, not the bare ``PoolTimeout``, because that is the
    only thing ``__main__`` handles: an uncategorized raise exits on a stderr traceback with
    a generic code, invisible to a deployment scraping the ADR-0090 structured floor — for
    what this change makes a *routine* condition (the backend is not up yet).
    """

    async def run() -> None:
        harness = AuxHarness()
        _install_aux_harness(monkeypatch, harness)
        monkeypatch.setattr(runtime, "POOL_OPEN_TIMEOUT_SECONDS", 0.5)
        registry = FakeSecretRegistry()
        pool = AsyncConnectionPool(_UNREACHABLE_DB, min_size=1, max_size=2, open=False)
        started = False

        async def body(_pool: object, _heartbeat: object, _probe: object) -> None:
            nonlocal started
            started = True

        with pytest.raises(CategorizedError) as caught:
            await runtime.run_process_runtime(
                process="worker",
                pool=pool,
                secret_registry=registry,  # ty: ignore[invalid-argument-type]
                telemetry=FakeTelemetry(),  # ty: ignore[invalid-argument-type]
                heartbeat_stale_after=5.0,
                probe_builder=lambda _pool: object(),  # ty: ignore[invalid-argument-type]
                body=body,
            )

        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        # Names all three causes, not just reachability: psycopg re-raises `from None`, so
        # a wrong password is indistinguishable from an outage at this seam.
        assert "Postgres unreachable, or the credentials or database name are wrong" in str(
            caught.value
        )
        assert caught.value.details["variable"] == DATABASE_URL.name
        # The cause is chained, so the underlying timeout stays in the operator's traceback.
        assert isinstance(caught.value.__cause__, PoolTimeout)
        assert not started
        assert registry.cleared
        assert not harness.started.is_set(), "aux listener started despite an unopened pool"

    asyncio.run(run())


def test_runtime_starts_on_one_connection_not_the_pools_min_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warming must need one connection, not ``min_size`` (ADR-0449).

    ``open(wait=True)`` returns only once ``len(pool) >= min_size``. The worker's pool is
    ``min_size=2``, so waiting would make it hard-fail whenever Postgres is reachable but at
    ``max_connections`` — a state it previously started and drained jobs in, and one each
    crash-loop restart would worsen by re-attempting connections against a saturated server.
    The defect #1535 is about is a pool with *zero* connections; one removes it.

    This pool models exactly that partial availability: it can serve a connection but can
    never reach ``min_size``, so ``open(wait=True)`` would time out where an acquire does not.
    """

    class PartiallyAvailablePool(FakePool):
        async def open(self, wait: bool = False, timeout: float = 30.0) -> None:
            if wait:
                raise PoolTimeout(f"pool initialization incomplete after {timeout} sec")
            await super().open()

    async def run() -> None:
        harness = AuxHarness()
        _install_aux_harness(monkeypatch, harness)
        pool = PartiallyAvailablePool()
        started = False

        async def body(_pool: object, _heartbeat: object, _probe: object) -> None:
            nonlocal started
            started = True

        await runtime.run_process_runtime(
            process="worker",
            pool=pool,  # ty: ignore[invalid-argument-type]
            secret_registry=FakeSecretRegistry(),  # ty: ignore[invalid-argument-type]
            telemetry=FakeTelemetry(),  # ty: ignore[invalid-argument-type]
            heartbeat_stale_after=5.0,
            probe_builder=lambda _pool: object(),  # ty: ignore[invalid-argument-type]
            body=body,
        )

        assert started, "a pool short of min_size but able to serve one connection must start"
        assert pool.events == [*_WARM_OPEN, _CLOSE]

    asyncio.run(run())


def test_runtime_success_closes_clears_and_cancels_aux_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        pool = FakePool()
        registry = FakeSecretRegistry()
        harness = AuxHarness()
        probe, probe_builder = _probe_builder(pool)
        _install_aux_harness(monkeypatch, harness)

        async def body(body_pool: object, heartbeat: object, body_probe: object) -> None:
            assert body_pool is pool
            assert body_probe is probe
            assert harness.app is not None
            assert harness.app["heartbeat"] is heartbeat
            await harness.started.wait()

        await runtime.run_process_runtime(
            process="worker",
            pool=pool,  # ty: ignore[invalid-argument-type]
            secret_registry=registry,  # ty: ignore[invalid-argument-type]
            telemetry=FakeTelemetry(),  # ty: ignore[invalid-argument-type]
            heartbeat_stale_after=5.0,
            probe_builder=probe_builder,  # ty: ignore[invalid-argument-type]
            body=body,
        )

        assert pool.events == [*_WARM_OPEN, _CLOSE]
        assert registry.cleared
        assert harness.cancelled

    asyncio.run(run())


def test_runtime_exception_still_closes_clears_cancels_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        pool = FakePool()
        registry = FakeSecretRegistry()
        harness = AuxHarness()
        _, probe_builder = _probe_builder(pool)
        _install_aux_harness(monkeypatch, harness)

        async def body(_pool: object, _heartbeat: object, _probe: object) -> None:
            await harness.started.wait()
            raise RuntimeError("body failed")

        with pytest.raises(RuntimeError, match="body failed"):
            await runtime.run_process_runtime(
                process="worker",
                pool=pool,  # ty: ignore[invalid-argument-type]
                secret_registry=registry,  # ty: ignore[invalid-argument-type]
                telemetry=FakeTelemetry(),  # ty: ignore[invalid-argument-type]
                heartbeat_stale_after=5.0,
                probe_builder=probe_builder,  # ty: ignore[invalid-argument-type]
                body=body,
            )

        assert pool.events == [*_WARM_OPEN, _CLOSE]
        assert registry.cleared
        assert harness.cancelled

    asyncio.run(run())


def test_runtime_tick_heartbeat_starts_and_cancels_heartbeat_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        pool = FakePool()
        registry = FakeSecretRegistry()
        harness = AuxHarness()
        _, probe_builder = _probe_builder(pool)
        heartbeat_started = asyncio.Event()
        heartbeat_cancelled = False
        _install_aux_harness(monkeypatch, harness)

        async def fake_tick_heartbeat_loop(_heartbeat: object) -> None:
            nonlocal heartbeat_cancelled
            heartbeat_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                heartbeat_cancelled = True
                raise

        monkeypatch.setattr(runtime, "tick_heartbeat_loop", fake_tick_heartbeat_loop)

        async def body(_pool: object, _heartbeat: object, _probe: object) -> None:
            await harness.started.wait()
            await heartbeat_started.wait()

        await runtime.run_process_runtime(
            process="worker",
            pool=pool,  # ty: ignore[invalid-argument-type]
            secret_registry=registry,  # ty: ignore[invalid-argument-type]
            telemetry=FakeTelemetry(),  # ty: ignore[invalid-argument-type]
            heartbeat_stale_after=5.0,
            probe_builder=probe_builder,  # ty: ignore[invalid-argument-type]
            body=body,
            tick_heartbeat=True,
        )

        assert harness.cancelled
        assert heartbeat_cancelled
        assert pool.events == [*_WARM_OPEN, _CLOSE]
        assert registry.cleared

    asyncio.run(run())
