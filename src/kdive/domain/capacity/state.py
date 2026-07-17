"""Durable object lifecycles and the transition guard (ADR-0003).

A :class:`~enum.StrEnum` per durable object plus :func:`can_transition`, the guard
the repository layer consults before persisting a state change. The legal edges
encode the current durable-object state machines.

Two readings of that table are pinned here because its notation is ambiguous:

* **System ``ready`` is not terminal.** The table bolds ``ready`` but the prose,
  the walking-skeleton path, and ``force_crash`` all transition out of it
  (``ready → crashing → crashed`` and ``ready → torn_down``); only ``torn_down`` and
  ``failed`` are terminal.
* **DebugSession is forward-only.** The conceptual model is ``attach ↔ live ↔
  detached``, but runtime tools do not reattach or step backward. The platform
  drives ``attach → live → detached`` with ``detached`` terminal. ``attach → detached``
  is also legal: a failed attach aborts straight to the terminal rather than stranding
  the row in ``attach``.

``failed`` is reachable from every non-terminal state of the objects that carry
it. Resource health (``available``/``degraded``/``offline``) is not a lifecycle —
it flips freely between its three values.
"""

from __future__ import annotations

from enum import StrEnum


class ResourceStatus(StrEnum):
    """Health of a registered resource host (free transitions among the three)."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    OFFLINE = "offline"


# Provenance: granted→releasing ADR-0023; expiry sweep ADR-0036/0040; queued cancel ADR-0069.
class AllocationState(StrEnum):
    """Capacity- and budget-checked allocation lifecycle.

    ``granted → releasing`` lets an admitted-but-unprovisioned allocation be released
    without first reaching ``active`` (which provisioning produces).
    ``granted/active → expired`` is the reconciler sweep reclaiming a lease past its
    window; ``expired`` is terminal and distinct from ``failed``.
    ``requested → released`` is the cancellation edge for a queued request: a
    queued row was never reserved, so it releases directly to ``released`` without the
    ``releasing`` hop and writes no ledger credit.
    """

    REQUESTED = "requested"
    GRANTED = "granted"
    ACTIVE = "active"
    RELEASING = "releasing"
    RELEASED = "released"
    EXPIRED = "expired"
    FAILED = "failed"


# Provenance: reprovision-in-place ADR-0038; snapshot restore/pause ADR-0378.
class SystemState(StrEnum):
    """A provisioned target's lifecycle.

    Reprovision-in-place cycles a ready System through
    ``ready → reprovisioning → ready`` on the same row; an interrupted reprovision fails to
    ``reprovisioning → failed``. ``defined → torn_down`` lets an abandoned
    create-without-provision System be torn down without first advancing to
    ``provisioning``. ``force_crash`` cycles a ready System ``ready → crashing → crashed``: the
    ``crashing`` marker is committed before the physical NMI so the power path (which refuses any
    non-``ready`` System) cannot reset the guest mid-crash.

    Snapshot restore fences a ready System through
    ``ready → restoring → {ready|paused|failed}``: a running restore returns to ``ready``, a
    ``start_paused`` restore lands in ``paused`` (the guest's vCPUs are suspended, awaiting
    ``control.power(resume)`` back to ``ready``), and an interrupted/failed revert goes to
    ``failed``. ``paused`` is a resting state, not ``ready``, so the ``ready ⇒ running`` invariant
    the snapshot/SSH tools rely on holds; a ``paused`` System can be torn down without resuming.
    """

    DEFINED = "defined"
    PROVISIONING = "provisioning"
    READY = "ready"
    REPROVISIONING = "reprovisioning"
    RESTORING = "restoring"
    PAUSED = "paused"
    CRASHING = "crashing"
    CRASHED = "crashed"
    TORN_DOWN = "torn_down"
    FAILED = "failed"


class InvestigationState(StrEnum):
    """Project-scoped campaign; becomes ``active`` on its first Run."""

    OPEN = "open"
    ACTIVE = "active"
    CLOSED = "closed"
    ABANDONED = "abandoned"


# Provenance: install/boot progress in run_steps ledger ADR-0179.
class RunState(StrEnum):
    """Build-phase lifecycle of a Run; one build per Run, a failed step is terminal.

    ``succeeded`` means the **build** step succeeded — not that the kernel is installed or
    booted. Install and boot progress live in the ``run_steps`` ledger and are surfaced by
    ``runs.get`` as ``data.steps``. A failed install/boot step fails the Run to
    ``failed``.
    """

    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class DebugSessionState(StrEnum):
    """One boot = one session; ends at reboot/crash (``detached``)."""

    ATTACH = "attach"
    LIVE = "live"
    DETACHED = "detached"


# Provenance: System snapshot child ledger ADR-0378.
class SnapshotState(StrEnum):
    """A ``snapshots`` ledger row's lifecycle (child of a System).

    A row is minted ``creating`` when a ``systems.snapshot`` job is enqueued and resolves to
    ``available`` on success or ``failed`` on error/cancel. ``available → failed`` covers a
    later invalidation. ``failed`` is terminal; deletion is row removal, not a state.
    """

    CREATING = "creating"
    AVAILABLE = "available"
    FAILED = "failed"


class JobState(StrEnum):
    """Durable job lifecycle; ``running → queued`` is a bounded-retry requeue."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class IllegalTransition(ValueError):
    """Raised when a state change is not permitted by the guard table.

    A programming/invariant error, distinct from the operational failures in
    :class:`kdive.domain.errors.ErrorCategory`.
    """


# Adjacency nested by enum class: each member maps to its allowed successors
# (terminals -> empty). StrEnum members hash by their string value, so members of
# different enums that share a value (e.g. several `"failed"`) collide in a single
# dict; nesting by the class object (hashed by identity) keeps each lifecycle's
# table isolated.
_TRANSITIONS: dict[type[StrEnum], dict[StrEnum, frozenset[StrEnum]]] = {
    ResourceStatus: {
        ResourceStatus.AVAILABLE: frozenset({ResourceStatus.DEGRADED, ResourceStatus.OFFLINE}),
        ResourceStatus.DEGRADED: frozenset({ResourceStatus.AVAILABLE, ResourceStatus.OFFLINE}),
        ResourceStatus.OFFLINE: frozenset({ResourceStatus.AVAILABLE, ResourceStatus.DEGRADED}),
    },
    AllocationState: {
        AllocationState.REQUESTED: frozenset(
            {AllocationState.GRANTED, AllocationState.RELEASED, AllocationState.FAILED}
        ),
        AllocationState.GRANTED: frozenset(
            {
                AllocationState.ACTIVE,
                AllocationState.RELEASING,
                AllocationState.EXPIRED,
                AllocationState.FAILED,
            }
        ),
        AllocationState.ACTIVE: frozenset(
            {AllocationState.RELEASING, AllocationState.EXPIRED, AllocationState.FAILED}
        ),
        AllocationState.RELEASING: frozenset({AllocationState.RELEASED, AllocationState.FAILED}),
        AllocationState.RELEASED: frozenset(),
        AllocationState.EXPIRED: frozenset(),
        AllocationState.FAILED: frozenset(),
    },
    SystemState: {
        SystemState.DEFINED: frozenset(
            {SystemState.PROVISIONING, SystemState.TORN_DOWN, SystemState.FAILED}
        ),
        SystemState.PROVISIONING: frozenset(
            {SystemState.READY, SystemState.FAILED, SystemState.TORN_DOWN}
        ),
        SystemState.READY: frozenset(
            {
                SystemState.CRASHING,
                SystemState.TORN_DOWN,
                SystemState.REPROVISIONING,
                SystemState.RESTORING,
                SystemState.FAILED,
            }
        ),
        SystemState.REPROVISIONING: frozenset({SystemState.READY, SystemState.FAILED}),
        SystemState.RESTORING: frozenset(
            {SystemState.READY, SystemState.PAUSED, SystemState.FAILED}
        ),
        SystemState.PAUSED: frozenset(
            {SystemState.READY, SystemState.TORN_DOWN, SystemState.FAILED}
        ),
        SystemState.CRASHING: frozenset(
            {SystemState.CRASHED, SystemState.FAILED, SystemState.TORN_DOWN}
        ),
        SystemState.CRASHED: frozenset({SystemState.TORN_DOWN, SystemState.FAILED}),
        SystemState.TORN_DOWN: frozenset(),
        SystemState.FAILED: frozenset(),
    },
    InvestigationState: {
        InvestigationState.OPEN: frozenset(
            {InvestigationState.ACTIVE, InvestigationState.CLOSED, InvestigationState.ABANDONED}
        ),
        InvestigationState.ACTIVE: frozenset(
            {InvestigationState.CLOSED, InvestigationState.ABANDONED}
        ),
        InvestigationState.CLOSED: frozenset(),
        InvestigationState.ABANDONED: frozenset(),
    },
    RunState: {
        RunState.CREATED: frozenset({RunState.RUNNING, RunState.CANCELED}),
        RunState.RUNNING: frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELED}),
        RunState.SUCCEEDED: frozenset(),
        RunState.FAILED: frozenset(),
        RunState.CANCELED: frozenset(),
    },
    DebugSessionState: {
        DebugSessionState.ATTACH: frozenset({DebugSessionState.LIVE, DebugSessionState.DETACHED}),
        DebugSessionState.LIVE: frozenset({DebugSessionState.DETACHED}),
        DebugSessionState.DETACHED: frozenset(),
    },
    SnapshotState: {
        SnapshotState.CREATING: frozenset({SnapshotState.AVAILABLE, SnapshotState.FAILED}),
        SnapshotState.AVAILABLE: frozenset({SnapshotState.FAILED}),
        SnapshotState.FAILED: frozenset(),
    },
    JobState: {
        JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELED}),
        JobState.RUNNING: frozenset(
            {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED, JobState.QUEUED}
        ),
        JobState.SUCCEEDED: frozenset(),
        JobState.FAILED: frozenset(),
        JobState.CANCELED: frozenset(),
    },
}


def can_transition(frm: StrEnum, to: StrEnum) -> bool:
    """Report whether ``frm → to`` is a legal transition.

    Args:
        frm: The current state.
        to: The proposed next state. Must be a member of the same enum as ``frm``.

    Returns:
        ``True`` if the edge is in the guard table; ``False`` otherwise (including
        self-transitions, which are never legal).

    Raises:
        TypeError: If ``frm`` and ``to`` are different enums, or ``frm`` is not a
            known lifecycle state — both signal a caller bug, not a denied
            transition.
    """
    if type(frm) is not type(to):
        raise TypeError(
            f"cannot compare states across {type(frm).__name__} and {type(to).__name__}"
        )
    table = _TRANSITIONS.get(type(frm))
    if table is None:
        raise TypeError(f"{type(frm).__name__} is not a known lifecycle")
    successors = table.get(frm)
    if successors is None:
        raise TypeError(f"{type(frm).__name__}.{frm.name} is not a known lifecycle state")
    return to in successors


def ensure_transition(frm: StrEnum, to: StrEnum) -> None:
    """Assert ``frm → to`` is legal, raising :class:`IllegalTransition` if not.

    Args:
        frm: The current state.
        to: The proposed next state.

    Raises:
        IllegalTransition: If the transition is not permitted.
        TypeError: Propagated from :func:`can_transition` for cross-enum or
            unknown-state misuse.
    """
    if not can_transition(frm, to):
        raise IllegalTransition(f"illegal {type(frm).__name__} transition: {frm} -> {to}")
