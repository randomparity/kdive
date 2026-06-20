"""Integration test: handler provider-kind tag flows into WorkerTelemetry (ADR-0191 F).

The contextvar tag is what connects a handler to the provider-op series; a handler that
forgets ``set_provider_kind`` silently emits nothing with green unit tests. This test drives
a real provider-backed handler through a fake resolver whose ``binding_for_system`` returns
a ``ProviderBinding(kind=ResourceKind.LOCAL_LIBVIRT, runtime=<fake>)`` inside a
``WorkerTelemetry.job_span`` backed by an ``InMemoryMetricReader``, then asserts
``kdive.provider.op.duration`` emits with ``provider="local-libvirt"``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle import System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.systems import teardown_handler
from kdive.jobs.provider_context import clear_provider_kind
from kdive.jobs.worker_telemetry import WorkerTelemetry
from kdive.providers.core.resolver import ProviderBinding, ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_SYSTEM_ID = uuid4()
_ALLOC_ID = uuid4()
_DOMAIN = f"kdive-{_SYSTEM_ID}"


class _FakeTeardown:
    def teardown(self, domain_name: str) -> None:
        pass


class _FakeRuntime:
    provisioner = _FakeTeardown()

    def for_resource(self, _name: str) -> _FakeRuntime:
        return self


class _FakeResolver:
    """Returns a LOCAL_LIBVIRT binding without touching the DB."""

    def __init__(self, runtime: _FakeRuntime) -> None:
        self._runtime = runtime

    async def binding_for_system(self, conn: Any, system_id: UUID) -> ProviderBinding:
        del conn
        return ProviderBinding(
            kind=ResourceKind.LOCAL_LIBVIRT,
            runtime=cast(ProviderRuntime, self._runtime),
        )

    async def runtime_for_system(self, conn: Any, system_id: UUID) -> _FakeRuntime:
        return cast(_FakeRuntime, (await self.binding_for_system(conn, system_id)).runtime)


def _make_job() -> Job:
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.TEARDOWN,
        payload={"system_id": str(_SYSTEM_ID)},
        state=JobState.RUNNING,
        max_attempts=1,
        authorizing={"principal": "u", "agent_session": "s", "project": "proj"},
        dedup_key=f"teardown:{_SYSTEM_ID}",
    )


def _make_system() -> System:
    return System(
        id=_SYSTEM_ID,
        created_at=_NOW,
        updated_at=_NOW,
        allocation_id=_ALLOC_ID,
        principal="u",
        project="proj",
        state=SystemState.TORN_DOWN,  # already torn down → handler skips state update
        provisioning_profile={},
        domain_name=_DOMAIN,
    )


def test_teardown_handler_tags_provider_kind_and_metric_is_emitted() -> None:
    """The teardown handler's set_provider_kind call is visible in the provider-op series.

    This proves the contextvar signal flows from handler → WorkerTelemetry._record:
    if the handler forgot ``set_provider_kind``, the assertion would fail.
    """
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    tracer = TracerProvider().get_tracer("test")
    telem = WorkerTelemetry(tracer=tracer, meter=meter)

    resolver = cast(ProviderResolver, _FakeResolver(_FakeRuntime()))
    system = _make_system()
    job = _make_job()

    # Fake a psycopg AsyncConnection sufficient for teardown_handler.
    # The handler wraps in conn.transaction() and advisory_xact_lock(conn, ...).
    # advisory_xact_lock calls conn.execute and checks conn.info.transaction_status.
    fake_conn = MagicMock()
    fake_conn.execute = AsyncMock()
    from psycopg.pq import TransactionStatus

    fake_conn.info.transaction_status = TransactionStatus.INTRANS
    # transaction() is used as an async context manager
    fake_cm = AsyncMock()
    fake_cm.__aenter__ = AsyncMock(return_value=None)
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    fake_conn.transaction.return_value = fake_cm

    async def _run() -> None:
        clear_provider_kind()
        from kdive.db.repositories import SYSTEMS

        with (
            patch.object(SYSTEMS, "get", new=AsyncMock(return_value=system)),
            telem.job_span("teardown") as span,
        ):
            await teardown_handler(fake_conn, job, resolver=resolver)
            span.set_outcome("ok")

    asyncio.run(_run())

    data = reader.get_metrics_data()
    assert data is not None
    points: list[Any] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == "kdive.provider.op.duration":
                    points.extend(metric.data.data_points)

    assert points, "kdive.provider.op.duration was not emitted — handler did not tag provider kind"
    assert points[0].attributes["provider"] == "local-libvirt"
    assert points[0].attributes["job_kind"] == "teardown"
