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
    BUILD = "build"
    INSTALL = "install"
    BOOT = "boot"
    FORCE_CRASH = "force_crash"
    POWER = "power"
    CAPTURE_VMCORE = "capture_vmcore"
    IMAGE_BUILD = "image_build"
    DIAGNOSTICS_WORKER_CHECK = "diagnostics_worker_check"
    BUILD_INSTALL_BOOT = "build_install_boot"


DESTRUCTIVE_JOB_KINDS: frozenset[JobKind] = frozenset(
    {JobKind.REPROVISION, JobKind.TEARDOWN, JobKind.FORCE_CRASH, JobKind.POWER}
)
"""Job kinds that require destructive-operation admission checks (ADR-0130)."""

BUILD_BEARING_JOB_KINDS: frozenset[JobKind] = frozenset({JobKind.BUILD, JobKind.BUILD_INSTALL_BOOT})
"""Job kinds that acquire a build-host lease and (on ephemeral hosts) a build VM.

Both ``build`` and ``build_install_boot`` hold the ``build_host_leases`` slot and the
ephemeral build-VM domain for the duration of their build phase. Reconciler liveness guards
and composite-admission conflict checks must treat both as live build holders (ADR-0268).
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
    "DESTRUCTIVE_JOB_KINDS",
    "Job",
    "JobAuthorizing",
    "JobKind",
    "PowerAction",
]
