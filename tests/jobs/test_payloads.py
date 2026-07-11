"""Tests for typed job payload contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.domain.capacity.state import JobState
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.operations.jobs import Job, JobKind, PowerAction
from kdive.domain.operations.sysrq import SysRqCommand
from kdive.jobs.payloads import (
    Authorizing,
    BuildPayload,
    CaptureVmcorePayload,
    DiagnosticsWorkerCheckPayload,
    InstallPayload,
    PayloadValidationError,
    PowerPayload,
    ReprovisionPayload,
    SysRqPayload,
    SystemPayload,
    dump_authorizing,
    dump_payload,
    load_payload,
    run_id_from_payload,
)

WORKER_LOCAL_ID = "00000000-0000-0000-0000-0000000000c0"  # was db.build_hosts.WORKER_LOCAL_ID


def test_build_payload_round_trips_with_optional_cmdline() -> None:
    run_id = uuid4()
    now = datetime.now(UTC)

    payload = dump_payload(
        JobKind.BUILD,
        {
            "run_id": str(run_id),
            "build_host_id": str(WORKER_LOCAL_ID),
            "cmdline": "panic=1",
        },
    )
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.BUILD,
        payload=payload,
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key="build",
    )
    decoded = load_payload(job, BuildPayload)

    assert payload == {
        "run_id": str(run_id),
        "build_host_id": str(WORKER_LOCAL_ID),
        "cmdline": "panic=1",
    }
    assert decoded.run_id == str(run_id)
    assert decoded.cmdline == "panic=1"


def test_build_payload_requires_build_host_id() -> None:
    with pytest.raises(PayloadValidationError, match="build_host_id"):
        dump_payload(JobKind.BUILD, {"run_id": str(uuid4())})


def test_install_payload_round_trips_and_strips_cmdline() -> None:
    run_id = uuid4()
    payload = dump_payload(JobKind.INSTALL, InstallPayload(run_id=str(run_id), cmdline="  a=1 "))
    assert payload == {"run_id": str(run_id), "cmdline": "a=1"}


def test_install_payload_omits_absent_cmdline() -> None:
    run_id = uuid4()
    payload = dump_payload(JobKind.INSTALL, InstallPayload(run_id=str(run_id)))
    assert payload == {"run_id": str(run_id)}


def test_install_payload_rejects_blank_cmdline() -> None:
    with pytest.raises(ValueError, match="cmdline must not be blank"):
        InstallPayload(run_id=str(uuid4()), cmdline="   ")


def test_install_payload_round_trips_crashkernel_size_and_range() -> None:
    # The crashkernel reservation (ADR-0300) is an opaque token: a size and a multi-range both ride.
    run_id = uuid4()
    payload = dump_payload(
        JobKind.INSTALL, InstallPayload(run_id=str(run_id), crashkernel="  512M ")
    )
    assert payload == {"run_id": str(run_id), "crashkernel": "512M"}
    ranged = InstallPayload(run_id=str(run_id), crashkernel="1G-2G:128M,2G-:256M")
    assert ranged.crashkernel == "1G-2G:128M,2G-:256M"


def test_install_payload_rejects_blank_crashkernel() -> None:
    with pytest.raises(ValueError, match="crashkernel must not be blank"):
        InstallPayload(run_id=str(uuid4()), crashkernel="   ")


def test_install_payload_rejects_crashkernel_with_internal_whitespace() -> None:
    # A space would inject an arbitrary extra kernel token into the space-joined cmdline.
    with pytest.raises(ValueError, match="crashkernel must be a single token"):
        InstallPayload(run_id=str(uuid4()), crashkernel="512M panic=1")


def test_install_payload_rejects_crashkernel_with_control_character() -> None:
    # A control char (e.g. NUL) is not ASCII whitespace, so it slips the token check; it would
    # reach the domain <cmdline> and fail XML serialization as an infrastructure error. Reject it.
    with pytest.raises(ValueError, match="crashkernel must be a single printable token"):
        InstallPayload(run_id=str(uuid4()), crashkernel="512M\x00panic")


def test_install_payload_rejects_crashkernel_with_token_prefix() -> None:
    # The caller passes the reservation argument, not the whole crashkernel= token.
    with pytest.raises(ValueError, match="crashkernel must not include the 'crashkernel=' prefix"):
        InstallPayload(run_id=str(uuid4()), crashkernel="crashkernel=512M")


def test_install_payload_decodes_legacy_run_only_payload() -> None:
    """A pre-#988 install job serialized as bare {run_id} decodes with cmdline=None."""
    now = datetime.now(UTC)
    run_id = uuid4()
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.INSTALL,
        payload={"run_id": str(run_id)},
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key=f"{run_id}:install",
    )
    decoded = load_payload(job, InstallPayload)
    assert decoded.run_id == str(run_id)
    assert decoded.cmdline is None


def test_dump_payload_omits_unset_optional_fields() -> None:
    run_id = uuid4()
    payload = dump_payload(
        JobKind.BUILD,
        {"run_id": str(run_id), "build_host_id": str(WORKER_LOCAL_ID), "cmdline": None},
    )
    assert "cmdline" not in payload
    assert payload == {"run_id": str(run_id), "build_host_id": str(WORKER_LOCAL_ID)}


def test_load_payload_rejects_unrelated_model_class_for_kind() -> None:
    now = datetime.now(UTC)
    run_id = uuid4()
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.BUILD,
        payload={"run_id": str(run_id), "build_host_id": str(WORKER_LOCAL_ID)},
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key="build",
    )
    with pytest.raises(
        PayloadValidationError, match="PowerPayload does not match build payload contract"
    ):
        load_payload(job, PowerPayload)


def test_load_payload_rejects_superclass_model_for_kind() -> None:
    now = datetime.now(UTC)
    system_id = uuid4()
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.REPROVISION,
        payload={"system_id": str(system_id), "profile_digest": "abc123"},
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key="reprovision",
    )
    with pytest.raises(
        PayloadValidationError, match="SystemPayload does not match reprovision payload contract"
    ):
        load_payload(job, SystemPayload)


def test_validation_error_joins_nested_loc_with_dots() -> None:
    with pytest.raises(PayloadValidationError, match=r"packages\.0:") as exc:
        dump_payload(
            JobKind.IMAGE_BUILD,
            {
                "provider": "local-libvirt",
                "name": "base",
                "arch": "x86_64",
                "releasever": "43",
                "source_image_digest": "sha256:" + "0" * 64,
                "format": "qcow2",
                "root_device": "/dev/vda",
                "packages": [123],
            },
        )
    assert "packages.0" in str(exc.value)


def test_dump_authorizing_accepts_plain_mapping() -> None:
    auth = dump_authorizing(
        cast(Any, {"principal": "alice", "agent_session": None, "project": "kernel-team"})
    )
    assert auth == {"principal": "alice", "agent_session": None, "project": "kernel-team"}


def test_payload_validation_rejects_wrong_shape_for_kind() -> None:
    with pytest.raises(PayloadValidationError, match="invalid build payload"):
        dump_payload(JobKind.BUILD, {"system_id": str(uuid4())})


def test_run_id_from_payload_returns_uuid_for_run_jobs() -> None:
    run_id = uuid4()

    assert run_id_from_payload(JobKind.INSTALL, {"run_id": str(run_id)}) == run_id
    assert run_id_from_payload(JobKind.BOOT, {"run_id": str(run_id)}) == run_id
    assert (
        run_id_from_payload(
            JobKind.CAPTURE_VMCORE,
            {"run_id": str(run_id), "method": "kdump"},
        )
        == run_id
    )


def test_run_id_from_payload_returns_none_for_system_jobs() -> None:
    assert run_id_from_payload(JobKind.PROVISION, {"system_id": str(uuid4())}) is None


def test_run_id_from_payload_returns_none_for_retired_build_jobs() -> None:
    run_id = uuid4()
    payload = {"run_id": str(run_id), "build_host_id": str(WORKER_LOCAL_ID)}
    assert run_id_from_payload(JobKind.BUILD, payload) is None
    assert run_id_from_payload(JobKind.BUILD_INSTALL_BOOT, payload) is None


def test_run_id_from_payload_rejects_malformed_run_jobs() -> None:
    with pytest.raises(PayloadValidationError, match="invalid install payload"):
        run_id_from_payload(JobKind.INSTALL, {"run_id": "not-a-uuid"})


def test_reprovision_payload_includes_profile_digest() -> None:
    system_id = uuid4()
    payload = dump_payload(
        JobKind.REPROVISION,
        {"system_id": str(system_id), "profile_digest": "abc123"},
    )

    decoded = ReprovisionPayload.model_validate(payload)

    assert decoded.system_id == str(system_id)
    assert decoded.profile_digest == "abc123"


def test_capture_payload_dumps_json_and_loads_enum() -> None:
    run_id = uuid4()
    now = datetime.now(UTC)

    payload = dump_payload(
        JobKind.CAPTURE_VMCORE,
        {"run_id": str(run_id), "method": "host_dump"},
    )
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.CAPTURE_VMCORE,
        payload=payload,
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key="capture",
    )

    decoded = load_payload(job, CaptureVmcorePayload)

    assert payload == {"run_id": str(run_id), "method": "host_dump"}
    assert decoded.run_id == str(run_id)
    assert decoded.method is CaptureMethod.HOST_DUMP


def test_power_payload_dumps_json_and_loads_enum() -> None:
    system_id = uuid4()
    now = datetime.now(UTC)

    payload = dump_payload(JobKind.POWER, {"system_id": str(system_id), "action": "reset"})
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.POWER,
        payload=payload,
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key="power",
    )

    decoded = load_payload(job, PowerPayload)

    assert payload == {"system_id": str(system_id), "action": "reset"}
    assert decoded.action is PowerAction.RESET


def test_sysrq_payload_dumps_json_and_loads_enum() -> None:
    system_id = uuid4()
    now = datetime.now(UTC)

    payload = dump_payload(
        JobKind.DIAGNOSTIC_SYSRQ,
        {"system_id": str(system_id), "command": "show_blocked_tasks"},
    )
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.DIAGNOSTIC_SYSRQ,
        payload=payload,
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key="sysrq",
    )

    decoded = load_payload(job, SysRqPayload)

    assert payload == {"system_id": str(system_id), "command": "show_blocked_tasks"}
    assert decoded.command is SysRqCommand.SHOW_BLOCKED_TASKS


def test_sysrq_payload_rejects_unknown_command() -> None:
    with pytest.raises(PayloadValidationError):
        dump_payload(JobKind.DIAGNOSTIC_SYSRQ, {"system_id": str(uuid4()), "command": "crash"})


def test_image_build_payload_serializes_private_scope() -> None:
    expires_at = datetime(2026, 1, 1, tzinfo=UTC)
    payload = dump_payload(
        JobKind.IMAGE_BUILD,
        {
            "provider": "local-libvirt",
            "name": "fedora-kdive-ready-43",
            "visibility": ImageVisibility.PRIVATE,
            "owner": "proj",
            "expires_at": expires_at,
        },
    )

    assert payload["visibility"] == "private"
    assert payload["owner"] == "proj"
    assert payload["expires_at"] == "2026-01-01T00:00:00Z"


def test_image_build_payload_rejects_catalog_derived_fields() -> None:
    """Identity fields now live on the catalog row, so the payload forbids them as extra inputs."""
    with pytest.raises(PayloadValidationError, match="invalid image_build payload"):
        dump_payload(
            JobKind.IMAGE_BUILD,
            {"provider": "local-libvirt", "name": "base", "format": "qcow2"},
        )


def test_image_build_payload_rejects_private_without_expiry() -> None:
    bad_scope = {
        "provider": "local-libvirt",
        "name": "fedora-kdive-ready-43",
        "visibility": "private",
        "owner": "proj",
    }
    with pytest.raises(PayloadValidationError, match="expires_at must be set iff"):
        dump_payload(JobKind.IMAGE_BUILD, bad_scope)


def test_authorizing_requires_project_at_enqueue_boundary() -> None:
    auth = dump_authorizing(
        Authorizing(principal="alice", agent_session="sess-1", project="kernel-team")
    )

    assert auth == {
        "principal": "alice",
        "agent_session": "sess-1",
        "project": "kernel-team",
    }


def test_authorizing_rejects_missing_project() -> None:
    with pytest.raises(PayloadValidationError, match="invalid job authorizing"):
        dump_authorizing(cast(Any, {"principal": "alice"}))


def test_diagnostics_worker_check_payload_roundtrips() -> None:
    payload = DiagnosticsWorkerCheckPayload(provider="remote-libvirt")
    dumped = dump_payload(JobKind.DIAGNOSTICS_WORKER_CHECK, payload)
    assert dumped == {"provider": "remote-libvirt"}

    now = datetime.now(UTC)
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.DIAGNOSTICS_WORKER_CHECK,
        payload=dumped,
        state=JobState.QUEUED,
        max_attempts=1,
        authorizing={
            "principal": "diagnostics",
            "agent_session": None,
            "project": "remote-libvirt",
        },
        dedup_key="diagnostics:remote-libvirt:x",
    )
    assert load_payload(job, DiagnosticsWorkerCheckPayload).provider == "remote-libvirt"


def test_authorize_ssh_key_payload_roundtrips() -> None:
    from kdive.jobs.payloads import _PAYLOAD_MODELS, AuthorizeSshKeyPayload

    payload = AuthorizeSshKeyPayload(
        system_id="11111111-2222-3333-4444-555555555555",
        public_key="ssh-ed25519 AAAAC3Nz agent@host",
    )
    assert payload.system_id == "11111111-2222-3333-4444-555555555555"
    assert payload.public_key == "ssh-ed25519 AAAAC3Nz agent@host"
    assert _PAYLOAD_MODELS[JobKind.AUTHORIZE_SSH_KEY] is AuthorizeSshKeyPayload


def test_check_ssh_reachable_payload_roundtrips() -> None:
    from kdive.jobs.payloads import _PAYLOAD_MODELS, CheckSshReachablePayload

    payload = CheckSshReachablePayload(system_id="11111111-2222-3333-4444-555555555555")
    assert payload.system_id == "11111111-2222-3333-4444-555555555555"
    assert _PAYLOAD_MODELS[JobKind.CHECK_SSH_REACHABLE] is CheckSshReachablePayload
