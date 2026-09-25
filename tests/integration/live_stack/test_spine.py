"""Non-gated unit tests for shared live-stack spine contracts (ADR-0042/0045)."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

import tests.integration.live_stack.spine as spine
from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import worker as job_worker
from kdive.mcp.responses import JsonValue, ToolResponse
from tests.integration.live_stack.spine import (
    SpinePhaseError,
    await_system_state,
    drain_job,
    ok,
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


_BOOT_CONFIG = b"CONFIG_VIRTIO_PCI=y\nCONFIG_VIRTIO_BLK=y\nCONFIG_EXT4_FS=y\n"


@pytest.mark.parametrize("missing", ["VIRTIO_PCI", "VIRTIO_BLK", "EXT4_FS"])
def test_spine_config_refuses_missing_built_in(tmp_path: Path, missing: str) -> None:
    config = _BOOT_CONFIG.replace(f"CONFIG_{missing}=y".encode(), f"CONFIG_{missing}=m".encode())
    (tmp_path / ".config").write_bytes(config)

    with pytest.raises(SpinePhaseError, match=f"CONFIG_{missing}=y"):
        spine.check_spine_kernel_config(tmp_path, "ppc64le", "upload-build")


def test_spine_config_refuses_missing_config(tmp_path: Path) -> None:
    with pytest.raises(SpinePhaseError, match=r"\.config"):
        spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build")


def test_spine_config_scopes_filesystem_to_guest(tmp_path: Path) -> None:
    (tmp_path / ".config").write_bytes(
        b"CONFIG_VIRTIO_PCI=y\nCONFIG_VIRTIO_BLK=y\nCONFIG_XFS_FS=y\n"
    )
    assert spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build")
    with pytest.raises(SpinePhaseError, match="CONFIG_EXT4_FS=y"):
        spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build", root_fs="ext4")
    with pytest.raises(SpinePhaseError, match="CONFIG_EXT4_FS=y"):
        spine.check_spine_kernel_config(tmp_path, "ppc64le", "upload-build")


def test_spine_config_network_module_route_is_caller_scoped(tmp_path: Path) -> None:
    config = tmp_path / ".config"
    config.write_bytes(_BOOT_CONFIG)
    assert spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build")
    with pytest.raises(SpinePhaseError, match="CONFIG_VIRTIO_NET"):
        spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build", require_network=True)
    config.write_bytes(_BOOT_CONFIG + b"CONFIG_VIRTIO_NET=m\n")
    assert spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build", require_network=True)


def test_spine_config_live_debug_requires_btf_and_dwarf(tmp_path: Path) -> None:
    config = tmp_path / ".config"
    config.write_bytes(_BOOT_CONFIG + b"CONFIG_DEBUG_INFO_DWARF5=y\n")
    with pytest.raises(SpinePhaseError, match="CONFIG_DEBUG_INFO_BTF=y"):
        spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build", require_live_debug=True)

    config.write_bytes(_BOOT_CONFIG + b"CONFIG_DEBUG_INFO_BTF=y\n")
    with pytest.raises(
        SpinePhaseError, match="CONFIG_DEBUG_INFO_DWARF4=y or CONFIG_DEBUG_INFO_DWARF5=y"
    ):
        spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build", require_live_debug=True)

    config.write_bytes(_BOOT_CONFIG + b"CONFIG_DEBUG_INFO_BTF=y\nCONFIG_DEBUG_INFO_DWARF5=y\n")
    assert spine.check_spine_kernel_config(
        tmp_path, "x86_64", "upload-build", require_live_debug=True
    )


@pytest.mark.parametrize("arch,required", [("x86_64", "FW_CFG_SYSFS"), ("ppc64le", "CRASH_DUMP")])
def test_spine_config_kdump_respects_arch(tmp_path: Path, arch: str, required: str) -> None:
    (tmp_path / ".config").write_bytes(
        _BOOT_CONFIG
        + b"CONFIG_KEXEC_FILE=y\nCONFIG_CRASH_DUMP=y\nCONFIG_PROC_VMCORE=y\n"
        + b"CONFIG_RELOCATABLE=y\n"
        if arch == "x86_64"
        else _BOOT_CONFIG + b"CONFIG_KEXEC_FILE=y\nCONFIG_PROC_VMCORE=y\nCONFIG_RELOCATABLE=y\n"
    )
    with pytest.raises(SpinePhaseError, match=f"CONFIG_{required}"):
        spine.check_spine_kernel_config(tmp_path, arch, "upload-build", require_kdump=True)


def test_spine_upload_rejects_config_before_staging_or_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".config").write_bytes(b"CONFIG_VIRTIO_PCI=m\n")
    monkeypatch.setenv(spine.KERNEL_TREE_ENV, str(tmp_path))
    monkeypatch.setattr(spine, "accepted_run_upload_names", lambda _contract: ["kernel"])
    stage = Mock(side_effect=AssertionError("staging reached"))
    monkeypatch.setattr(spine, "combined_kernel_tar", stage)
    client = SimpleNamespace(
        read_text_resource=AsyncMock(return_value="{}"),
        call_tool=AsyncMock(side_effect=AssertionError("upload reached")),
    )

    with pytest.raises(SpinePhaseError, match="CONFIG_VIRTIO_PCI=y"):
        asyncio.run(spine.build_and_upload_kernel(cast(Any, client), run_id="run-1"))
    (tmp_path / ".config").write_bytes(_BOOT_CONFIG + b"CONFIG_VIRTIO_NET=y\n")
    with pytest.raises(SpinePhaseError, match="CONFIG_DEBUG_INFO_BTF=y"):
        asyncio.run(
            spine.build_and_upload_kernel(
                cast(Any, client), run_id="run-1", require_live_debug=True
            )
        )
    with pytest.raises(SpinePhaseError, match="CONFIG_KEXEC"):
        asyncio.run(
            spine.build_and_upload_kernel(cast(Any, client), run_id="run-1", require_kdump=True)
        )
    stage.assert_not_called()
    client.call_tool.assert_not_called()


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


def test_full_artifact_text_nests_request_and_pages() -> None:
    client = _client(
        [
            ToolResponse.success(
                "artifact-1",
                "ready",
                data={"content": "first", "content_truncated": True, "next_offset": 5},
            ),
            ToolResponse.success(
                "artifact-1",
                "ready",
                data={"content": "second", "content_truncated": False},
            ),
        ]
    )

    result = asyncio.run(
        spine.full_artifact_text(_live_client(client), "artifact-1", "read-artifact")
    )

    assert result == "firstsecond"
    assert client.calls == [
        (
            "artifacts.get",
            {"request": {"artifact_id": "artifact-1", "byte_offset": 0}},
        ),
        (
            "artifacts.get",
            {"request": {"artifact_id": "artifact-1", "byte_offset": 5}},
        ),
    ]


def _console_part(artifact_id: str, index: int) -> ToolResponse:
    return ToolResponse.success(
        artifact_id,
        "ready",
        refs={"object": f"local/systems/system-1/console-part-1-{index:06d}"},
    )


def _artifact_listing(*parts: ToolResponse) -> ToolResponse:
    return ToolResponse.collection("system-1", "ready", list(parts))


def _artifact_content(artifact_id: str, content: str) -> ToolResponse:
    return ToolResponse.success(
        artifact_id,
        "ready",
        data={"content": content, "content_truncated": False},
    )


def test_poll_skips_new_console_part_without_marker_and_returns_later_match() -> None:
    marker = "proof-marker"
    client = _client(
        [
            _artifact_listing(_console_part("early", 1), _console_part("old", 0)),
            _artifact_content("early", "boot output only"),
            _artifact_listing(
                _console_part("early", 1),
                _console_part("later", 2),
                _console_part("old", 0),
            ),
            _artifact_content("later", f"prefix {marker} suffix"),
        ]
    )

    result = asyncio.run(
        spine.poll_for_new_console_part(
            _live_client(client),
            "system-1",
            {"old"},
            marker,
            deadline_s=1.0,
            interval_s=0.0,
        )
    )

    assert result == ("later", f"prefix {marker} suffix")
    assert client.calls == [
        ("artifacts.list", {"system_id": "system-1"}),
        (
            "artifacts.get",
            {"request": {"artifact_id": "early", "byte_offset": 0}},
        ),
        ("artifacts.list", {"system_id": "system-1"}),
        (
            "artifacts.get",
            {"request": {"artifact_id": "later", "byte_offset": 0}},
        ),
    ]


def test_poll_times_out_after_inspecting_each_immutable_part_once() -> None:
    client = _client(
        [
            _artifact_listing(_console_part("early", 1), _console_part("old", 0)),
            _artifact_content("early", "boot output only"),
        ]
    )

    with pytest.raises(
        SpinePhaseError,
        match=r"no marker-bearing console-part artifacts within 0s .*inspected=1",
    ):
        asyncio.run(
            spine.poll_for_new_console_part(
                _live_client(client),
                "system-1",
                {"old"},
                "proof-marker",
                deadline_s=0.0,
                interval_s=0.0,
            )
        )

    assert [call for call in client.calls if call[0] == "artifacts.get"] == [
        (
            "artifacts.get",
            {"request": {"artifact_id": "early", "byte_offset": 0}},
        )
    ]


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("created_at", None, id="missing-enqueued-at"),
        pytest.param("heartbeat_at", None, id="missing-claim-at"),
        pytest.param("created_at", datetime(2026, 8, 26, 12), id="naive-enqueued-at"),
        pytest.param("heartbeat_at", datetime(2026, 8, 26, 12), id="naive-claim-at"),
    ],
)
def test_worker_claim_rejects_non_authoritative_timestamps_before_commit(
    field: str,
    value: datetime | None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provision = _claimed_job(JobKind.PROVISION)
    object.__setattr__(provision, field, value)
    transaction_failed: list[bool] = []

    class _FailClosedConnection(_WorkerConnection):
        async def __aexit__(self, *args: object) -> None:
            transaction_failed.append(args[0] is ValueError)

    class _FailClosedPool(_WorkerPool):
        def connection(self) -> _WorkerConnection:
            return _FailClosedConnection()

    async def queue_is_running(_conn: object) -> bool:
        return False

    async def dequeue(_conn: object, _worker_id: str, **_kwargs: object) -> Job:
        return provision

    monkeypatch.setattr(job_worker.queue, "is_queue_paused", queue_is_running)
    monkeypatch.setattr(job_worker.queue, "dequeue", dequeue)
    worker = _contract_worker(pool=_FailClosedPool())
    caplog.clear()

    with (
        caplog.at_level(logging.INFO, logger="kdive.jobs.worker"),
        pytest.raises(ValueError, match="timezone-aware"),
    ):
        asyncio.run(worker.run_once("claim-loop-lane"))

    assert transaction_failed == [True]
    assert not [record for record in caplog.records if "claimed provision" in record.getMessage()]


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


def test_spine_phase_error_renders_detail_and_data() -> None:
    """The rendered message and attributes carry the envelope's detail and data (#2500)."""
    error = SpinePhaseError(
        "allocate",
        "error envelope",
        error_category="allocation_denied",
        detail="host capacity exhausted (cap 1, in use 1)",
        data={"reason": "at_capacity", "cap": "1", "in_use": "1"},
    )

    assert error.detail == "host capacity exhausted (cap 1, in use 1)"
    assert error.data == {"reason": "at_capacity", "cap": "1", "in_use": "1"}
    message = str(error)
    assert "host capacity exhausted (cap 1, in use 1)" in message
    assert "at_capacity" in message
    assert "cap=1" in message


def test_spine_phase_error_stays_readable_without_detail_or_data() -> None:
    """No detail/data on the envelope reproduces the original bare rendering (#2500)."""
    error = SpinePhaseError("allocate", "error envelope", error_category="allocation_denied")

    assert error.detail is None
    assert error.data == {}
    assert str(error) == "phase 'allocate' failed: error envelope (allocation_denied)"


def test_spine_phase_error_caps_a_large_rendered_data_value() -> None:
    """A nested/oversized ``data`` value renders truncated, not as an unbounded one-liner."""
    unmet: list[JsonValue] = [
        {
            "gate": f"gate-{i}",
            "current": i,
            "required": i + 5,
            "remedy": "accounting.set_quota",
        }
        for i in range(6)
    ]
    error = SpinePhaseError(
        "allocate", "error envelope", error_category="allocation_denied", data={"unmet": unmet}
    )

    message = str(error)
    assert message.endswith("…]")
    assert len(message) < 300


def test_ok_carries_envelope_detail_and_data_into_the_phase_error() -> None:
    """``ok()`` surfaces the failing envelope's detail and data, not just status/category."""
    envelope = ToolResponse(
        object_id="alloc-1",
        status="error",
        error_category="allocation_denied",
        detail="host capacity exhausted (cap 1, in use 1)",
        data={"reason": "at_capacity", "cap": "1", "in_use": "1"},
    )

    with pytest.raises(SpinePhaseError) as excinfo:
        ok(envelope, "allocate")

    assert excinfo.value.error_category == "allocation_denied"
    assert excinfo.value.detail == "host capacity exhausted (cap 1, in use 1)"
    assert excinfo.value.data == {"reason": "at_capacity", "cap": "1", "in_use": "1"}


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


# --- the worker's libvirt endpoint (#2383) ---------------------------------------------------


def _no_published_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the fallback away from the real host file, so these cases do not depend on the host."""
    monkeypatch.setattr(spine, "_PUBLISHED_LIBVIRT_URI", Path("/nonexistent/kdive-libvirt.env"))


def test_worker_libvirt_uri_defaults_to_system(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    _no_published_endpoint(monkeypatch)
    assert spine.worker_libvirt_uri() == "qemu:///system"


def test_worker_libvirt_uri_honors_a_session_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    # The regression: #2383's native-POWER9 worker published this socket, the attribute phases
    # ran `virsh -c qemu:///system`, and all three tests died before reaching the crash step.
    socket_uri = "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", socket_uri)
    assert spine.worker_libvirt_uri() == socket_uri


def test_worker_libvirt_uri_treats_empty_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Matches `scripts/live-stack/lib.sh`'s `${KDIVE_LIBVIRT_URI:-qemu:///system}`, where an empty
    # value takes the default rather than producing `virsh -c ''`.
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "   ")
    _no_published_endpoint(monkeypatch)
    assert spine.worker_libvirt_uri() == "qemu:///system"


def _publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, *, mode: int = 0o644
) -> Path:
    """Stand in for the root-published endpoint file, with its ownership guard satisfied."""
    published = tmp_path / "live-worker-libvirt.env"
    published.write_text(text, encoding="utf-8")
    published.chmod(mode)
    monkeypatch.setattr(spine, "_PUBLISHED_LIBVIRT_URI", published)
    # The real file is root-owned; a test file is owned by the test user, so report the guard's
    # expected identity and let the mode and single-assignment checks do the discriminating.
    real_lstat = Path.lstat

    def _lstat(self: Path) -> os.stat_result:
        info = real_lstat(self)
        if self != published:
            return info
        fields = list(info)
        fields[4] = 0  # st_uid
        fields[5] = 0  # st_gid
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", _lstat)
    return published


_SOCKET_URI = "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"


def test_worker_libvirt_uri_reads_the_published_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `lib.sh` exports the variable in the bring-up shell, not in the pytest process that runs
    # hours later, so the published file is what a session-daemon host actually falls back to.
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    _publish(monkeypatch, tmp_path, f"KDIVE_LIBVIRT_URI={_SOCKET_URI}\n")
    assert spine.worker_libvirt_uri() == _SOCKET_URI


def test_explicit_env_outranks_the_published_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///session")
    _publish(monkeypatch, tmp_path, f"KDIVE_LIBVIRT_URI={_SOCKET_URI}\n")
    assert spine.worker_libvirt_uri() == "qemu:///session"


def test_a_world_writable_published_endpoint_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The file selects the socket a virsh subprocess connects to, so anything but the installer's
    # own 0644 is not read — the same guard scripts/live-stack/libvirt-uri.sh applies.
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    _publish(monkeypatch, tmp_path, f"KDIVE_LIBVIRT_URI={_SOCKET_URI}\n", mode=0o666)
    assert spine.worker_libvirt_uri() == "qemu:///system"


def test_a_multi_line_published_endpoint_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # libvirt-uri.sh requires exactly one assignment; a second line means the file is not the
    # contract it claims to be, so the default stands rather than a guessed first match.
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    _publish(monkeypatch, tmp_path, f"KDIVE_LIBVIRT_URI={_SOCKET_URI}\nEXTRA=1\n")
    assert spine.worker_libvirt_uri() == "qemu:///system"


def test_a_missing_published_endpoint_takes_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    _no_published_endpoint(monkeypatch)
    assert spine.worker_libvirt_uri() == "qemu:///system"
