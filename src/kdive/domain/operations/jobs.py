"""Job domain vocabulary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, TypedDict

from pydantic import Field

from kdive.domain._records import DomainModel
from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory


class JobKind(StrEnum):
    """The async job kinds — every tool that returns a ``{job_id}`` handle."""

    PROVISION = "provision"
    REPROVISION = "reprovision"
    TEARDOWN = "teardown"
    # BUILD / BUILD_INSTALL_BOOT are inert: the server-build lane was removed, but Postgres
    # cannot drop a value from an existing enum, so the members (and their payload shapes) stay.
    BUILD = "build"
    INSTALL = "install"
    BOOT = "boot"
    FORCE_CRASH = "force_crash"
    POWER = "power"
    CAPTURE_VMCORE = "capture_vmcore"
    IMAGE_BUILD = "image_build"
    DIAGNOSTICS_WORKER_CHECK = "diagnostics_worker_check"
    BUILD_INSTALL_BOOT = "build_install_boot"
    AUTHORIZE_SSH_KEY = "authorize_ssh_key"
    CONSOLE_ROTATE = "console_rotate"
    DIAGNOSTIC_SYSRQ = "diagnostic_sysrq"
    CHECK_SSH_REACHABLE = "check_ssh_reachable"
    WATCH_FOR_CRASH = "watch_for_crash"
    # System snapshot lifecycle (ADR-0378): capture and delete are async because an internal
    # memory snapshot writes/frees multi-GB of qcow2 clusters; restore fences via `restoring`.
    SNAPSHOT = "snapshot"
    RESTORE = "restore"
    DELETE_SNAPSHOT = "delete_snapshot"
    # Host-side network traffic capture (ADR-0385): async because a filter-dump runs for a bounded
    # window and stores a Run-owned pcap; contributor-cancelable so a stray capture can be stopped.
    CAPTURE_TRAFFIC = "capture_traffic"
    # Investigation-rootfs reclaim (ADR-0442): the reconciler enqueues, the worker unlinks. The
    # staged base lives in a tree the worker created and may not be writable by the reconciler's
    # user, so the filesystem half of the reclaim runs where the file was made.
    RECLAIM_INVESTIGATION_ROOTFS = "reclaim_investigation_rootfs"


RETIRED_JOB_KINDS: frozenset[JobKind] = frozenset({JobKind.BUILD, JobKind.BUILD_INSTALL_BOOT})
"""Persisted historical job kinds that are no longer valid active enqueue/filter choices."""

DEFAULT_JOB_DISPATCH_LANE = "default"
"""Dispatch lane used by the generic worker pool and all historical jobs."""

STATE_FENCED_JOB_DISPATCH_LANE = "state-fenced"
"""Dispatch lane for the kinds that fence a durable object at enqueue (ADR-0550)."""

STATE_FENCED_JOB_KINDS: frozenset[JobKind] = frozenset(
    {JobKind.RESTORE, JobKind.REPROVISION, JobKind.SNAPSHOT}
)
"""The kinds whose **enqueue transaction** writes a transient state another tool rejects on.

That is the whole rule, and it is checkable at the enqueue site: ``systems.restore`` sets
``SystemState.RESTORING``, ``systems.reprovision`` sets ``SystemState.REPROVISIONING``, and
``systems.snapshot`` inserts its ledger row as ``SnapshotState.CREATING`` — each in the same
transaction as the ``enqueue``, so the job's *queue wait* is time the object is unusable rather
than time the agent waits. These route to :data:`STATE_FENCED_JOB_DISPATCH_LANE` so that wait
cannot sit behind unrelated long work (ADR-0550, #1538).

Three near misses stay on the default lane. ``delete_snapshot`` writes no state — only its queued
presence is read, by ``_active_snapshot_op`` — so the rule admitting it would be "queued presence
is a rejection predicate", a property of the readers that cannot be evaluated where the routing
happens. ``teardown``'s handler writes the state, not its enqueue. ``provision`` has no
pre-existing object to fence.

Distinct from :data:`SYSTEM_FAILING_JOB_KINDS`, which is scoped to handlers that write
``SystemState.FAILED``: that answers *why is this System failed*, this answers *is this object
fenced while the job merely waits*. Neither set is a substitute for the other.
"""

ACTIVE_JOB_KINDS: frozenset[JobKind] = frozenset(
    kind for kind in JobKind if kind not in RETIRED_JOB_KINDS
)
"""Job kinds accepted by current tool affordances and production handler registration."""

OPT_IN_DESTRUCTIVE_JOB_KINDS: frozenset[JobKind] = frozenset({JobKind.FORCE_CRASH})
"""Destructive ops whose opt-in factor is resolved from a profile's ``destructive_ops`` list.
Only ``force_crash`` remains: ``teardown`` is gated by role only (ADR-0129); ``power`` is not
destructive; ``reprovision`` became contributor leaseholder lifecycle (ADR-0326) — so none of
the three is a valid ``destructive_ops`` token.
"""

SYSTEM_FAILING_JOB_KINDS: frozenset[JobKind] = frozenset(
    {JobKind.PROVISION, JobKind.REPROVISION, JobKind.RESTORE}
)
"""The job kinds whose handlers write ``SystemState.FAILED`` (ADR-0454 §2).

``systems.get`` reads the newest dead-lettered job of one of these kinds to report *why* a
System is ``failed``, since the ``systems`` table carries no failure category of its own. The
set is deliberately narrow: a System also accumulates failed jobs of kinds that never touch its
state (a failed ``check_ssh_reachable`` on a healthy System is routine), and reading one of
those as the reason would be a confident mis-attribution. Adding a kind here is only correct
alongside a handler that actually drives the System to ``failed``.
"""

CONTRIBUTOR_CANCELABLE_JOB_KINDS: frozenset[JobKind] = frozenset(
    {
        JobKind.PROVISION,
        JobKind.REPROVISION,
        JobKind.INSTALL,
        JobKind.BOOT,
        JobKind.POWER,
        JobKind.DIAGNOSTIC_SYSRQ,
        JobKind.CAPTURE_VMCORE,
        JobKind.AUTHORIZE_SSH_KEY,
        JobKind.CHECK_SSH_REACHABLE,
        JobKind.WATCH_FOR_CRASH,
        JobKind.SNAPSHOT,
        JobKind.RESTORE,
        JobKind.DELETE_SNAPSHOT,
        JobKind.CAPTURE_TRAFFIC,
    }
)
"""Job kinds a contributor may cancel: the leaseholder-lifecycle jobs a contributor (or a lower
role) can itself enqueue, so cancelling one is acting on its own transient resource — matching
``runs.cancel`` over the install/boot lane (ADR-0320). The provision lane
(``provision``/``reprovision``) joined when it became contributor leaseholder control (ADR-0326).
Retired server-build kinds are intentionally absent: historical rows remain readable, but no
active handler is registered for ``build`` or ``build_install_boot``.
``jobs.cancel`` requires operator for every other kind: the destructive kinds
(``teardown``/``force_crash``) and the platform/internal kinds
(image_build/diagnostics_worker_check/console_rotate/reclaim_investigation_rootfs). The gate fails
closed — a kind absent
here requires operator — so a newly added privileged kind is never silently
contributor-cancellable.
"""


class PowerAction(StrEnum):
    """Power operations accepted by the durable control-plane job contract.

    ``resume`` (ADR-0378) resumes a ``paused`` System's suspended vCPUs (``virDomainResume``)
    back to ``ready``; it is the one action admitted from a non-``ready`` state, and unlike the
    others it moves System state (``paused → ready``).
    """

    ON = "on"
    OFF = "off"
    CYCLE = "cycle"
    RESET = "reset"
    RESUME = "resume"


class JobAuthorizing(TypedDict):
    principal: str
    agent_session: str | None
    project: str


class Job(DomainModel):
    """A durable unit of async work; the ``jobs`` table is the queue."""

    kind: JobKind
    dispatch_lane: str = DEFAULT_JOB_DISPATCH_LANE
    payload: dict[str, Any] = Field(default_factory=dict)
    state: JobState
    attempt: int = 0
    max_attempts: int
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    result_ref: str | None = None
    error_category: ErrorCategory | None = None
    failure_context: dict[str, str] = Field(default_factory=dict)
    authorizing: JobAuthorizing
    dedup_key: str


def dispatch_lane_for_kind(kind: JobKind) -> str:
    """Return the dispatch lane ``kind`` is admitted onto (ADR-0550).

    Total over :class:`JobKind`. This is the single point that decides routing: ``enqueue``
    derives the lane here rather than accepting one from its caller, so a new kind lands on the
    lane its membership implies instead of on whichever lane fourteen call sites remembered.
    """
    if kind in STATE_FENCED_JOB_KINDS:
        return STATE_FENCED_JOB_DISPATCH_LANE
    return DEFAULT_JOB_DISPATCH_LANE


__all__ = [
    "ACTIVE_JOB_KINDS",
    "CONTRIBUTOR_CANCELABLE_JOB_KINDS",
    "DEFAULT_JOB_DISPATCH_LANE",
    "OPT_IN_DESTRUCTIVE_JOB_KINDS",
    "RETIRED_JOB_KINDS",
    "STATE_FENCED_JOB_DISPATCH_LANE",
    "STATE_FENCED_JOB_KINDS",
    "SYSTEM_FAILING_JOB_KINDS",
    "Job",
    "JobAuthorizing",
    "JobKind",
    "PowerAction",
    "dispatch_lane_for_kind",
]
