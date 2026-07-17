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
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.systems import teardown_handler
from kdive.jobs.provider_context import clear_provider_kind
from kdive.jobs.worker_telemetry import WorkerTelemetry
from kdive.providers.core.resolver import ProviderBinding, ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_SYSTEM_ID = uuid4()
_ALLOC_ID = uuid4()
# A stored domain name deliberately distinct from ``domain_name_for(_SYSTEM_ID)`` so tests
# can tell the stored-name branch from the derived-name fallback.
_DOMAIN = "kdive-custom-stored-name"
_DERIVED_DOMAIN = f"kdive-{_SYSTEM_ID}"


class _FakeTeardown:
    def __init__(self) -> None:
        self.teardown_calls: list[str] = []

    def teardown(self, domain_name: str) -> None:
        self.teardown_calls.append(domain_name)


class _FakeRuntime:
    def __init__(self) -> None:
        self.provisioner = _FakeTeardown()
        self.snapshot = None  # no snapshot port; teardown skips delete_all

    def for_resource(self, _name: str) -> _FakeRuntime:
        return self


class _FakeResolver:
    """Returns a LOCAL_LIBVIRT binding without touching the DB."""

    def __init__(self, runtime: _FakeRuntime) -> None:
        self._runtime = runtime
        self.binding_calls: list[tuple[Any, Any]] = []

    async def binding_for_system(self, conn: Any, system_id: UUID) -> ProviderBinding:
        self.binding_calls.append((conn, system_id))
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


def _make_system(*, domain_name: str | None = _DOMAIN) -> System:
    return System(
        id=_SYSTEM_ID,
        created_at=_NOW,
        updated_at=_NOW,
        allocation_id=_ALLOC_ID,
        principal="u",
        project="proj",
        state=SystemState.TORN_DOWN,  # already torn down → handler skips state update
        provisioning_profile={},
        domain_name=domain_name,
    )


def _make_fake_conn() -> MagicMock:
    """A psycopg AsyncConnection stub sufficient for teardown_handler.

    The handler wraps work in ``conn.transaction()`` and ``advisory_xact_lock(conn, ...)``;
    the lock calls ``conn.execute`` and reads ``conn.info.transaction_status``.
    """
    from psycopg.pq import TransactionStatus

    fake_conn = MagicMock()
    fake_conn.execute = AsyncMock()
    fake_conn.info.transaction_status = TransactionStatus.INTRANS
    fake_cm = AsyncMock()
    fake_cm.__aenter__ = AsyncMock(return_value=None)
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    fake_conn.transaction.return_value = fake_cm
    return fake_conn


def test_teardown_handler_tags_provider_kind_and_metric_is_emitted() -> None:
    """The teardown handler's set_provider_kind call is visible in the provider-op series.

    This proves the contextvar signal flows from handler → WorkerTelemetry._record:
    if the handler forgot ``set_provider_kind``, the assertion would fail.
    """
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    tracer = TracerProvider().get_tracer("test")
    telem = WorkerTelemetry(tracer=tracer, meter=meter)

    runtime = _FakeRuntime()
    fake_resolver = _FakeResolver(runtime)
    resolver = cast(ProviderResolver, fake_resolver)
    system = _make_system()
    job = _make_job()
    fake_conn = _make_fake_conn()
    get_mock = AsyncMock(return_value=system)

    result: list[str | None] = []

    async def _run() -> None:
        clear_provider_kind()
        from kdive.db.repositories import SYSTEMS

        with (
            patch.object(SYSTEMS, "get", new=get_mock),
            telem.job_span("teardown") as span,
        ):
            result.append(await teardown_handler(fake_conn, job, resolver=resolver))
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

    # The handler resolves the System by (conn, system_id) parsed from the payload.
    get_mock.assert_awaited_once_with(fake_conn, _SYSTEM_ID)
    # The advisory lock is acquired on (SYSTEM, system_id): the SELECT carries the lock key
    # derived from this system id, not some other key.
    from kdive.db.locks import LockScope, _lock_key

    expected_key = _lock_key(LockScope.SYSTEM, _SYSTEM_ID)
    lock_calls = [
        call
        for call in fake_conn.execute.await_args_list
        if call.args and call.args[0] == "SELECT pg_advisory_xact_lock(%s)"
    ]
    assert lock_calls, "advisory lock SELECT was never issued"
    assert lock_calls[0].args[1] == (expected_key,)
    # The binding is resolved with the same connection and system id.
    assert fake_resolver.binding_calls == [(fake_conn, _SYSTEM_ID)]
    # An already-torn-down System keeps its stored domain name (the ``or`` left operand),
    # which is passed verbatim to the provisioner teardown.
    assert runtime.provisioner.teardown_calls == [_DOMAIN]
    # The handler returns the system id as a string.
    assert result == [str(_SYSTEM_ID)]


def test_teardown_handler_falls_back_to_derived_domain_when_unnamed() -> None:
    """When the System has no stored domain_name, teardown uses ``domain_name_for``."""
    runtime = _FakeRuntime()
    resolver = cast(ProviderResolver, _FakeResolver(runtime))
    system = _make_system(domain_name=None)
    job = _make_job()
    fake_conn = _make_fake_conn()

    async def _run() -> str | None:
        clear_provider_kind()
        from kdive.db.repositories import SYSTEMS

        with patch.object(SYSTEMS, "get", new=AsyncMock(return_value=system)):
            return await teardown_handler(fake_conn, job, resolver=resolver)

    result = asyncio.run(_run())

    assert result == str(_SYSTEM_ID)
    assert runtime.provisioner.teardown_calls == [_DERIVED_DOMAIN]


def test_teardown_handler_returns_none_when_system_missing() -> None:
    """A missing System short-circuits to ``None`` and never touches the provider."""
    runtime = _FakeRuntime()
    fake_resolver = _FakeResolver(runtime)
    resolver = cast(ProviderResolver, fake_resolver)
    job = _make_job()
    fake_conn = _make_fake_conn()

    async def _run() -> str | None:
        clear_provider_kind()
        from kdive.db.repositories import SYSTEMS

        with patch.object(SYSTEMS, "get", new=AsyncMock(return_value=None)):
            return await teardown_handler(fake_conn, job, resolver=resolver)

    result = asyncio.run(_run())

    assert result is None
    assert fake_resolver.binding_calls == []
    assert runtime.provisioner.teardown_calls == []
