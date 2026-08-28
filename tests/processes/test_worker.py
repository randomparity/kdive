"""Worker process assembly tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.worker import WorkerConfig
from kdive.observability.facade import Telemetry
from kdive.processes.worker import run_worker
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.worker_lifecycle.authority_store import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    DockerWorkerIncarnation,
    WorkerIncarnation,
)


def _telemetry() -> Telemetry:
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


def test_run_worker_wires_runtime_registry_probe_and_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    secret_registry = SecretRegistry()
    handler_registry = object()

    def dispose_spool(operation_id: object) -> bool:
        return True

    handler_assembly = SimpleNamespace(
        resolver=object(),
        object_stores=SimpleNamespace(store=None),
        capture_supervisor=SimpleNamespace(dispose_recovery_spool=dispose_spool),
    )

    class _ConnectionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    class _Pool:
        def connection(self) -> _ConnectionContext:
            return _ConnectionContext()

    pool = _Pool()
    probe = object()
    stop = asyncio.Event()
    incarnation_credential = SecretStr("authority-delivered-credential")

    class _Store:
        def validate_conditional_create(self) -> None:
            events.append("store-admission")

    store_instance = _Store()
    handler_assembly.object_stores.store = store_instance

    monkeypatch.setattr("kdive.processes.worker.create_pool", lambda **kw: pool)
    monkeypatch.setattr("kdive.processes.worker.worker_incarnation_id", lambda pid: "docker:nonce")
    monkeypatch.setattr(
        "kdive.processes.worker.worker_incarnation_credential", lambda: incarnation_credential
    )
    monkeypatch.setattr("kdive.processes.worker.install_stop", lambda: stop)
    monkeypatch.setattr("kdive.health.processes.server.build_postgres_ping", lambda value: value)
    monkeypatch.setattr(
        "kdive.health.processes.worker.build_worker_probe",
        lambda **kw: {
            "postgres_ping": kw["postgres_ping"],
            "store": kw["object_store_factory"],
            "capture_manifest_verifier": kw["capture_manifest_verifier"],
        },
    )
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: store_instance)
    monkeypatch.setattr(
        "kdive.jobs.assembly.build_handler_registry",
        lambda assembly: handler_registry if assembly is handler_assembly else None,
    )
    monkeypatch.setattr(
        "kdive.jobs.assembly.build_production_worker_handler_assembly",
        lambda **kw: handler_assembly,
    )

    async def recover(
        recovery_pool: object,
        resolver: object,
        store: object,
        recovery_dispose_spool: object,
        host_identity: str,
        credential: SecretStr,
    ) -> object:
        assert recovery_pool is pool
        assert resolver is handler_assembly.resolver
        assert store is store_instance
        assert recovery_dispose_spool is dispose_spool
        assert host_identity == "a" * 64
        assert credential is incarnation_credential
        events.append("recover")
        return SimpleNamespace(pending=0)

    monkeypatch.setattr(
        "kdive.jobs.capture_operations.recovery.recover_capture_operations", recover
    )

    class _Worker:
        def __init__(
            self,
            worker_pool: object,
            registry: object,
            *,
            worker_id: str,
            incarnation_credential: SecretStr,
            secret_registry: SecretRegistry,
            config: WorkerConfig,
        ) -> None:
            assert worker_pool is pool
            assert registry is handler_registry
            assert secret_registry is secret_registry_arg
            assert incarnation_credential is incarnation_credential_arg
            assert ":" in worker_id
            assert config.heartbeat == "heartbeat"
            assert config.readiness is not None
            assert config.telemetry is not None
            # Not merely present: `WorkerTelemetry.disabled()` is a non-None inert
            # stand-in, so wiring one would satisfy the check above (#1695).
            assert config.telemetry.enabled
            events.append("init")

        async def run(self, worker_stop: asyncio.Event) -> None:
            assert worker_stop is stop
            events.append("run")

    secret_registry_arg = secret_registry
    incarnation_credential_arg = incarnation_credential
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
        assert callable(built_probe["capture_manifest_verifier"])
        store = cast(Callable[[], _Store], built_probe["store"])
        assert store() is store_instance

        async def authenticate(conn: object, credential: SecretStr) -> WorkerIncarnation:
            assert credential is incarnation_credential
            events.append("authenticate")
            return DockerWorkerIncarnation(
                incarnation="docker:nonce",
                authority_kind="docker",
                authority_binding={"container_id": "a" * 64},
                fence_protocol=CURRENT_WORKER_FENCE_PROTOCOL,
            )

        monkeypatch.setattr("kdive.processes.worker.authenticate_worker_incarnation", authenticate)
        await body(pool, "heartbeat", probe)

    monkeypatch.setattr("kdive.processes.worker.run_process_runtime", _runtime)

    asyncio.run(run_worker(secret_registry, _telemetry()))

    assert events == ["authenticate", "store-admission", "recover", "init", "run"]
    assert secret_registry.snapshot() == frozenset({"authority-delivered-credential"})


def test_run_worker_store_admission_failure_prevents_recovery_and_job_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    secret_registry = SecretRegistry()
    credential = SecretStr("authority-delivered-credential")

    class _ConnectionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    class _Pool:
        def connection(self) -> _ConnectionContext:
            return _ConnectionContext()

    class _RejectedStore:
        def validate_conditional_create(self) -> None:
            events.append("store-admission")
            raise CategorizedError(
                "configure a conforming object store and retry worker startup",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )

    pool = _Pool()
    monkeypatch.setattr("kdive.processes.worker.create_pool", lambda **kw: pool)
    monkeypatch.setattr("kdive.processes.worker.worker_incarnation_id", lambda pid: "docker:nonce")
    monkeypatch.setattr("kdive.processes.worker.worker_incarnation_credential", lambda: credential)
    monkeypatch.setattr("kdive.processes.worker.install_stop", asyncio.Event)
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: _RejectedStore())

    async def authenticate(conn: object, supplied: SecretStr) -> WorkerIncarnation:
        events.append("authenticate")
        return DockerWorkerIncarnation(
            incarnation="docker:nonce",
            authority_kind="docker",
            authority_binding={"container_id": "a" * 64},
            fence_protocol=CURRENT_WORKER_FENCE_PROTOCOL,
        )

    async def recover(*args: object) -> object:
        events.append("recover")
        return SimpleNamespace(pending=0)

    monkeypatch.setattr("kdive.processes.worker.authenticate_worker_incarnation", authenticate)
    monkeypatch.setattr(
        "kdive.jobs.capture_operations.recovery.recover_capture_operations", recover
    )
    monkeypatch.setattr(
        "kdive.jobs.worker.Worker", lambda *args, **kwargs: events.append("worker-init")
    )

    async def _runtime(**kwargs: object) -> None:
        body = cast(Callable[[object, object, object], Awaitable[None]], kwargs["body"])
        await body(pool, "heartbeat", object())

    monkeypatch.setattr("kdive.processes.worker.run_process_runtime", _runtime)

    with pytest.raises(CategorizedError, match="configure a conforming object store"):
        asyncio.run(run_worker(secret_registry, _telemetry()))

    assert events == ["authenticate", "store-admission"]


def test_run_worker_refuses_a_credential_bound_to_another_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    secret_registry = SecretRegistry()
    credential = SecretStr("wrong-holder-credential")

    class _ConnectionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    class _Pool:
        def connection(self) -> _ConnectionContext:
            return _ConnectionContext()

    pool = _Pool()
    monkeypatch.setattr("kdive.processes.worker.create_pool", lambda **kw: pool)
    monkeypatch.setattr(
        "kdive.processes.worker.worker_incarnation_id", lambda pid: "docker:configured"
    )
    monkeypatch.setattr("kdive.processes.worker.worker_incarnation_credential", lambda: credential)
    monkeypatch.setattr("kdive.processes.worker.install_stop", asyncio.Event)

    async def authenticate(conn: object, supplied: SecretStr) -> WorkerIncarnation:
        return DockerWorkerIncarnation(
            incarnation="docker:other",
            authority_kind="docker",
            authority_binding={"container_id": "b" * 64},
            fence_protocol=CURRENT_WORKER_FENCE_PROTOCOL,
        )

    monkeypatch.setattr("kdive.processes.worker.authenticate_worker_incarnation", authenticate)
    monkeypatch.setattr("kdive.jobs.worker.Worker", lambda *args, **kwargs: events.append("init"))

    async def _runtime(**kwargs: object) -> None:
        body = cast(Callable[[object, object, object], Awaitable[None]], kwargs["body"])
        await body(pool, "heartbeat", object())

    monkeypatch.setattr("kdive.processes.worker.run_process_runtime", _runtime)

    with pytest.raises(RuntimeError, match="does not match configured runtime identity"):
        asyncio.run(run_worker(secret_registry, _telemetry()))
    assert events == []
