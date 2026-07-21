"""Shared MCP request payload models."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdive.domain.catalog.resources import ResourceKind

_DEFAULT_COST_CLASS = "local"

_WINDOW_DESCRIPTION = "Lease window length in hours, e.g. 24."
_WINDOW_EXAMPLE = 24

_VCPUS_DESCRIPTION = "Guest virtual CPU count (a positive integer), e.g. 4."
_MEMORY_GB_DESCRIPTION = "Guest RAM in whole gigabytes (a positive integer), e.g. 8."

# Stable local exception for the shape-XOR-custom violation. The binding middleware (ADR-0132)
# keys on Pydantic's wrapped original exception to distinguish the XOR violation from a
# field-level error and to surface the `both` flag as the envelope `detail`.
SHAPE_XOR_ERROR_TYPE = "shape_xor_custom"


class ShapeXorCustomError(ValueError):
    """Allocation size-source XOR violation."""

    code = SHAPE_XOR_ERROR_TYPE

    def __init__(self, message: str, *, both: bool) -> None:
        super().__init__(message)
        self.both = both


class ToolPayload(BaseModel):
    """Base model for MCP JSON payloads."""

    model_config = ConfigDict(extra="forbid")


class SelectorPayload(ToolPayload):
    """The size/window shape shared by estimation and allocation admission.

    ``vcpus`` / ``memory_gb`` are optional on the base so a shape-named allocation request
    (which carries no raw sizing) validates; :class:`EstimateRequestPayload` re-declares
    them required (estimate is custom-only, ADR-0067), and :class:`AllocationRequestPayload`
    enforces the shape-XOR-custom rule.
    """

    vcpus: int | None = Field(default=None, description=_VCPUS_DESCRIPTION)
    memory_gb: int | None = Field(default=None, description=_MEMORY_GB_DESCRIPTION)
    window: Decimal | None = Field(
        default=None, gt=0, description=_WINDOW_DESCRIPTION, examples=[_WINDOW_EXAMPLE]
    )


class ResourceById(ToolPayload):
    mode: Literal["id"]
    resource_id: str


class ResourceByKind(ToolPayload):
    mode: Literal["kind"] = "kind"
    kind: ResourceKind = ResourceKind.LOCAL_LIBVIRT


class ResourceByPool(ToolPayload):
    # Provenance: ADR-0186, #561 (pool selector).
    """Select the first available resource carrying ``pool``."""

    mode: Literal["pool"] = "pool"
    pool: str = Field(min_length=1)


type ResourceSelector = ResourceById | ResourceByKind | ResourceByPool


class AllocationRequestPayload(SelectorPayload):
    """An allocation request sized by a named shape XOR a full custom triple (ADR-0067).

    Exactly one sizing source is supplied: ``shape`` (a catalog name resolved to the
    priced tuple at admission), or the full custom triple ``{vcpus, memory_gb, disk_gb}``.
    Both sides set, neither set, or a *partial* custom triple is a structural error here,
    so a partial size can never reach a NULL ``requested_disk_gb`` snapshot that would
    silently disable the size unification.
    """

    shape: str | None = Field(
        default=None,
        description=(
            "Named size from `shapes.list`; mutually exclusive with vcpus/memory_gb/disk_gb "
            "(supply exactly one sizing source)."
        ),
    )
    disk_gb: int | None = Field(
        default=None,
        description=(
            "Guest disk in GB (part of the custom triple; omit when using a shape). Sizes the "
            "guest's usable disk — the filesystem grows to fill it on first boot — so allow "
            "headroom for tool installs + build artifacts + a vmcore. Bounded by the host disk "
            "ceiling (over-ceiling is a configuration_error)."
        ),
    )
    resource: ResourceSelector = Field(default_factory=ResourceByKind, discriminator="mode")
    arch: str | None = Field(
        default=None,
        description=(
            "Guest architecture to place and price for (e.g. 'ppc64le'); omit for an "
            "architecture-blind request. When set, only hosts that can boot it are candidates "
            "(a host advertising other guest arches is skipped; one advertising none is still "
            "eligible), and the reserved cost reflects the host's accelerator for this arch — "
            "an emulated (TCG) guest is priced above a native (KVM) one. The bill is finalized "
            "from the System's provisioned architecture."
        ),
    )
    pcie_devices: list[str] = Field(
        default_factory=list,
        description="PCIe match specs ('vendor:device' or 'class=NN') to resolve + claim.",
    )
    on_capacity: Literal["deny", "queue"] = Field(
        default="deny",
        description=(
            "On a capacity denial (host cap / concurrency quota): 'deny' (default) returns "
            "the denial; 'queue' enqueues a durable 'requested' allocation holding a queue "
            "position (no budget/lease/occupancy). Budget and configuration denials always "
            "hard-deny."
        ),
    )

    @model_validator(mode="after")
    def _shape_xor_custom_triple(self) -> AllocationRequestPayload:
        custom = (self.vcpus, self.memory_gb, self.disk_gb)
        custom_set = [v is not None for v in custom]
        if self.shape is not None:
            if any(custom_set):
                raise ShapeXorCustomError(
                    "supply a shape or a custom size, not both",
                    both=True,
                )
            return self
        if not all(custom_set):
            raise ShapeXorCustomError(
                "supply a shape, or the full custom triple {vcpus, memory_gb, disk_gb}",
                both=False,
            )
        return self


_COST_CLASS_DESCRIPTION = (
    "Hypothetical cost class to price against (default 'local'); selects the per-class "
    "pricing coefficient. This is a what-if input, not the class you are billed under: "
    "actual usage is billed under the persisted cost_class of the resource the allocation "
    "books. To get an estimate that matches the bill, pass the cost_class of the resource "
    "you intend to allocate on (read it from `resources.describe`). An unknown class is a "
    "configuration_error."
)


_ACCEL_DESCRIPTION = (
    "Optional accelerator to price the estimate at: 'kvm' (native) or 'tcg' (foreign-arch "
    "emulation). Omit for the native baseline. A TCG guest is priced above a same-size KVM "
    "guest — price both to compare architectures before you allocate. This is a what-if input; "
    "the host resolves the real accelerator for your arch at provision. An unknown value is a "
    "configuration_error."
)


class EstimateRequestPayload(SelectorPayload):
    vcpus: int = Field(description=_VCPUS_DESCRIPTION)
    memory_gb: int = Field(description=_MEMORY_GB_DESCRIPTION)
    window: Decimal = Field(gt=0, description=_WINDOW_DESCRIPTION, examples=[_WINDOW_EXAMPLE])
    cost_class: str = Field(default=_DEFAULT_COST_CLASS, description=_COST_CLASS_DESCRIPTION)
    accel: str | None = Field(default=None, description=_ACCEL_DESCRIPTION)
