"""The System-wide external-boot admission matrix (ADR-0583).

One closed table decides every operation against the activation restricting a System. A
System with no restricting activation admits everything; a restricted one admits only what
its state's row lists, and the owning-Run-scoped subset only for the Run that owns the
activation.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import CategorizedError, ErrorCategory


class ExternalBootOperation(StrEnum):
    """Every operation the matrix decides."""

    RUN_CREATE = "run_create"
    RUN_BIND = "run_bind"
    RUN_CANCEL = "run_cancel"
    RUN_INSTALL = "run_install"
    RUN_BOOT = "run_boot"
    SYSTEM_REPROVISION = "system_reprovision"
    SYSTEM_POWER = "system_power"
    SYSTEM_SNAPSHOT = "system_snapshot"
    SYSTEM_SYSRQ = "system_sysrq"
    SYSTEM_TEARDOWN = "system_teardown"
    SYSTEM_AUTHORIZE_SSH_KEY = "system_authorize_ssh_key"
    SYSTEM_WATCH_CRASH = "system_watch_crash"
    FORCE_CRASH = "force_crash"
    CAPTURE_VMCORE = "capture_vmcore"
    CAPTURE_TRAFFIC = "capture_traffic"
    DEBUG_ATTACH = "debug_attach"
    DEBUG_DETACH = "debug_detach"
    EXTERNAL_BOOT_RELEASE = "external_boot_release"
    EXTERNAL_BOOT_RESOLVE_CONFLICT = "external_boot_resolve_conflict"


class ExternalBootDenied(CategorizedError):
    """A matrix denial, carrying the next actions `details` cannot carry.

    `ToolResponse.failure_from_error` reduces every `details` value to a JSON scalar, so a
    list of next actions there would be dropped. Binding them to the error also keeps a call
    site from reporting one state's actions against another state's denial.

    `project` travels with them because the actions must be RBAC-filtered before they reach
    the agent (ADR-0261) and two render frames — `runs.create`'s and `runs.bind`'s MCP
    adapters — hold no project of their own.
    """

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, object],
        next_actions: list[str],
        project: str,
    ) -> None:
        super().__init__(message, category=ErrorCategory.CONFLICT, details=details, terminal=True)
        self.next_actions = next_actions
        self.project = project


# Admitted in every restricting state. Teardown is the escape hatch out of a stuck activation;
# `DEBUG_DETACH` is the reversal of an attach the matrix itself admitted, so denying it would
# strand a `live` session and leave its provider transport open with no agent-reachable way to
# close it (#2117 review H1). It is also absent from `_OWNING_RUN_SCOPED`, because the release
# refuses on any live session of the System regardless of owning Run: fencing the detach to the
# activation's Run would wedge a session no caller could clear. Both departures from
# ADR-0583:348-351 are recorded in docs/debt/0006-external-boot-detach-departs-from-adr-0583.md.
_ALWAYS_ADMITTED = frozenset(
    {ExternalBootOperation.SYSTEM_TEARDOWN, ExternalBootOperation.DEBUG_DETACH}
)

# Total over the state enum, so no restricting activation can reach an undecided operation.
_ADMITTED: Mapping[ExternalBootActivationState, frozenset[ExternalBootOperation]] = {
    ExternalBootActivationState.PREPARING: _ALWAYS_ADMITTED,
    ExternalBootActivationState.PREPARED: _ALWAYS_ADMITTED,
    ExternalBootActivationState.ACTIVATING: _ALWAYS_ADMITTED,
    ExternalBootActivationState.RECOVERING: _ALWAYS_ADMITTED,
    ExternalBootActivationState.RECOVERY_CONFLICT: _ALWAYS_ADMITTED
    | {ExternalBootOperation.EXTERNAL_BOOT_RESOLVE_CONFLICT},
    ExternalBootActivationState.RECOVERY_FAILED: _ALWAYS_ADMITTED,
    ExternalBootActivationState.ACTIVE: _ALWAYS_ADMITTED
    | {
        ExternalBootOperation.EXTERNAL_BOOT_RELEASE,
        ExternalBootOperation.FORCE_CRASH,
        ExternalBootOperation.SYSTEM_WATCH_CRASH,
        ExternalBootOperation.CAPTURE_VMCORE,
        ExternalBootOperation.CAPTURE_TRAFFIC,
        ExternalBootOperation.DEBUG_ATTACH,
    },
    ExternalBootActivationState.RECOVERED: _ALWAYS_ADMITTED,
    ExternalBootActivationState.ABANDONED: _ALWAYS_ADMITTED,
}

# Admitted only for the Run that owns the activation. Teardown and conflict resolution are
# System-scoped by ADR-0583. `SYSTEM_WATCH_CRASH` and `FORCE_CRASH` are absent because their
# handlers carry no caller Run to fence against — ADR-0583 asks for that modifier and this
# matrix does not enforce it; the open deferral is
# docs/debt/0004-force-crash-owning-run-modifier-unenforced.md, owned by #2118.
_OWNING_RUN_SCOPED = frozenset(
    {
        ExternalBootOperation.EXTERNAL_BOOT_RELEASE,
        ExternalBootOperation.CAPTURE_VMCORE,
        ExternalBootOperation.CAPTURE_TRAFFIC,
        ExternalBootOperation.DEBUG_ATTACH,
    }
)

_STATE_NEXT_ACTION: Mapping[ExternalBootActivationState, str] = {
    ExternalBootActivationState.ACTIVE: "runs.release_external_boot",
    ExternalBootActivationState.RECOVERY_CONFLICT: "systems.teardown",
    ExternalBootActivationState.RECOVERY_FAILED: "systems.teardown",
}

# The `data.reason` every matrix denial carries, matching the convention the recovery contracts
# set (`no_active_activation`, `system_job_active`, `debug_session_active`, ...). A bounded
# scalar, so `safe_error_details` passes it through and it can label bounded telemetry.
DENIAL_REASON = "external_boot_restricted"

_REPOSITORY = ExternalBootActivationRepository()


async def check_external_boot_admission(
    conn: AsyncConnection,
    system_id: UUID,
    operation: ExternalBootOperation,
    *,
    project: str,
    run_id: UUID | None = None,
) -> None:
    """Admit `operation` on `system_id`, or raise :class:`ExternalBootDenied`.

    Returns `None` both when no activation restricts the System and when the matrix admits
    the operation against the restricting one: this is a guard, not a lookup.

    `project` decides nothing here; it is stamped onto the denial so the render frame can drop
    the next actions the caller cannot invoke for that project (ADR-0261). It is required
    rather than optional so a new call site cannot silently ship an unfiltered breadcrumb.
    """
    activation = await _REPOSITORY.get_restricting_for_system(conn, system_id)
    if activation is None:
        return
    if operation in _ADMITTED[activation.state] and (
        operation not in _OWNING_RUN_SCOPED or run_id == activation.run_id
    ):
        return
    next_action = _STATE_NEXT_ACTION.get(activation.state)
    raise ExternalBootDenied(
        f"{operation.value} is denied while external-boot activation {activation.id} "
        f"holds System {system_id} in {activation.state.value}",
        details={
            "reason": DENIAL_REASON,
            "activation_id": str(activation.id),
            "activation_state": activation.state.value,
            "owning_run_id": str(activation.run_id),
        },
        next_actions=["runs.get"] if next_action is None else ["runs.get", next_action],
        project=project,
    )


__all__ = [
    "DENIAL_REASON",
    "ExternalBootDenied",
    "ExternalBootOperation",
    "check_external_boot_admission",
]
