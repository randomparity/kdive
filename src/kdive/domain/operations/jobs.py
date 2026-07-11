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


DESTRUCTIVE_JOB_KINDS: frozenset[JobKind] = frozenset({JobKind.TEARDOWN, JobKind.FORCE_CRASH})
"""Job kinds gated by the destructive-operation admission gate (ADR-0130, ADR-0320, ADR-0326).

Power (ADR-0320) and reprovision (ADR-0326) both left this set: each is contributor
leaseholder lifecycle over its own transient resource, not destructive administration.
"""

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
        JobKind.BUILD,
        JobKind.INSTALL,
        JobKind.BOOT,
        JobKind.BUILD_INSTALL_BOOT,
        JobKind.POWER,
        JobKind.DIAGNOSTIC_SYSRQ,
        JobKind.CAPTURE_VMCORE,
        JobKind.AUTHORIZE_SSH_KEY,
        JobKind.CHECK_SSH_REACHABLE,
    }
)
"""Job kinds a contributor may cancel: the leaseholder-lifecycle jobs a contributor (or a lower
role) can itself enqueue, so cancelling one is acting on its own transient resource — matching
``runs.cancel`` over the build/install/boot lane (ADR-0320). The provision lane
(``provision``/``reprovision``) joined when it became contributor leaseholder control (ADR-0326).
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
    "CONTRIBUTOR_CANCELABLE_JOB_KINDS",
    "DESTRUCTIVE_JOB_KINDS",
    "OPT_IN_DESTRUCTIVE_JOB_KINDS",
    "Job",
    "JobAuthorizing",
    "JobKind",
    "PowerAction",
]
