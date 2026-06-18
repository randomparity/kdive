"""Durable lifecycle object vocabulary."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from kdive.domain._records import DomainBase, DomainModel
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.sizing import MB_PER_GB
from kdive.domain.pcie import PCIeClaim
from kdive.domain.profile_documents import (
    SerializedBuildProfile,
    SerializedExpectedBootFailure,
    SerializedProvisioningProfile,
)


class Attribution(DomainBase):
    """The attribution tuple recorded for tenant-owned objects."""

    principal: str
    agent_session: str | None = None
    project: str


class ExternalRef(DomainBase):
    """A mutable link to an external tracker (e.g. bugzilla, jira)."""

    tracker: str
    id: str
    url: str


class Allocation(DomainModel, Attribution):
    """A capacity- and budget-checked booking of a Resource."""

    resource_id: UUID | None = None
    state: AllocationState
    lease_expiry: datetime | None = None
    requested_vcpus: int | None = None
    requested_memory_gb: int | None = None
    requested_disk_gb: int | None = None
    shape: str | None = None
    active_started_at: datetime | None = None
    active_ended_at: datetime | None = None
    pcie_claim: list[PCIeClaim] = Field(default_factory=list)
    requested_pcie_specs: list[str] = Field(default_factory=list)
    requested_kind: ResourceKind | None = None
    requested_resource_id: UUID | None = None
    failure_category: ErrorCategory | None = None


class System(DomainModel, Attribution):
    """A provisioned target; one per Allocation."""

    allocation_id: UUID
    state: SystemState
    provisioning_profile: SerializedProvisioningProfile
    target_fingerprint: str | None = None
    domain_name: str | None = None
    shape: str | None = None


class Investigation(DomainModel, Attribution):
    """A project-scoped campaign grouping Runs toward a goal."""

    title: str
    description: str | None = None
    external_refs: list[ExternalRef] = Field(default_factory=list)
    state: InvestigationState
    last_run_at: datetime | None = None


class ExpectedBootFailure(DomainBase):
    """Run-scoped expected boot failure metadata (ADR-0064)."""

    kind: Literal["console_crash"]
    pattern: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=256)

    @field_validator("pattern")
    @classmethod
    def _literal_or_pattern(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("pattern must not contain NUL")
        terms = value.split("|")
        if any(term == "" for term in terms):
            raise ValueError("pattern contains an empty term")
        if len(terms) > 16:
            raise ValueError("pattern has too many terms")
        return value


class Run(DomainModel, Attribution):
    """One build/install/boot attempt — the join of a System and an Investigation."""

    investigation_id: UUID
    system_id: UUID
    state: RunState
    build_profile: SerializedBuildProfile
    expected_boot_failure: SerializedExpectedBootFailure | None = None
    kernel_ref: str | None = None
    debuginfo_ref: str | None = None
    failure_category: ErrorCategory | None = None
    failing_job_id: UUID | None = None


class DebugSession(DomainModel, Attribution):
    """One boot's debug attachment over a transport."""

    run_id: UUID
    state: DebugSessionState
    transport: str
    transport_handle: str | None = None
    worker_heartbeat_at: datetime | None = None


class SystemShape(DomainBase):
    """One named sizing preset in the shapes catalog (ADR-0067)."""

    name: str
    vcpus: int = Field(gt=0, strict=True)
    memory_mb: int = Field(gt=0, strict=True)
    disk_gb: int = Field(gt=0, strict=True)
    pcie_match: str | None = None
    updated_at: datetime

    @field_validator("memory_mb")
    @classmethod
    def _whole_gb(cls, value: int) -> int:
        if value % MB_PER_GB != 0:
            raise ValueError(f"memory_mb {value} must be a whole-GB multiple of {MB_PER_GB}")
        return value


__all__ = [
    "Allocation",
    "Attribution",
    "DebugSession",
    "ExpectedBootFailure",
    "ExternalRef",
    "Investigation",
    "Run",
    "System",
    "SystemShape",
]
