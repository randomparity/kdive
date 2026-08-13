"""Worker-authority supervision for gated capture operations (ADR-0558)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.capture_operations.launcher import GatedCaptureLauncher
from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult
from kdive.jobs.capture_operations.repository import CaptureOperation, CaptureOperationState
from kdive.jobs.capture_operations.supervisor import (
    LOCK_PROBE_INTERVAL_SECONDS,
    LOCK_PROBE_TIMEOUT_SECONDS,
    CaptureOperationSupervisor,
    CaptureSnapshot,
    RecoverySummary,
    recover_capture_operations,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.traffic import QuiescenceEvidence

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _job() -> Job:
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.CAPTURE_TRAFFIC,
        payload={},
        state=JobState.RUNNING,
        attempt=1,
        max_attempts=3,
        worker_id="worker-a",
        authorizing={"principal": "p", "agent_session": None, "project": "proj"},
        dedup_key="capture",
    )


def _request(job: Job) -> CaptureRequest:
    return CaptureRequest(
        job_id=job.id,
        provider_kind="local-libvirt",
        resource_id=uuid4(),
        system_id=uuid4(),
        domain_name="guest",
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=1,
    )


def _operation(job: Job, request: CaptureRequest, state: str = "launching") -> CaptureOperation:
    return CaptureOperation(
        id=uuid4(),
        job_id=job.id,
        job_attempt=job.attempt,
        worker_incarnation="worker-a",
        provider_kind=request.provider_kind,
        resource_id=request.resource_id,
        system_id=request.system_id,
        domain_name=request.domain_name,
        request_digest=request.digest,
        launch_token="a" * 64,
        host_instance="host-a",
        boot_id=None if state == "launching" else "boot-a",
        pid=None if state == "launching" else 123,
        start_ticks=None if state == "launching" else 456,
        state=cast(CaptureOperationState, state),
        exit_outcome=None,
        exit_code=None,
        process_absent=False,
        provider_quiescence={},
        recovered_by=None,
        created_at=_NOW,
        identity_recorded_at=None,
        running_at=None,
        cancel_requested_at=None,
        exited_at=None,
        updated_at=_NOW,
    )


class _Connection:
    def __init__(self, events: list[str], *, fail_probe: int | None = None) -> None:
        self.events = events
        self.fail_probe = fail_probe
        self.probes = 0

    async def execute(self, query: str, parameters: object = None) -> object:
        del parameters
        if query == "SELECT 1":
            self.probes += 1
            self.events.append("probe")
            if self.probes == self.fail_probe:
                raise RuntimeError("lock session lost")
        elif "pg_advisory_lock" in query:
            self.events.append("lock")
        elif "pg_advisory_unlock" in query:
            self.events.append("unlock")
        elif "statement_timeout" in query:
            self.events.append("timeout")
        return object()


class _StalledProbeConnection(_Connection):
    async def execute(self, query: str, parameters: object = None) -> object:
        if query == "SELECT 1":
            self.events.append("probe_stalled")
            await asyncio.Event().wait()
        return await super().execute(query, parameters)


class _Launched:
    def __init__(
        self,
        events: list[str],
        result: CaptureResult,
        *,
        wait_gate: asyncio.Event | None = None,
        cancel_result: bool = True,
    ) -> None:
        self.events = events
        self.result = result
        self.wait_gate = wait_gate
        self.cancel_result = cancel_result
        self.identity = SimpleNamespace(
            host_instance="host-a", boot_id="boot-a", pid=123, start_ticks=456
        )
        self.returncode = None
        self.attempt_dir = Path("/unused")

    def stage_configuration(self, configuration: bytes) -> None:
        assert configuration == b"configuration"
        self.events.append("stage")

    def release(self) -> None:
        self.events.append("release")

    async def wait_process(self) -> int:
        if self.wait_gate is not None:
            await self.wait_gate.wait()
        self.returncode = 0
        self.events.append("absent")
        return 0

    async def cancel(self) -> bool:
        self.events.append("cancel")
        return self.cancel_result

    def read_result(self) -> CaptureResult:
        self.events.append("result")
        return self.result

    def read_capture(self, maximum: int) -> bytes:
        assert maximum == 1_048_576
        return b"pcap"


class _Launcher:
    def __init__(self, launched: _Launched) -> None:
        self.launched = launched

    async def launch(self, request: CaptureRequest, operation: CaptureOperation) -> _Launched:
        assert operation.request_digest == request.digest
        return self.launched


def _typed_connection(events: list[str], *, fail_probe: int | None = None) -> AsyncConnection:
    return cast(AsyncConnection, _Connection(events, fail_probe=fail_probe))


def _typed_launcher(launched: _Launched) -> GatedCaptureLauncher:
    return cast(GatedCaptureLauncher, _Launcher(launched))


class _Quiescence:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def prove_absent(self, resource_id: UUID, domain_name: str, qom_id: str) -> QuiescenceEvidence:
        self.events.append("quiescence")
        return QuiescenceEvidence(
            provider_kind="local-libvirt",
            resource_id=resource_id,
            domain_name=domain_name,
            qom_id=qom_id,
            result="absent",
            ordering="fresh-qmp-connection",
        )


def _snapshot(request: CaptureRequest, events: list[str]) -> CaptureSnapshot:
    return CaptureSnapshot(
        provider_kind=request.provider_kind,
        resource_id=request.resource_id,
        system_id=request.system_id,
        domain_name=request.domain_name,
        resource_name="local",
        project="proj",
        write_remediation="fix permissions",
        configuration=lambda: b"configuration",
        quiescence=lambda _configuration: _Quiescence(events),
    )


def _patch_repository(
    monkeypatch: pytest.MonkeyPatch,
    operation: CaptureOperation,
    events: list[str],
) -> None:
    from kdive.jobs.capture_operations import supervisor

    async def create(*args: object, **kwargs: object) -> CaptureOperation:
        return operation

    async def identity(*args: object, **kwargs: object) -> CaptureOperation:
        events.append("identity")
        return _operation(_job_for(operation), _request_for(operation), "gated")

    async def running(*args: object, **kwargs: object) -> CaptureOperation:
        events.append("running")
        return _operation(_job_for(operation), _request_for(operation), "running")

    async def cancel(*args: object, **kwargs: object) -> CaptureOperation:
        events.append("cancel_requested")
        return _operation(_job_for(operation), _request_for(operation), "cancel_requested")

    async def acknowledge(*args: object, **kwargs: object) -> CaptureOperation:
        events.append("ack")
        return operation

    monkeypatch.setattr(supervisor, "create_launching", create)
    monkeypatch.setattr(supervisor, "record_identity", identity)
    monkeypatch.setattr(supervisor, "mark_running", running)
    monkeypatch.setattr(supervisor, "request_cancel", cancel)
    monkeypatch.setattr(supervisor, "acknowledge_exit", acknowledge)


def _job_for(operation: CaptureOperation) -> Job:
    job = _job()
    return job.model_copy(update={"id": operation.job_id, "attempt": operation.job_attempt})


def _request_for(operation: CaptureOperation) -> CaptureRequest:
    return CaptureRequest(
        job_id=operation.job_id,
        provider_kind=operation.provider_kind,
        resource_id=operation.resource_id,
        system_id=operation.system_id,
        domain_name=operation.domain_name,
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=1,
    )


def test_execute_stages_then_probes_releases_and_acks_before_reading_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    job = _job()
    request = _request(job)
    operation = _operation(job, request)
    launched = _Launched(
        events,
        CaptureResult(outcome="success", size_bytes=4, truncated=False),
    )
    _patch_repository(monkeypatch, operation, events)
    supervisor = CaptureOperationSupervisor(
        launcher=_typed_launcher(launched),
        credential=SecretStr("credential"),
    )

    result = asyncio.run(
        supervisor.execute(_typed_connection(events), job, _snapshot(request, events), request)
    )

    assert result == b"pcap"
    assert events.index("stage") < events.index("probe") < events.index("release")
    assert events.index("absent") < events.index("quiescence") < events.index("ack")
    assert events.index("ack") < events.index("result")
    assert LOCK_PROBE_INTERVAL_SECONDS == 0.25
    assert LOCK_PROBE_TIMEOUT_SECONDS == 1.0


def test_lock_loss_before_release_cancels_without_releasing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    job = _job()
    request = _request(job)
    operation = _operation(job, request)
    launched = _Launched(
        events,
        CaptureResult(outcome="success", size_bytes=4, truncated=False),
    )
    _patch_repository(monkeypatch, operation, events)
    supervisor = CaptureOperationSupervisor(
        launcher=_typed_launcher(launched),
        credential=SecretStr("credential"),
    )

    with pytest.raises(CategorizedError) as raised:
        asyncio.run(
            supervisor.execute(
                _typed_connection(events, fail_probe=1),
                job,
                _snapshot(request, events),
                request,
            )
        )

    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "release" not in events
    assert events.index("cancel_requested") < events.index("cancel")
    assert events.index("cancel") < events.index("quiescence") < events.index("ack")


def test_stalled_release_probe_uses_one_second_client_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    job = _job()
    request = _request(job)
    operation = _operation(job, request)
    launched = _Launched(
        events,
        CaptureResult(outcome="success", size_bytes=4, truncated=False),
    )
    _patch_repository(monkeypatch, operation, events)
    supervisor = CaptureOperationSupervisor(
        launcher=_typed_launcher(launched),
        credential=SecretStr("credential"),
    )

    async def run() -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(CategorizedError):
            await supervisor.execute(
                cast(AsyncConnection, _StalledProbeConnection(events)),
                job,
                _snapshot(request, events),
                request,
            )
        return loop.time() - started

    elapsed = asyncio.run(run())
    assert 0.9 <= elapsed < 2.0
    assert "release" not in events
    assert "cancel" in events


def test_recurring_lock_loss_after_release_cancels_and_bars_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    wait_gate = asyncio.Event()
    job = _job()
    request = _request(job)
    operation = _operation(job, request)
    launched = _Launched(
        events,
        CaptureResult(outcome="success", size_bytes=4, truncated=False),
        wait_gate=wait_gate,
    )
    _patch_repository(monkeypatch, operation, events)
    supervisor = CaptureOperationSupervisor(
        launcher=_typed_launcher(launched),
        credential=SecretStr("credential"),
    )

    with pytest.raises(CategorizedError):
        asyncio.run(
            supervisor.execute(
                _typed_connection(events, fail_probe=2),
                job,
                _snapshot(request, events),
                request,
            )
        )

    assert "release" in events
    assert "cancel" in events
    assert "result" not in events


def test_transition_failure_immediately_after_release_still_cancels_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kdive.jobs.capture_operations import supervisor as supervisor_module

    events: list[str] = []
    job = _job()
    request = _request(job)
    operation = _operation(job, request)
    launched = _Launched(
        events,
        CaptureResult(outcome="success", size_bytes=4, truncated=False),
    )
    _patch_repository(monkeypatch, operation, events)

    async def fail_running(*args: object, **kwargs: object) -> CaptureOperation:
        raise RuntimeError("transition connection lost")

    monkeypatch.setattr(supervisor_module, "mark_running", fail_running)
    supervisor = CaptureOperationSupervisor(
        launcher=_typed_launcher(launched),
        credential=SecretStr("credential"),
    )

    with pytest.raises(RuntimeError, match="transition connection lost"):
        asyncio.run(
            supervisor.execute(_typed_connection(events), job, _snapshot(request, events), request)
        )

    assert events.index("release") < events.index("cancel_requested") < events.index("cancel")
    assert "result" not in events


def test_durable_cancel_failure_does_not_skip_child_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kdive.jobs.capture_operations import supervisor as supervisor_module

    events: list[str] = []
    job = _job()
    request = _request(job)
    operation = _operation(job, request)
    launched = _Launched(
        events,
        CaptureResult(outcome="success", size_bytes=4, truncated=False),
    )
    _patch_repository(monkeypatch, operation, events)

    async def fail_cancel(*args: object, **kwargs: object) -> CaptureOperation:
        events.append("cancel_request_failed")
        raise RuntimeError("cancel transition unavailable")

    monkeypatch.setattr(supervisor_module, "request_cancel", fail_cancel)
    supervisor = CaptureOperationSupervisor(
        launcher=_typed_launcher(launched),
        credential=SecretStr("credential"),
    )

    with pytest.raises(CategorizedError):
        asyncio.run(
            supervisor.execute(
                _typed_connection(events, fail_probe=1),
                job,
                _snapshot(request, events),
                request,
            )
        )

    assert events.index("cancel_request_failed") < events.index("cancel")


def test_cancellation_waits_for_cleanup_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> list[str]:
        events: list[str] = []
        wait_gate = asyncio.Event()
        job = _job()
        request = _request(job)
        operation = _operation(job, request)
        launched = _Launched(
            events,
            CaptureResult(outcome="success", size_bytes=4, truncated=False),
            wait_gate=wait_gate,
        )
        _patch_repository(monkeypatch, operation, events)
        supervisor = CaptureOperationSupervisor(
            launcher=_typed_launcher(launched),
            credential=SecretStr("credential"),
        )
        task = asyncio.create_task(
            supervisor.execute(_typed_connection(events), job, _snapshot(request, events), request)
        )
        while "release" not in events:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return events

    events = asyncio.run(_run())
    assert events.index("cancel_requested") < events.index("cancel")
    assert events.index("quiescence") < events.index("ack")


def test_child_surviving_term_and_kill_remains_cancel_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    job = _job()
    request = _request(job)
    operation = _operation(job, request)
    launched = _Launched(
        events,
        CaptureResult(outcome="success", size_bytes=4, truncated=False),
        cancel_result=False,
    )
    _patch_repository(monkeypatch, operation, events)
    supervisor = CaptureOperationSupervisor(
        launcher=_typed_launcher(launched),
        credential=SecretStr("credential"),
    )

    with pytest.raises(CategorizedError):
        asyncio.run(
            supervisor.execute(
                _typed_connection(events, fail_probe=1),
                job,
                _snapshot(request, events),
                request,
            )
        )

    assert "cancel_requested" in events
    assert "ack" not in events
    assert "result" not in events


def test_startup_recovery_proves_process_then_provider_before_acknowledgment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kdive.jobs.capture_operations import supervisor as supervisor_module
    from kdive.jobs.capture_operations.repository import CaptureRecoveryCandidate

    operation_id = uuid4()
    resource_id = uuid4()
    system_id = uuid4()
    events: list[str] = []
    candidate = CaptureRecoveryCandidate(
        id=operation_id,
        job_id=uuid4(),
        job_attempt=1,
        worker_incarnation="old-worker",
        provider_kind="local-libvirt",
        resource_id=resource_id,
        system_id=system_id,
        domain_name="guest",
        launch_token=None,
        host_instance="host-a",
        boot_id="boot-a",
        pid=123,
        start_ticks=456,
        state="running",
    )

    async def candidates(*args: object) -> tuple[CaptureRecoveryCandidate, ...]:
        return (candidate,)

    class _Identity:
        def is_absent(self, *, current_host_instance: str) -> bool:
            assert current_host_instance == "host-a"
            events.append("absent")
            return True

    class _RecoveryQuiescence:
        def prove_absent(self, *args: object) -> QuiescenceEvidence:
            events.append("quiescence")
            return QuiescenceEvidence(
                provider_kind="local-libvirt",
                resource_id=resource_id,
                domain_name="guest",
                qom_id=f"kdive-dump-{candidate.job_id}",
                result="absent",
                ordering="fresh-qmp-connection",
            )

    async def recover(*args: object) -> CaptureOperation:
        events.append("recover")
        return _operation(_job_for_candidate(candidate), _request_for_candidate(candidate))

    monkeypatch.setattr(supervisor_module, "list_recovery_candidates", candidates)
    monkeypatch.setattr(supervisor_module, "LinuxIdentity", lambda **kwargs: _Identity())

    async def recovery_quiescence(*args: object, **kwargs: object) -> _RecoveryQuiescence:
        return _RecoveryQuiescence()

    monkeypatch.setattr(supervisor_module, "_recovery_quiescence", recovery_quiescence)
    monkeypatch.setattr(supervisor_module, "recover_operation", recover)

    class _Pool:
        def connection(self) -> object:
            class _Context:
                async def __aenter__(self) -> object:
                    return object()

                async def __aexit__(self, *args: object) -> None:
                    return None

            return _Context()

    summary = asyncio.run(
        recover_capture_operations(
            cast(AsyncConnectionPool, _Pool()),
            cast(ProviderResolver, SimpleNamespace()),
            "host-a",
            SecretStr("replacement"),
        )
    )
    assert summary == RecoverySummary(scanned=1, recovered=1, pending=0)
    assert events == ["absent", "quiescence", "recover"]


def _job_for_candidate(candidate: Any) -> Job:
    job = _job()
    return job.model_copy(update={"id": candidate.job_id, "attempt": candidate.job_attempt})


def _request_for_candidate(candidate: Any) -> CaptureRequest:
    return CaptureRequest(
        job_id=candidate.job_id,
        provider_kind=candidate.provider_kind,
        resource_id=candidate.resource_id,
        system_id=candidate.system_id,
        domain_name=candidate.domain_name,
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=1,
    )
