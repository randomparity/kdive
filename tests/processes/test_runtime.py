"""Shared process runtime cleanup and background-task cancellation tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from kdive.processes import runtime


class FakePool:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def open(self) -> None:
        self.events.append("open")

    async def close(self) -> None:
        self.events.append("close")


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

        assert pool.events == ["open", "close"]
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

        assert pool.events == ["open", "close"]
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
        assert pool.events == ["open", "close"]
        assert registry.cleared

    asyncio.run(run())
