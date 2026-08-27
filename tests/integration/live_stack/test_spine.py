"""Non-gated unit tests for shared live-stack spine contracts (ADR-0042/0045)."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

import tests.integration.live_stack.spine as spine
from kdive.domain.capacity.state import JobState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import worker as job_worker
from kdive.mcp.responses import ToolResponse
from kdive.providers.local_libvirt.lifecycle import provisioning
from tests.integration.live_stack.spine import (
    SpinePhaseError,
    await_system_state,
    drain_job,
    phase,
)


class _FakeClient:
    def __init__(self, responses: list[ToolResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, **args: object) -> ToolResponse:
        self.calls.append((name, args))
        if not self._responses:
            raise AssertionError(f"unexpected {name} call with {args}")
        return self._responses.pop(0)


def _client(responses: list[ToolResponse]) -> _FakeClient:
    return _FakeClient(responses)


def _live_client(client: _FakeClient) -> Any:
    return cast(Any, client)


def _job(status: str, *, category: ErrorCategory | None = None) -> ToolResponse:
    return ToolResponse(
        object_id="job-1",
        status=status,
        error_category=category.value if category else None,
    )


def _system(status: str, *, category: ErrorCategory | None = None) -> ToolResponse:
    return ToolResponse(
        object_id="system-1",
        status=status,
        error_category=category.value if category else None,
    )


def test_record_provision_evidence_target_creates_private_exact_record(tmp_path: Path) -> None:
    target = tmp_path / "provision-target"

    spine.record_provision_evidence_target(
        target,
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    )

    assert target.read_text() == (
        "11111111-1111-1111-1111-111111111111\t22222222-2222-2222-2222-222222222222\n"
    )
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_record_provision_evidence_target_normalizes_restrictive_umask(tmp_path: Path) -> None:
    target = tmp_path / "provision-target"
    previous_umask = os.umask(0o777)
    try:
        spine.record_provision_evidence_target(
            target,
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
    finally:
        os.umask(previous_umask)

    assert os.stat(target).st_mode & 0o777 == 0o600
    assert target.read_text() == (
        "11111111-1111-1111-1111-111111111111\t22222222-2222-2222-2222-222222222222\n"
    )


def test_record_provision_evidence_target_refuses_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "provision-target"
    target.write_text("first")

    with pytest.raises(FileExistsError):
        spine.record_provision_evidence_target(
            target,
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )

    assert target.read_text() == "first"


_PAYLOAD_SENTINEL = "PAYLOAD_SENTINEL"
_AUTHORIZING_SENTINEL = "AUTHORIZING_SENTINEL"
_PROVISION_STAGES = (
    "resolve-arch",
    "materialize-rootfs",
    "prepare-baseline",
    "prepare-overlay",
    "render-domain",
    "customize-overlay",
    "prepare-console",
    "define-start",
)


def _claimed_job(kind: JobKind) -> Job:
    enqueued_at = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    return Job.model_construct(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        created_at=enqueued_at,
        updated_at=enqueued_at,
        kind=kind,
        dispatch_lane="persisted-provision-lane",
        payload={"system_id": _PAYLOAD_SENTINEL},
        state=JobState.RUNNING,
        attempt=3,
        max_attempts=5,
        worker_id="fixed-worker-1",
        heartbeat_at=enqueued_at - timedelta(seconds=2),
        authorizing={
            "principal": _AUTHORIZING_SENTINEL,
            "agent_session": None,
            "project": _AUTHORIZING_SENTINEL,
        },
        dedup_key="provision",
    )


class _WorkerConnection:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


class _WorkerPool:
    max_size = 8

    def connection(self) -> _WorkerConnection:
        return _WorkerConnection()


def _contract_worker(
    *, telemetry: object | None = None, pool: object | None = None
) -> job_worker.Worker:
    registry = SimpleNamespace(get=lambda _kind: object())
    return job_worker.Worker(
        cast(Any, pool if pool is not None else _WorkerPool()),
        cast(Any, registry),
        worker_id="fixed-worker-1",
        incarnation_credential=cast(Any, object()),
        secret_registry=cast(Any, object()),
        config=job_worker.WorkerConfig(telemetry=cast(Any, telemetry)),
    )


def test_worker_lanes_publish_exact_startup_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="kdive.jobs.worker"):
        _contract_worker()

    messages = [record.getMessage() for record in caplog.records]
    assert "worker fixed-worker-1 accepting dispatch lanes: default,state-fenced" in messages


def test_worker_claim_captures_dequeue_record_before_mutation(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provision = _claimed_job(JobKind.PROVISION)

    class _CommittedConnection(_WorkerConnection):
        async def __aexit__(self, *_args: object) -> None:
            provision.heartbeat_at = datetime(2026, 8, 26, 12, 30, tzinfo=UTC)
            logging.getLogger("kdive.jobs.worker").info("claim transaction committed")

    class _CommittedPool(_WorkerPool):
        def connection(self) -> _WorkerConnection:
            return _CommittedConnection()

    jobs = [provision, _claimed_job(JobKind.INSTALL)]
    claimed_lanes: list[tuple[str, ...]] = []

    async def queue_is_running(_conn: object) -> bool:
        return False

    async def dequeue(_conn: object, _worker_id: str, **kwargs: object) -> Job | None:
        claimed_lanes.append(cast(tuple[str, ...], kwargs["accepted_lanes"]))
        return jobs.pop(0)

    monkeypatch.setattr(job_worker.queue, "is_queue_paused", queue_is_running)
    monkeypatch.setattr(job_worker.queue, "dequeue", dequeue)

    async def skip_dispatch(_job: Job, _handler: object) -> None:
        return None

    worker = _contract_worker(pool=_CommittedPool())
    monkeypatch.setattr(worker, "_dispatch", skip_dispatch)
    caplog.clear()

    async def run_claims() -> None:
        await worker.run_once("claim-loop-lane")
        await worker.run_once("claim-loop-lane")

    with caplog.at_level(logging.INFO, logger="kdive.jobs.worker"):
        asyncio.run(run_claims())

    messages = [
        record.getMessage()
        for record in caplog.records
        if "claimed provision" in record.getMessage()
    ]
    assert claimed_lanes == [("claim-loop-lane",), ("claim-loop-lane",)]
    assert provision.heartbeat_at == datetime(2026, 8, 26, 12, 30, tzinfo=UTC)
    assert messages == [
        "worker fixed-worker-1 claimed provision job "
        "11111111-1111-1111-1111-111111111111 lane=persisted-provision-lane attempt=3 "
        "enqueued_at=2026-08-26T12:00:00+00:00 claim_at=2026-08-26T11:59:58+00:00 "
        "queue_delay_s=0.000000"
    ]
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert _PAYLOAD_SENTINEL not in rendered
    assert _AUTHORIZING_SENTINEL not in rendered
    rendered_messages = [record.getMessage() for record in caplog.records]
    assert rendered_messages.index("claim transaction committed") < rendered_messages.index(
        messages[0]
    )


def test_worker_claim_is_not_logged_when_queue_depth_rolls_back_transaction(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provision = _claimed_job(JobKind.PROVISION)

    async def queue_is_running(_conn: object) -> bool:
        return False

    async def dequeue(_conn: object, _worker_id: str, **_kwargs: object) -> Job:
        return provision

    async def count_claimable(_conn: object, **_kwargs: object) -> int:
        raise RuntimeError("telemetry depth query failed")

    telemetry = SimpleNamespace(
        enabled=True,
        observe_queue_depth=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(job_worker.queue, "is_queue_paused", queue_is_running)
    monkeypatch.setattr(job_worker.queue, "dequeue", dequeue)
    monkeypatch.setattr(job_worker.queue, "count_claimable", count_claimable)
    worker = _contract_worker(telemetry=telemetry)
    caplog.clear()

    with (
        caplog.at_level(logging.INFO, logger="kdive.jobs.worker"),
        pytest.raises(RuntimeError, match="telemetry depth query failed"),
    ):
        asyncio.run(worker.run_once("claim-loop-lane"))

    assert not [record for record in caplog.records if "claimed provision" in record.getMessage()]


def test_provision_stage_logs_completion_only_after_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    system_id = UUID("22222222-2222-2222-2222-222222222222")
    job_id = UUID("11111111-1111-1111-1111-111111111111")

    with caplog.at_level(
        logging.INFO, logger="kdive.providers.local_libvirt.lifecycle.provisioning"
    ):
        with provisioning._provision_stage(system_id, job_id, "prepare-baseline"):
            pass
        with (
            pytest.raises(RuntimeError, match="stalled"),
            provisioning._provision_stage(system_id, job_id, "prepare-overlay"),
        ):
            raise RuntimeError("stalled")

    messages = [record.getMessage() for record in caplog.records]
    assert [message.rsplit(" event=", 1)[-1] for message in messages] == [
        "start",
        "complete",
        "start",
    ]


def _provision_operation(failing_stage: str | None, stage: str, result: object) -> Any:
    def operation(*_args: object, **_kwargs: object) -> object:
        if failing_stage == stage:
            raise CategorizedError(
                "GUEST_OUTPUT_SENTINEL CREDENTIAL_SENTINEL",
                category=ErrorCategory.PROVISIONING_FAILURE,
            )
        return result

    return operation


def _configured_provisioner(
    monkeypatch: pytest.MonkeyPatch, failing_stage: str | None = None
) -> tuple[Any, object, Any]:
    provider_config = SimpleNamespace(
        rootfs=SimpleNamespace(value="PROFILE_SENTINEL"),
        baseline_kernel="PROFILE_SENTINEL",
        debug=SimpleNamespace(gdbstub=True),
    )
    profile = SimpleNamespace(
        arch="PROFILE_SENTINEL",
        disk_gb=10,
        provider=SimpleNamespace(local_libvirt=provider_config),
    )
    instance = cast(Any, object.__new__(provisioning.LocalLibvirtProvisioning))
    instance._guest_egress = False
    instance._files = SimpleNamespace(
        prepare_overlay=_provision_operation(
            failing_stage,
            "prepare-overlay",
            SimpleNamespace(path=Path("/PATH_SENTINEL/overlay"), created=True),
        ),
        prepare_console=_provision_operation(failing_stage, "prepare-console", None),
    )
    instance._resolve_guest_arch = _provision_operation(
        failing_stage, "resolve-arch", ("kvm", "/PATH_SENTINEL/emulator")
    )
    instance._materialize_rootfs = _provision_operation(
        failing_stage, "materialize-rootfs", Path("/PATH_SENTINEL/base")
    )
    instance._prepare_baseline_kernel = _provision_operation(
        failing_stage,
        "prepare-baseline",
        SimpleNamespace(
            kernel=Path("/PATH_SENTINEL/kernel"),
            initrd=Path("/PATH_SENTINEL/initrd"),
        ),
    )
    instance._gdb_port_for = lambda _system_id: 1234
    instance._ssh_port_for = lambda _system_id: 22000
    instance._define_and_start = _provision_operation(failing_stage, "define-start", None)
    instance._snapshot_pre_existing = lambda _system_id: SimpleNamespace(
        overlay=False, baseline=False
    )
    instance._reclaim_materialized_on_failure = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        provisioning,
        "render_domain_xml",
        _provision_operation(
            failing_stage,
            "render-domain",
            "<XML_SENTINEL>GUEST_OUTPUT_SENTINEL CREDENTIAL_SENTINEL</XML_SENTINEL>",
        ),
    )
    customizer = _provision_operation(failing_stage, "customize-overlay", "GUEST_OUTPUT_SENTINEL")
    return instance, profile, customizer


def _provider_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        message
        for record in caplog.records
        if (message := record.getMessage()).startswith("local-libvirt provision ")
    ]


def _expected_provider_records(system_id: UUID, job_id: UUID, stages: tuple[str, ...]) -> list[str]:
    return [
        f"local-libvirt provision system={system_id} job={job_id} stage={stage} event={event}"
        for stage in stages
        for event in ("start", "complete")
    ]


def test_provision_logs_exact_safe_records_for_every_mapped_stage(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_id = UUID("22222222-2222-2222-2222-222222222222")
    job_id = UUID("11111111-1111-1111-1111-111111111111")
    instance, profile, customizer = _configured_provisioner(monkeypatch)

    with caplog.at_level(
        logging.INFO, logger="kdive.providers.local_libvirt.lifecycle.provisioning"
    ):
        instance.provision(
            system_id,
            profile,
            overlay_customizers=(customizer,),
            bootstrap_pubkey="CREDENTIAL_SENTINEL",
            job_id=job_id,
        )

    records = _provider_records(caplog)
    assert records == _expected_provider_records(system_id, job_id, _PROVISION_STAGES)
    rendered = "\n".join(records)
    for sentinel in (
        "PROFILE_SENTINEL",
        "PATH_SENTINEL",
        "XML_SENTINEL",
        "GUEST_OUTPUT_SENTINEL",
        "CREDENTIAL_SENTINEL",
    ):
        assert sentinel not in rendered


@pytest.mark.parametrize("failing_stage", _PROVISION_STAGES)
def test_provision_operation_failure_leaves_exact_stage_start_unmatched(
    failing_stage: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_id = UUID("22222222-2222-2222-2222-222222222222")
    job_id = UUID("11111111-1111-1111-1111-111111111111")
    instance, profile, customizer = _configured_provisioner(monkeypatch, failing_stage)

    with (
        caplog.at_level(
            logging.INFO, logger="kdive.providers.local_libvirt.lifecycle.provisioning"
        ),
        pytest.raises(CategorizedError),
    ):
        instance.provision(
            system_id,
            profile,
            overlay_customizers=(customizer,),
            bootstrap_pubkey="CREDENTIAL_SENTINEL",
            job_id=job_id,
        )

    failed_index = _PROVISION_STAGES.index(failing_stage)
    expected = _expected_provider_records(system_id, job_id, _PROVISION_STAGES[:failed_index])
    expected.append(
        f"local-libvirt provision system={system_id} job={job_id} stage={failing_stage} event=start"
    )
    records = _provider_records(caplog)
    assert records == expected
    assert records[-1].endswith(f"stage={failing_stage} event=start")


async def _no_sleep(_seconds: float) -> None:
    return None


def test_phase_names_the_failing_phase() -> None:
    """A raised exception inside a phase becomes a SpinePhaseError naming that phase."""

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("provision"):
                raise ValueError("libvirt exploded")
        assert excinfo.value.phase == "provision"
        assert isinstance(excinfo.value.__cause__, ValueError)

    asyncio.run(_run())


def test_phase_passes_through_spine_phase_error() -> None:
    """An inner SpinePhaseError is preserved (not re-wrapped under the outer phase name)."""

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("outer"):
                raise SpinePhaseError("boot", "job failed", error_category="infrastructure_failure")
        assert excinfo.value.phase == "boot"

    asyncio.run(_run())


def test_drain_job_waits_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job drain helper keeps polling non-terminal jobs and returns success."""
    monkeypatch.setattr(spine.asyncio, "sleep", _no_sleep)
    client = _client([_job("running"), _job("succeeded")])

    async def _run() -> None:
        result = await drain_job(_live_client(client), "build", "job-1", deadline_s=60.0)

        assert result.status == "succeeded"
        assert [name for name, _args in client.calls] == ["jobs.wait", "jobs.wait"]
        assert client.calls[0][1] == {"job_id": "job-1", "timeout_s": 60.0}

    asyncio.run(_run())


def test_drain_job_classifies_terminal_failure() -> None:
    """Terminal job failure raises a phase-scoped error with the original category."""
    client = _client([_job("failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)])

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            await drain_job(_live_client(client), "capture", "job-1")

        assert excinfo.value.phase == "capture"
        assert excinfo.value.reason == "job failed"
        assert excinfo.value.error_category == "infrastructure_failure"

    asyncio.run(_run())


def test_drain_job_classifies_worker_stall_without_sleeping() -> None:
    """A non-terminal job past its deadline reports a worker-stall timeout."""
    client = _client([_job("running")])

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            await drain_job(_live_client(client), "install", "job-1", deadline_s=-1.0)

        assert excinfo.value.phase == "install"
        assert excinfo.value.reason == "drain_timeout"

    asyncio.run(_run())


def test_await_system_state_polls_until_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """System-state polling returns once the target state is visible."""
    monkeypatch.setattr(spine.asyncio, "sleep", _no_sleep)
    client = _client([_system("booting"), _system("ready")])

    async def _run() -> None:
        await await_system_state(_live_client(client), "provision", "system-1", "ready")

        assert [name for name, _args in client.calls] == ["systems.get", "systems.get"]
        assert client.calls[0][1] == {"system_id": "system-1"}

    asyncio.run(_run())


def test_await_system_state_logs_distinct_status_transitions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every DISTINCT observed status is logged once with its elapsed offset (#2056).

    A red proof must carry its own state timeline on stderr — "stuck provisioning" vs
    "cycling states" — without a hand-instrumented re-run; repeated identical statuses
    stay silent so the log reads as transitions, not poll ticks.
    """
    monkeypatch.setattr(spine.asyncio, "sleep", _no_sleep)
    client = _client([_system("provisioning"), _system("provisioning"), _system("ready")])

    async def _run() -> None:
        await await_system_state(_live_client(client), "ppc64le:provision", "system-1", "ready")

        transitions = [
            line
            for line in capsys.readouterr().err.splitlines()
            if line.startswith("ppc64le:provision:")
        ]
        assert transitions == [
            "ppc64le:provision: t+0s provisioning",
            "ppc64le:provision: t+0s ready",
        ]

    asyncio.run(_run())


def test_await_system_state_classifies_error_envelope() -> None:
    """Error envelopes from systems.get keep their category on the phase failure."""
    client = _client([_system("error", category=ErrorCategory.NOT_FOUND)])

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            await await_system_state(_live_client(client), "teardown", "system-1", "torn_down")

        assert excinfo.value.phase == "teardown"
        assert excinfo.value.reason == "system error"
        assert excinfo.value.error_category == "not_found"

    asyncio.run(_run())


def test_await_system_state_classifies_timeout_without_sleeping() -> None:
    """A system that never reaches the target reports the missing target state."""
    client = _client([_system("releasing")])

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            await await_system_state(
                _live_client(client), "teardown", "system-1", "torn_down", deadline_s=-1.0
            )

        assert excinfo.value.phase == "teardown"
        assert excinfo.value.reason == "system did not reach torn_down"

    asyncio.run(_run())
