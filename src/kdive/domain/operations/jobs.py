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


RETIRED_JOB_KINDS: frozenset[JobKind] = frozenset({JobKind.BUILD, JobKind.BUILD_INSTALL_BOOT})
"""Persisted historical job kinds that are no longer valid active enqueue/filter choices."""

DEFAULT_JOB_DISPATCH_LANE = "default"
"""Dispatch lane used by the generic worker pool and all historical jobs."""

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
(image_build/diagnostics_worker_check/console_rotate). The gate fails closed — a kind absent
here requires operator — so a newly added privileged kind is never silently
contributor-cancellable.
"""


class PowerAction(StrEnum):
    """Power operations accepted by the durable control-plane job contract."""

    ON = "on"
    OFF = "off"
    CYCLE = "cycle"
    RESET = "reset"


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


__all__ = [
    "ACTIVE_JOB_KINDS",
    "CONTRIBUTOR_CANCELABLE_JOB_KINDS",
    "DEFAULT_JOB_DISPATCH_LANE",
    "OPT_IN_DESTRUCTIVE_JOB_KINDS",
    "RETIRED_JOB_KINDS",
    "Job",
    "JobAuthorizing",
    "JobKind",
    "PowerAction",
]
