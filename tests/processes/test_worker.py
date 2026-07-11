"""Worker process assembly tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from kdive.jobs.worker import WorkerConfig
from kdive.observability.facade import Telemetry
from kdive.processes.worker import run_worker
from kdive.security.secrets.secret_registry import SecretRegistry


def _telemetry() -> Telemetry:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider

    reader = InMemoryMetricReader()
    return Telemetry(
        logger_provider=LoggerProvider(),
        tracer_provider=TracerProvider(),
        meter_provider=MeterProvider(metric_readers=[reader]),
        scrape_reader=reader,
    )


def test_run_worker_wires_runtime_registry_probe_and_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    secret_registry = SecretRegistry()
    handler_registry = object()
    pool = object()
    probe = object()
    stop = asyncio.Event()

    monkeypatch.setattr("kdive.processes.worker.create_pool", lambda **kw: pool)
    monkeypatch.setattr("kdive.processes.worker.install_stop", lambda: stop)
    monkeypatch.setattr("kdive.health.processes.server.build_postgres_ping", lambda value: value)
    monkeypatch.setattr(
        "kdive.health.processes.worker.build_worker_probe",
        lambda **kw: {"postgres_ping": kw["postgres_ping"], "store": kw["object_store_factory"]},
    )
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: "store")
    monkeypatch.setattr(
        "kdive.jobs.assembly.build_handler_registry",
        lambda **kw: handler_registry if kw["secret_registry"] is secret_registry else None,
    )

    class _Worker:
        def __init__(
            self,
            worker_pool: object,
            registry: object,
            *,
            worker_id: str,
            secret_registry: SecretRegistry,
            config: WorkerConfig,
        ) -> None:
            assert worker_pool is pool
            assert registry is handler_registry
            assert secret_registry is secret_registry_arg
            assert ":" in worker_id
            assert config.heartbeat == "heartbeat"
            assert config.readiness is not None
            assert config.telemetry is not None
            events.append("init")

        async def run(self, worker_stop: asyncio.Event) -> None:
            assert worker_stop is stop
            events.append("run")

    secret_registry_arg = secret_registry
    monkeypatch.setattr("kdive.jobs.worker.Worker", _Worker)

    async def _runtime(**kwargs: object) -> None:
        assert kwargs["process"] == "worker"
        assert kwargs["pool"] is pool
        assert kwargs["secret_registry"] is secret_registry
        assert kwargs["heartbeat_stale_after"] == 10.0
        probe_builder = cast(Callable[[object], dict[str, object]], kwargs["probe_builder"])
        body = cast(Callable[[object, object, object], Awaitable[None]], kwargs["body"])
        built_probe = probe_builder(pool)
        assert built_probe["postgres_ping"] is pool
        store = cast(Callable[[], str], built_probe["store"])
        assert store() == "store"
        await body(pool, "heartbeat", probe)

    monkeypatch.setattr("kdive.processes.worker.run_process_runtime", _runtime)

    asyncio.run(run_worker(secret_registry, _telemetry()))

    assert events == ["init", "run"]
