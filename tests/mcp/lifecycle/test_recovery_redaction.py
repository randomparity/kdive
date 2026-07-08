"""No free-form profile reference string leaks into a recovery envelope (#568, ADR-0180)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from kdive.domain.capacity.state import RunState, SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.records import Run, System
from kdive.mcp.responses import JsonValue
from kdive.mcp.tools.lifecycle.runs.common import envelope_for_run
from kdive.mcp.tools.lifecycle.systems.view import system_envelope
from kdive.services.runs.steps import StepProgress

_PLANTED = "PLANTED-DO-NOT-LEAK"  # a benign marker; the test asserts it never leaks
_DT = datetime(2026, 6, 18, tzinfo=UTC)
_CREATED = datetime(2026, 6, 18, 1, tzinfo=UTC)
_UPDATED = datetime(2026, 6, 18, 2, tzinfo=UTC)
_RUN_TEMPLATE = Run(
    id=uuid4(),
    created_at=_DT,
    updated_at=_DT,
    principal="u",
    project="proj",
    investigation_id=uuid4(),
    system_id=None,
    target_kind=ResourceKind.LOCAL_LIBVIRT,
    state=RunState.RUNNING,
    build_profile={"source": "server", "build_host": "build-1"},
)


def _system(
    *,
    state: SystemState = SystemState.READY,
    shape: str | None = "small",
) -> System:
    return System(
        id=uuid4(),
        created_at=_CREATED,
        updated_at=_UPDATED,
        principal="u",
        project="proj",
        allocation_id=uuid4(),
        state=state,
        shape=shape,
        provisioning_profile={
            "schema_version": 1,
            "arch": "x86_64",
            "boot_method": "direct-kernel",
            "vcpu": 2,
            "memory_mb": 4096,
            "disk_gb": 20,
        },
    )


def _fresh_run(update: dict[str, object]) -> Run:
    return _RUN_TEMPLATE.model_copy(update={"id": uuid4(), "investigation_id": uuid4(), **update})


def _bound_run(state: RunState = RunState.RUNNING, *, system_id: UUID | None = None) -> Run:
    return _fresh_run({"state": state, "system_id": system_id or uuid4()})


def _unbound_run(state: RunState = RunState.RUNNING) -> Run:
    return _fresh_run({"state": state, "system_id": None})


def _run_with_artifact_refs(*, kernel_ref: str, debuginfo_ref: str) -> Run:
    return _bound_run(RunState.SUCCEEDED).model_copy(
        update={"kernel_ref": kernel_ref, "debuginfo_ref": debuginfo_ref}
    )


def _run_with_expected_boot_failure(detail: dict[str, object]) -> Run:
    return _bound_run().model_copy(update={"expected_boot_failure": detail})


def test_system_envelope_excludes_kernel_source_ref() -> None:
    # The provisioning-profile summary allowlists only shape fields, so a secret-bearing field
    # like kernel_source_ref never reaches the recovery envelope.
    system = System(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="u",
        project="proj",
        allocation_id=uuid4(),
        state=SystemState.READY,
        provisioning_profile={
            "schema_version": 1,
            "arch": "x86_64",
            "boot_method": "direct-kernel",
            "vcpu": 2,
            "memory_mb": 4096,
            "disk_gb": 20,
            "kernel_source_ref": f"git+https://h/{_PLANTED}/r.git",
            "provider": {"local-libvirt": {}},
        },
    )
    resp = system_envelope(system, resource_kind="local-libvirt", resource_id=str(uuid4()))
    assert _PLANTED not in resp.model_dump_json()


def test_run_envelope_excludes_git_remote_token() -> None:
    run = Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="u",
        project="proj",
        investigation_id=uuid4(),
        system_id=None,
        target_kind=ResourceKind.LOCAL_LIBVIRT,
        state=RunState.SUCCEEDED,
        build_profile={
            "source": "server",
            "build_host": "build-1",
            # Secret embedded as a path segment (not basic-auth userinfo, which the
            # detect-secrets hook would flag); the test asserts it does not leak.
            "kernel_source_ref": {"git": {"remote": f"https://h/{_PLANTED}/r.git", "ref": "main"}},
        },
    )
    resp = envelope_for_run(run)
    assert _PLANTED not in resp.model_dump_json()


# --- envelope_for_run: non-failed envelope shape -------------------------------------


def test_envelope_created_run_advances_to_build() -> None:
    run = _bound_run(RunState.CREATED)

    resp = envelope_for_run(run)

    assert resp.status == "created"
    assert resp.object_id == str(run.id)
    assert resp.suggested_next_actions == ["runs.get", "runs.build"]


def test_envelope_running_run_advances_to_build() -> None:
    run = _bound_run()

    resp = envelope_for_run(run)

    assert resp.suggested_next_actions == ["runs.get", "runs.build"]


def test_envelope_canceled_run_only_offers_get() -> None:
    run = _bound_run(RunState.CANCELED)

    resp = envelope_for_run(run)

    assert resp.suggested_next_actions == ["runs.get"]


def test_envelope_succeeded_run_surfaces_steps_and_next_step() -> None:
    run = _bound_run(RunState.SUCCEEDED)
    progress = StepProgress(install="succeeded", boot="succeeded", boot_outcome=None)

    resp = envelope_for_run(run, step_progress=progress)

    assert resp.suggested_next_actions == ["runs.get", "debug.start_session"]
    assert resp.data["steps"] == {"build": "succeeded", "install": "succeeded", "boot": "succeeded"}


def test_envelope_succeeded_run_without_progress_omits_steps_and_installs_next() -> None:
    run = _bound_run(RunState.SUCCEEDED)

    resp = envelope_for_run(run)

    # No step ledger -> install is the next step and no steps map is surfaced.
    assert resp.suggested_next_actions == ["runs.get", "runs.install"]
    assert "steps" not in resp.data


def test_envelope_data_carries_run_identity_fields() -> None:
    system_id = uuid4()
    run = _bound_run(system_id=system_id)

    data = envelope_for_run(run).data

    assert data["project"] == "proj"
    assert data["target_kind"] == "local-libvirt"
    assert data["system_id"] == str(system_id)
    assert data["active_debug_session_ids"] == []
    assert data["investigation_id"] == str(run.investigation_id)


def test_envelope_succeeded_unbound_run_must_bind_first() -> None:
    # A SUCCEEDED Run with no bound System advances to runs.bind before install.
    run = _unbound_run(RunState.SUCCEEDED)

    resp = envelope_for_run(run)

    assert resp.suggested_next_actions == ["runs.get", "runs.bind"]


def test_envelope_unbound_run_reports_null_system_id() -> None:
    run = _unbound_run()

    assert envelope_for_run(run).data["system_id"] is None


def test_envelope_surfaces_active_debug_session_ids() -> None:
    run = _bound_run()

    data = envelope_for_run(run, active_debug_session_ids=["sess-1", "sess-2"]).data

    assert data["active_debug_session_ids"] == ["sess-1", "sess-2"]


def test_envelope_includes_required_cmdline_when_supplied() -> None:
    run = _bound_run()

    with_cmdline = envelope_for_run(run, required_cmdline="console=ttyS0").data
    without_cmdline = envelope_for_run(run).data

    assert with_cmdline["required_cmdline"] == "console=ttyS0"
    assert "required_cmdline" not in without_cmdline


def test_envelope_refs_carry_artifact_keys() -> None:
    run = _run_with_artifact_refs(
        kernel_ref="s3://b/kernel",
        debuginfo_ref="s3://b/debuginfo",
    )

    resp = envelope_for_run(run, step_progress=None)

    assert resp.refs == {"kernel": "s3://b/kernel", "debuginfo": "s3://b/debuginfo"}


def test_envelope_surfaces_expected_boot_failure_kind() -> None:
    run = _run_with_expected_boot_failure({"kind": "panic"})

    data = envelope_for_run(run).data

    assert data["expected_boot_failure"] == "panic"
    assert data["expected_boot_failure_detail"] == {"kind": "panic"}


# --- system_envelope: envelope shape -------------------------------------------------


def test_system_envelope_ready_carries_identity_and_actions() -> None:
    system = _system(state=SystemState.READY, shape="small")

    resp = system_envelope(system)

    assert resp.object_id == str(system.id)
    assert resp.status == "ready"
    assert resp.suggested_next_actions == ["systems.get", "systems.teardown"]
    assert resp.data["project"] == "proj"
    assert resp.data["allocation_id"] == str(system.allocation_id)
    assert resp.data["shape"] == "small"
    assert resp.data["created_at"] == _CREATED.isoformat()
    assert resp.data["updated_at"] == _UPDATED.isoformat()
    # Provisioning summary is folded into data.
    assert resp.data["arch"] == "x86_64"


def test_system_envelope_omits_get_only_fields_by_default() -> None:
    system = _system()

    data = system_envelope(system).data

    assert "resource_kind" not in data
    assert "resource_id" not in data
    assert "active_debug_session_ids" not in data
    assert "active_run" not in data


def test_system_envelope_includes_placement_when_supplied() -> None:
    system = _system()

    data = system_envelope(system, resource_kind="local-libvirt", resource_id="res-1").data

    assert data["resource_kind"] == "local-libvirt"
    assert data["resource_id"] == "res-1"


def test_system_envelope_includes_get_only_recovery_fields() -> None:
    system = _system()
    active_run: dict[str, JsonValue] = {"id": "run-1", "state": "running"}

    data = system_envelope(
        system,
        active_debug_session_ids=["sess-1"],
        active_run=active_run,
    ).data

    assert data["active_debug_session_ids"] == ["sess-1"]
    assert data["active_run"] == active_run


def test_system_envelope_failed_state_is_failure_envelope() -> None:
    system = _system(state=SystemState.FAILED)

    resp = system_envelope(system, resource_kind="local-libvirt", resource_id="res-1")

    assert resp.status == "error"
    assert resp.error_category == "infrastructure_failure"
    assert resp.data["current_status"] == "failed"
    # The success-only teardown action is not offered on a failure envelope.
    assert resp.suggested_next_actions == []
