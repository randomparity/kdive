"""CLI wiring for `python -m kdive worker`: aux listener + readiness + telemetry (#267)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr

from kdive.jobs.worker import WorkerConfig
from kdive.observability.facade import Telemetry
from kdive.processes.runtime import (
    POOL_CLOSE_TIMEOUT_SECONDS,
    POOL_OPEN_TIMEOUT_SECONDS,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs.worker_incarnations import WorkerIncarnation


def _warm_open() -> list[str]:
    """The warm-up the runtime must perform: open, then take one connection (ADR-0449)."""
    return ["open", f"acquire(timeout={POOL_OPEN_TIMEOUT_SECONDS})"]


def _close() -> str:
    """Teardown, at the bounded timeout that keeps a stuck worker off the fail-fast path."""
    return f"close(timeout={POOL_CLOSE_TIMEOUT_SECONDS})"


def _fake_telemetry() -> Telemetry:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from tests.support.otel import tracer_provider

    reader = InMemoryMetricReader()
    return Telemetry(
        logger_provider=LoggerProvider(),
        tracer_provider=tracer_provider(),
        meter_provider=MeterProvider(metric_readers=[reader]),
        scrape_reader=reader,
    )


def test_run_worker_wires_heartbeat_readiness_and_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run_worker` opens a pool, builds a Worker with the aux stack, runs, closes."""
    from kdive import __main__
    from kdive.jobs import worker as worker_module

    events: list[str] = []

    class _FakePool:
        # Mirrors the `AsyncConnectionPool` surface the runtime uses. Record the warm-up,
        # not just the open: the runtime must establish one connection at start (ADR-0449),
        # and a fake that discards that lets a revert to the cold default pass.
        async def open(self, wait: bool = False, timeout: float = 30.0) -> None:
            events.append("open" if not wait else f"open(wait=True, timeout={timeout})")

        @contextlib.asynccontextmanager
        async def connection(self, timeout: float | None = None) -> AsyncIterator[object]:
            events.append(f"acquire(timeout={timeout})")
            yield object()

        async def close(self, timeout: float = 5.0) -> None:
            events.append(f"close(timeout={timeout})")

    monkeypatch.setattr("kdive.processes.worker.create_pool", lambda **kw: _FakePool())

    credential = SecretStr("authority-delivered-credential")

    async def _authenticate(*args: object) -> WorkerIncarnation:
        events.append("authenticate")
        return WorkerIncarnation(
            incarnation="docker:nonce",
            authority_kind="docker",
            authority_binding={"container_id": "a" * 64},
            fence_protocol=1,
        )

    monkeypatch.setattr("kdive.processes.worker.worker_incarnation_id", lambda pid: "docker:nonce")
    monkeypatch.setattr("kdive.processes.worker.worker_incarnation_credential", lambda: credential)
    monkeypatch.setattr("kdive.processes.worker.authenticate_worker_incarnation", _authenticate)
    monkeypatch.setattr("kdive.processes.worker.install_stop", lambda: asyncio.Event())
    monkeypatch.setattr("kdive.jobs.assembly.build_handler_registry", lambda **kw: object())
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: object())
    monkeypatch.setattr(
        "kdive.health.processes.server.build_postgres_ping", lambda pool: lambda: None
    )

    async def _no_serve(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr("kdive.health.aux_listener.serve_aux", _no_serve)

    constructed: dict[str, object] = {}

    def _fake_init(self: object, pool: object, registry: object, **kw: object) -> None:
        constructed.update(kw)

    async def _fake_run(self: object, stop: object) -> None:
        events.append("run")

    monkeypatch.setattr(worker_module.Worker, "__init__", _fake_init)
    monkeypatch.setattr(worker_module.Worker, "run", _fake_run)

    asyncio.run(__main__._run_worker(SecretRegistry(), _fake_telemetry()))

    assert events == [*_warm_open(), "acquire(timeout=None)", "authenticate", "run", _close()]
    config = constructed["config"]
    assert isinstance(config, WorkerConfig)
    assert config.heartbeat is not None
    assert config.readiness is not None
    assert config.telemetry is not None
    # Not merely present: `WorkerTelemetry.disabled()` is a non-None inert stand-in,
    # so wiring one would satisfy the check above (#1695).
    assert config.telemetry.enabled
