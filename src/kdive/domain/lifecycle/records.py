"""Durable lifecycle object vocabulary."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from kdive.domain._records import DomainBase, DomainModel
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SnapshotState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.crash_signatures import CRASH_SIGNATURE_PRESETS
from kdive.domain.lifecycle.sizing import MB_PER_GB
from kdive.domain.pcie import PCIeClaim
from kdive.domain.profile_documents import (
    SerializedBuildProfile,
    SerializedExpectedBootFailure,
    SerializedProvisioningProfile,
)
from kdive.serialization import JsonValue


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
    requested_pool: str | None = None
    requested_arch: str | None = None
    failure_category: ErrorCategory | None = None


class System(DomainModel, Attribution):
    """A provisioned target; one per Allocation."""

    allocation_id: UUID
    state: SystemState
    provisioning_profile: SerializedProvisioningProfile
    target_fingerprint: str | None = None
    domain_name: str | None = None
    shape: str | None = None
    #: Optional client-supplied human handle, set at mint (ADR-0264, #867). Validated by
    #: `validate_label` before persistence; echoed in the systems read envelopes.
    label: str | None = None
    #: Host-derived accelerator (`kvm`/`tcg`) resolved from the bound Resource's advertised
    #: `guest_arches` at admission (ADR-0339). NULL when the resource advertises none — not
    #: host-derived; downstream consumers must treat NULL as "unknown", never crash on it.
    accel: str | None = None
    #: Host-derived guest CPU baseline resolved from the bound Resource's advertised `host_cpu`
    #: at mint (ADR-0368): `{model, vendor?, arch, baseline_level?}`. NULL when the resource
    #: advertises none (local/fault/un-refreshed remote) — treat NULL as unknown, never crash.
    resolved_cpu: dict[str, JsonValue] | None = None


class Snapshot(DomainModel, Attribution):
    """A named checkpoint of a System — a child ledger row (ADR-0378, #1254).

    Postgres is the index-of-record for `systems.list_snapshots`, audit, and teardown cleanup;
    libvirt holds the actual RAM+disk data inside the System's qcow2. `include_memory` records
    whether the checkpoint captured live RAM+CPU (a full system checkpoint) or disk only.
    """

    system_id: UUID
    name: str
    include_memory: bool
    state: SnapshotState


class Investigation(DomainModel, Attribution):
    """A project-scoped campaign grouping Runs toward a goal."""

    title: str
    description: str | None = None
    #: Terminal close-time account of the work, distinct from the anytime-editable `description`
    #: (ADR-0416, #1349). Required at `investigations.close` and stamped on the close transition;
    #: NULL while open/active and for closed rows that predate the field.
    summary: str | None = None
    external_refs: list[ExternalRef] = Field(default_factory=list)
    state: InvestigationState
    last_run_at: datetime | None = None
    #: Set when the investigation closes; the reconciler `gc_investigation_artifacts` sweep reclaims
    #: its run-owned build artifacts after a grace window, then clears it (ADR-0234 §4, #768).
    cleanup_pending_at: datetime | None = None


class ExpectedBootFailure(DomainBase):
    """Run-scoped expected boot failure metadata (ADR-0064, ADR-0266).

    ``kind`` is either the custom-pattern lane ``console_crash`` (the caller supplies
    ``pattern``) or one of the named presets ``oops``/``panic``/``hung_task``/``ubsan``, which
    resolve to a canonical literal console pattern (`crash_signatures.CRASH_SIGNATURE_PRESETS`). A
    preset takes no ``pattern``; supplying both is rejected. The resolved doc keeps the preset name
    and the canonical pattern, so the record states which signature the Run was matched against.

    This model validates the incoming request once and is then persisted as serialized JSON
    (``SerializedExpectedBootFailure``); ``Run.expected_boot_failure`` holds that raw object, not
    a re-parsed model. The model is therefore *not* idempotent under re-validation: feeding a
    persisted preset doc (which carries both the preset ``kind`` and its resolved ``pattern``)
    back through it is rejected by ``_resolve_preset``. Do not re-validate stored docs through
    this model — match on the raw dict, as ``expected_crash_matched_line`` does.
    """

    kind: Literal["console_crash", "oops", "panic", "hung_task", "ubsan"]
    pattern: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=256)

    @model_validator(mode="before")
    @classmethod
    def _resolve_preset(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        kind = data.get("kind")
        if not (isinstance(kind, str) and kind in CRASH_SIGNATURE_PRESETS):
            return data
        if data.get("pattern") is not None:
            raise ValueError("preset kind does not accept a custom pattern; use console_crash")
        return {**data, "pattern": CRASH_SIGNATURE_PRESETS[kind]}

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
    """One build/install/boot attempt — joins an Investigation to a System (ADR-0169).

    ``system_id`` is bound at create for the classic path or deferred to ``runs.bind`` for the
    decoupled path, so it is ``None`` while a Run is unbound. ``target_kind`` is the resource
    kind the Run committed to: it selects the builder and constrains the System a later bind may
    attach, and is always present (the bound path derives it from the System).
    """

    investigation_id: UUID
    system_id: UUID | None = None
    target_kind: ResourceKind
    state: RunState
    build_profile: SerializedBuildProfile
    expected_boot_failure: SerializedExpectedBootFailure | None = None
    kernel_ref: str | None = None
    debuginfo_ref: str | None = None
    failure_category: ErrorCategory | None = None
    failing_job_id: UUID | None = None
    #: Optional client-supplied human handle, set at create (ADR-0264, #867). Validated by
    #: `validate_label` before persistence; echoed in the runs read/create envelopes.
    label: str | None = None
    #: Optional free-form post-hoc outcome note (ADR-0415, #1386), distinct from the write-once
    #: `label`: set/updated via `runs.set` at any time after create — including on a terminal
    #: Run — to record the Run's verdict. `None` until an agent records one; echoed in the runs
    #: read envelopes.
    outcome_note: str | None = None

    def require_system_id(self) -> UUID:
        """Return the bound System id, or fail closed for an unbound Run (ADR-0169).

        Consumers that structurally require a bound System (install, boot, and the
        system-join runtime lookups) call this; the unbound lanes (build, create, bind)
        never do.

        Raises:
            CategorizedError: ``configuration_error`` (``reason: run_not_bound``) when the Run
                has no System bound yet.
        """
        if self.system_id is None:
            raise CategorizedError(
                "run is not bound to a system",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"run_id": str(self.id), "reason": "run_not_bound"},
            )
        return self.system_id


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
    "Snapshot",
    "System",
    "SystemShape",
]
