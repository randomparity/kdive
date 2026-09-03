"""The closed external-boot admission matrix (ADR-0583, #2117).

The expected table is written transposed against the design's table — states admitting each
operation, rather than operations admitted in each state — so a transcription slip in the
implementation cannot be mirrored here.
"""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from mcp.types import ToolAnnotations
from psycopg import AsyncConnection

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.services.external_boot import (
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)
from kdive.services.external_boot import admission as admission_module
from tests.mcp.tool_registry_support import build_registered_tools
from tests.services.external_boot.conftest import SeedActivation, build_activation

_STATE = ExternalBootActivationState
_OP = ExternalBootOperation

_EVERY_RESTRICTED_STATE = frozenset(_STATE)
_ACTIVE_ONLY = frozenset({_STATE.ACTIVE})

_ADMITTING_STATES: dict[ExternalBootOperation, frozenset[ExternalBootActivationState]] = {
    _OP.SYSTEM_TEARDOWN: _EVERY_RESTRICTED_STATE,
    _OP.EXTERNAL_BOOT_RESOLVE_CONFLICT: frozenset({_STATE.RECOVERY_CONFLICT}),
    _OP.EXTERNAL_BOOT_RELEASE: _ACTIVE_ONLY,
    _OP.FORCE_CRASH: _ACTIVE_ONLY,
    _OP.SYSTEM_WATCH_CRASH: _ACTIVE_ONLY,
    _OP.CAPTURE_VMCORE: _ACTIVE_ONLY,
    _OP.CAPTURE_TRAFFIC: _ACTIVE_ONLY,
    _OP.DEBUG_ATTACH: _ACTIVE_ONLY,
    # Every restricting state, like teardown, and for any Run: a detach reverses an attach the
    # matrix admitted, so denying it would strand a live session and leak its transport — and the
    # release refuses on any live session of the System regardless of owning Run, so an
    # owning-Run fence here would wedge a session nobody could clear. Both departures from
    # ADR-0583:348-351 are recorded in
    # docs/debt/0006-external-boot-detach-departs-from-adr-0583.md
    _OP.DEBUG_DETACH: _EVERY_RESTRICTED_STATE,
}

_NEVER_ADMITTED = frozenset(
    {
        _OP.RUN_CREATE,
        _OP.RUN_BIND,
        _OP.RUN_CANCEL,
        _OP.RUN_INSTALL,
        _OP.RUN_BOOT,
        _OP.SYSTEM_REPROVISION,
        _OP.SYSTEM_POWER,
        _OP.SYSTEM_SNAPSHOT,
        _OP.SYSTEM_SYSRQ,
        _OP.SYSTEM_AUTHORIZE_SSH_KEY,
    }
)

_OWNING_RUN_SCOPED = frozenset(
    {
        _OP.EXTERNAL_BOOT_RELEASE,
        _OP.CAPTURE_VMCORE,
        _OP.CAPTURE_TRAFFIC,
        _OP.DEBUG_ATTACH,
    }
)

# Every (state, cleanup_complete) pair a restricting row can hold: no state is fully cleaned
# while it still restricts, except the two teardown states, whose cleanup does not release the
# System. `recovered` and `abandoned` with `cleanup_complete=true` stop restricting entirely and
# are covered by the no-activation case instead.
_RESTRICTING_CASES = [
    *((state, False) for state in _STATE),
    (_STATE.RECOVERY_CONFLICT, True),
    (_STATE.RECOVERY_FAILED, True),
]

_CALLERS = ("owning_run", "other_run", "no_run")


def _restricted_by(
    monkeypatch: pytest.MonkeyPatch, activation: ExternalBootActivation | None
) -> None:
    async def _get_restricting_for_system(
        _self: ExternalBootActivationRepository, _conn: AsyncConnection, _system_id: UUID
    ) -> ExternalBootActivation | None:
        return activation

    monkeypatch.setattr(
        ExternalBootActivationRepository,
        "get_restricting_for_system",
        _get_restricting_for_system,
    )


def _check(system_id: UUID, operation: ExternalBootOperation, run_id: UUID | None = None) -> None:
    asyncio.run(
        check_external_boot_admission(
            cast("AsyncConnection", None), system_id, operation, project="proj", run_id=run_id
        )
    )


def _denial(
    monkeypatch: pytest.MonkeyPatch,
    state: ExternalBootActivationState,
    operation: ExternalBootOperation,
) -> ExternalBootDenied:
    system_id, run_id = uuid4(), uuid4()
    activation = build_activation(
        activation_id=uuid4(), system_id=system_id, run_id=run_id, state=state
    )
    _restricted_by(monkeypatch, activation)
    with pytest.raises(ExternalBootDenied) as raised:
        _check(system_id, operation)
    assert raised.value.details == {
        # Every other refusal this surface raises carries a `reason`; the matrix denial matches
        # the convention, and the value is a bounded scalar `safe_error_details` passes through.
        "reason": admission_module.DENIAL_REASON,
        "activation_id": str(activation.id),
        "activation_state": state.value,
        "owning_run_id": str(run_id),
    }
    return raised.value


def test_the_admitted_table_is_total_over_the_activation_state_enum() -> None:
    """Totality, so no restricting activation can reach an undecided operation."""
    assert admission_module._ADMITTED.keys() == set(ExternalBootActivationState)
    assert admission_module._OWNING_RUN_SCOPED == _OWNING_RUN_SCOPED


def test_the_expected_table_decides_every_operation() -> None:
    assert _ADMITTING_STATES.keys().isdisjoint(_NEVER_ADMITTED)
    assert set(_ADMITTING_STATES) | _NEVER_ADMITTED == set(ExternalBootOperation)


@pytest.mark.parametrize("caller", _CALLERS)
@pytest.mark.parametrize("operation", list(ExternalBootOperation))
@pytest.mark.parametrize(("state", "cleanup_complete"), _RESTRICTING_CASES)
def test_a_restricting_activation_admits_only_the_table(
    monkeypatch: pytest.MonkeyPatch,
    state: ExternalBootActivationState,
    cleanup_complete: bool,
    operation: ExternalBootOperation,
    caller: str,
) -> None:
    system_id, owning_run_id = uuid4(), uuid4()
    activation = build_activation(
        activation_id=uuid4(),
        system_id=system_id,
        run_id=owning_run_id,
        state=state,
        cleanup_complete=cleanup_complete,
    )
    _restricted_by(monkeypatch, activation)
    caller_run = {"owning_run": owning_run_id, "other_run": uuid4(), "no_run": None}[caller]
    admitted = state in _ADMITTING_STATES.get(operation, frozenset()) and (
        operation not in _OWNING_RUN_SCOPED or caller_run == owning_run_id
    )
    if admitted:
        assert _check(system_id, operation, caller_run) is None
        return
    with pytest.raises(ExternalBootDenied) as raised:
        _check(system_id, operation, caller_run)
    assert raised.value.category is ErrorCategory.CONFLICT
    assert raised.value.terminal


@pytest.mark.parametrize("operation", list(ExternalBootOperation))
def test_no_restricting_activation_admits_every_operation(
    monkeypatch: pytest.MonkeyPatch, operation: ExternalBootOperation
) -> None:
    _restricted_by(monkeypatch, None)
    assert _check(uuid4(), operation) is None


@pytest.mark.parametrize(
    ("state", "operation", "next_actions"),
    [
        # `systems.teardown` rides with the release: until #2118 installs the executor the
        # release refuses every call, so alone it is a breadcrumb to a tool that changes nothing.
        (
            _STATE.ACTIVE,
            _OP.RUN_INSTALL,
            ["runs.get", "runs.release_external_boot", "systems.teardown"],
        ),
        (_STATE.RECOVERY_CONFLICT, _OP.SYSTEM_POWER, ["runs.get", "systems.teardown"]),
        (_STATE.RECOVERY_FAILED, _OP.RUN_CREATE, ["runs.get", "systems.teardown"]),
        (_STATE.PREPARING, _OP.RUN_BOOT, ["runs.get"]),
    ],
)
def test_denial_carries_the_state_s_next_actions_off_details(
    monkeypatch: pytest.MonkeyPatch,
    state: ExternalBootActivationState,
    operation: ExternalBootOperation,
    next_actions: list[str],
) -> None:
    denied = _denial(monkeypatch, state, operation)
    assert denied.next_actions == next_actions


def test_a_denial_carries_the_project_its_render_frame_filters_on() -> None:
    """ADR-0261 filtering happens at the render frame, which reads the project off the error."""
    system_id, run_id = uuid4(), uuid4()
    activation = build_activation(
        activation_id=uuid4(), system_id=system_id, run_id=run_id, state=_STATE.RECOVERY_CONFLICT
    )
    with pytest.MonkeyPatch.context() as patch:
        _restricted_by(patch, activation)
        with pytest.raises(ExternalBootDenied) as raised:
            _check(system_id, _OP.SYSTEM_POWER)
    assert raised.value.project == "proj"


_NON_ACTIVE_RESTRICTING = [state for state in _STATE if state is not _STATE.ACTIVE]


def _detach_check(
    monkeypatch: pytest.MonkeyPatch, state: ExternalBootActivationState, caller_run: UUID | None
) -> None:
    system_id, owning_run_id = uuid4(), uuid4()
    activation = build_activation(
        activation_id=uuid4(), system_id=system_id, run_id=owning_run_id, state=state
    )
    _restricted_by(monkeypatch, activation)
    _check(system_id, _OP.DEBUG_DETACH, owning_run_id if caller_run is None else caller_run)


@pytest.mark.parametrize("state", _NON_ACTIVE_RESTRICTING)
def test_the_owning_run_may_detach_in_every_restricting_state(
    monkeypatch: pytest.MonkeyPatch, state: ExternalBootActivationState
) -> None:
    """Denying the reversal of an admitted attach strands a live session and leaks its transport.

    The states here are the ones the matrix restricts *other* than ``active``; ``active`` is
    already covered by the table test.
    """
    assert _detach_check(monkeypatch, state, None) is None


@pytest.mark.parametrize("state", list(_STATE))
def test_a_different_run_may_also_detach_in_every_restricting_state(
    monkeypatch: pytest.MonkeyPatch, state: ExternalBootActivationState
) -> None:
    """Detach carries no owning-Run fence.

    See docs/debt/0006-external-boot-detach-departs-from-adr-0583.md.
    The release refuses on any live session of the System regardless of owning Run, so fencing
    the detach to the activation's Run would wedge a foreign session no caller could clear.
    ``DEBUG_ATTACH`` keeps its fence; the table test above covers that.
    """
    assert _detach_check(monkeypatch, state, uuid4()) is None


def test_get_restricting_for_system_sees_only_uncleaned_activations(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            uncleaned = await seeded_activation(conn, state=_STATE.RECOVERED)
            cleaned = await seeded_activation(conn, state=_STATE.RECOVERED, cleanup_complete=True)
            torn_down = await seeded_activation(
                conn, state=_STATE.RECOVERY_FAILED, cleanup_complete=True
            )
            await conn.commit()

            found = await repo.get_restricting_for_system(conn, uncleaned.system_id)
            assert found is not None
            assert (found.id, found.state, found.run_id) == (
                uncleaned.activation.id,
                _STATE.RECOVERED,
                uncleaned.run_id,
            )
            assert await repo.get_restricting_for_system(conn, cleaned.system_id) is None
            still_restricting = await repo.get_restricting_for_system(conn, torn_down.system_id)
            assert still_restricting is not None
            assert still_restricting.id == torn_down.activation.id
            assert await repo.get_restricting_for_system(conn, uuid4()) is None

    asyncio.run(_run())


# Every registered mutating tool, mapped to the operation its handler guards with. A tool here
# is enforced by `tests/services/external_boot/test_reverse_admission.py`, except the two
# external-boot recovery contracts, whose guard is enforced by
# `tests/services/external_boot/test_recovery_requests.py` (they are the reverse operations, so
# they have no reverse case of their own).
_GUARDED_TOOLS: dict[str, ExternalBootOperation] = {
    "control.capture_traffic": _OP.CAPTURE_TRAFFIC,
    "control.diagnostic_sysrq": _OP.SYSTEM_SYSRQ,
    "control.force_crash": _OP.FORCE_CRASH,
    "control.power": _OP.SYSTEM_POWER,
    "control.watch_for_crash": _OP.SYSTEM_WATCH_CRASH,
    "debug.end_session": _OP.DEBUG_DETACH,
    "debug.start_session": _OP.DEBUG_ATTACH,
    "runs.bind": _OP.RUN_BIND,
    "runs.boot": _OP.RUN_BOOT,
    "runs.cancel": _OP.RUN_CANCEL,
    "runs.create": _OP.RUN_CREATE,
    "runs.install": _OP.RUN_INSTALL,
    "runs.release_external_boot": _OP.EXTERNAL_BOOT_RELEASE,
    "systems.authorize_ssh_key": _OP.SYSTEM_AUTHORIZE_SSH_KEY,
    "systems.delete_snapshot": _OP.SYSTEM_SNAPSHOT,
    "systems.reprovision": _OP.SYSTEM_REPROVISION,
    "systems.resolve_external_boot_conflict": _OP.EXTERNAL_BOOT_RESOLVE_CONFLICT,
    "systems.restore": _OP.SYSTEM_SNAPSHOT,
    "systems.snapshot": _OP.SYSTEM_SNAPSHOT,
    "systems.teardown": _OP.SYSTEM_TEARDOWN,
    "vmcore.fetch": _OP.CAPTURE_VMCORE,
}

# The reviewed exemptions, each with the reason it decides nothing about a System's external
# boot. A new mutating tool belongs in one map or the other before it can ship.
_UNGUARDED_TOOLS: dict[str, str] = {
    "accounting.set_budget": "accounting state; touches no System",
    "accounting.set_quota": "accounting state; touches no System",
    "allocations.release": (
        "unguarded: the release path reads no System and takes no teardown precondition, so "
        "the matrix is not consulted on the Allocation wind-down; the gap is recorded in "
        "docs/debt/0007-allocation-release-bypasses-the-external-boot-matrix.md"
    ),
    "allocations.renew": "extends a lease; changes nothing about the guest",
    "allocations.request": "grants capacity before any System exists",
    "artifacts.create_investigation_upload": "mints an upload slot; touches no System",
    "artifacts.create_run_upload": "mints an upload slot; touches no System",
    "debug.advance": "in-session debugger control on an already-admitted attach",
    "debug.clear_breakpoint": "in-session debugger control on an already-admitted attach",
    "debug.clear_watchpoint": "in-session debugger control on an already-admitted attach",
    "debug.continue": "in-session debugger control on an already-admitted attach",
    "debug.interrupt": "in-session debugger control on an already-admitted attach",
    "debug.load_module_symbols": "in-session debugger control on an already-admitted attach",
    "debug.set_breakpoint": "in-session debugger control on an already-admitted attach",
    "debug.set_watchpoint": "in-session debugger control on an already-admitted attach",
    "images.delete": "image catalog administration; touches no System",
    "images.extend": "image catalog administration; touches no System",
    "images.prune_expired": "image catalog administration; touches no System",
    "images.publish": "image catalog administration; touches no System",
    "images.upload": "image catalog administration; touches no System",
    "introspect.script": "read-only guest introspection over an already-admitted transport",
    "inventory.clear_override": "operator inventory bookkeeping on a Resource, not a System",
    "investigations.close": "Investigation bookkeeping; closes through systems.teardown",
    "investigations.complete_rootfs_upload": "finishes an upload; touches no System",
    "investigations.link": "Investigation bookkeeping; touches no System",
    "investigations.open": "Investigation bookkeeping; touches no System",
    "investigations.set": "Investigation bookkeeping; touches no System",
    "investigations.unlink": "Investigation bookkeeping; touches no System",
    "jobs.cancel": (
        "cancelling is de-escalation: it starts no guest work and frees the System-held job "
        "that blocks a release, which is why it is the escape hatch that refusal names"
    ),
    "ops.diagnostics": "operator read-out; enqueues no System work",
    "ops.export_systems_toml": "operator read-out; enqueues no System work",
    "ops.force_release": "operator break-glass, the escape hatch a stuck activation needs",
    "ops.force_teardown": "operator break-glass, the escape hatch a stuck activation needs",
    "ops.reconcile_now": "operator break-glass reconcile; must run against a stuck activation",
    "ops.reconcile_systems": "operator break-glass reconcile; must run against a stuck activation",
    "ops.recover_build_use": "build-ledger repair; touches no System",
    "ops.resolve_recovery_orphan": (
        "repairs quarantined recovery objects, which are not the activation the matrix keys on"
    ),
    "ops.set_cost_class_coeff": "accounting configuration; touches no System",
    "ops.set_host_capacity": "capacity configuration on a Resource, not a System",
    "ops.set_queue_paused": "worker-lane configuration; touches no System",
    "resources.deregister": "Resource administration below the System layer",
    "resources.drain": "Resource administration below the System layer",
    "resources.register": "Resource administration below the System layer",
    "resources.renew": "Resource administration below the System layer",
    "resources.set_scheduling": "Resource administration below the System layer",
    "resources.set_status": "Resource administration below the System layer",
    "runs.complete_build": "records a build result; the guest is untouched",
    "runs.set": "Run metadata; the guest is untouched",
    "shapes.delete": "shape catalog administration; touches no System",
    "shapes.set": "shape catalog administration; touches no System",
    "systems.check_ssh_reachable": (
        "read-only liveness probe (read_only, VIEWER); ADR-0583 does not reject System "
        "observation in any restricted state"
    ),
    "systems.provision": "creates the System; no activation can restrict it yet",
    "tools.invoke": "gateway dispatcher; the re-entered inner tool carries its own guard",
}


def _registered_tools() -> dict[str, ToolAnnotations | None]:
    return {tool.name: tool.annotations for tool in build_registered_tools()}


def test_every_registered_mutating_tool_is_guarded_or_exempt() -> None:
    """The inverted gate: enumerate the registry, not the enum.

    A forward-only assertion (every ``ExternalBootOperation`` has a call site) is blind to the
    failure it exists to catch — a mutating tool nobody wrote an enum member for is absent from
    both sides of that comparison, which is how the 2026-09-02 scope audit found three unguarded
    handlers. Enumerating the registry instead makes a new mutating tool fail here until someone
    decides its admission.
    """
    decided = _GUARDED_TOOLS.keys() | _UNGUARDED_TOOLS.keys()
    mutating = {
        name
        for name, annotations in _registered_tools().items()
        if annotations is not None and annotations.readOnlyHint is False
    }
    assert mutating - decided == set()
    assert _GUARDED_TOOLS.keys().isdisjoint(_UNGUARDED_TOOLS)
    # Every decided name is a tool that actually exists, so a rename cannot leave a stale entry
    # silently exempting nothing.
    assert decided - _registered_tools().keys() == set()
    assert all(reason.strip() for reason in _UNGUARDED_TOOLS.values())


def test_every_operation_the_matrix_decides_has_a_guarded_tool() -> None:
    """The forward half: a member added without a call site fails too."""
    assert set(_GUARDED_TOOLS.values()) == set(ExternalBootOperation)
