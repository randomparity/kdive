"""System-shape sizing value types (ADR-0067).

A shape names a curated sizing preset (``small`` … ``max``, seeded by migration 0013). A
resolved shape yields one :class:`ShapeSizing` tuple ``{vcpus, memory_mb, disk_gb,
pcie_match?}``. The mapping is exact: ``memory_mb`` is a whole-GB multiple (the
:class:`~kdive.domain.models.SystemShape` model and the migration CHECK both enforce it),
so the cost Selector's ``memory_mb → memory_gb`` is lossless.

A shape fixes **size only**: ``cost_class`` (and therefore price) is resolved admission-side
from the chosen Resource, never from the shape, so the same shape on a costlier host costs
more. DB-backed catalog resolution lives in :mod:`kdive.services.allocation.admission.sizing`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ShapeSizing(BaseModel):
    """The resolved sizing a shape fixes (ADR-0067).

    Carries ``vcpus`` / ``memory_mb`` / ``disk_gb`` and the optional ``pcie_match``;
    **not** ``cost_class``, which stays host-resolved at admission. ``memory_mb`` is a
    whole-GB multiple by the shape's own constraint, so a caller may map it to ``memory_gb``
    by integer division without loss.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vcpus: int
    memory_mb: int
    disk_gb: int
    pcie_match: str | None = None


class ResolvedSizing(BaseModel):
    """The unified allocation sizing after a shape-XOR-custom request is resolved (ADR-0067).

    ``vcpus`` / ``memory_gb`` are the priced size the cost Selector models; ``disk_gb`` and
    ``pcie_match`` are carried onward (to provisioning and PCIe admission), not priced;
    ``shape`` is the named preset the size came from (``None`` for full-custom), recorded as
    a label. This is the single authority for pricing, the capacity check, and the booted
    domain — admitted size and booted size are one number by construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vcpus: int
    memory_gb: int
    disk_gb: int
    pcie_match: str | None = None
    shape: str | None = None
