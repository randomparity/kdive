"""Computed image-capability signals over build-recorded provenance (ADR-0286).

Generalizes the kdump-capability predicate: each signal computes a feature answer from a
build-recorded operand and degrades to a non-confident status when the operand is absent, so
metadata that predates a signal cannot report a confident-but-wrong answer. The framework is
deliberately minimal — its value is the honesty invariant and the guarded backlog below, not
code reuse across a single implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from kdive.domain.catalog.images import Capability, ImageCatalogEntry
from kdive.images.drgn_support import live_drgn_capability
from kdive.images.kdump_support import KernelVersion, kdump_capability
from kdive.images.planes.base import (
    PROVENANCE_BOOT_KERNEL_COUNT,
    PROVENANCE_DRGN_VERSION,
    PROVENANCE_MAKEDUMPFILE_VERSION,
)
from kdive.serialization import JsonValue

type SignalRender = Callable[[ImageCatalogEntry, KernelVersion], dict[str, JsonValue]]


def _provenance_basis(entry: ImageCatalogEntry) -> str:
    """The evidence basis for a present operand: an operator claim vs a KDIVE-verified fact.

    ``operator_attested`` when the row's provenance was declared by an operator
    (``image_catalog.provenance_attested``, an ``s3`` image's ``[image.attested]`` operands,
    ADR-0323); ``build_verified`` when it was recorded by a KDIVE build/publish or build-fs sidecar.
    Surfaced on each **present**-operand signal block so a confident answer discloses whether it
    rests on a claim or a fact — the ADR-0286 honesty invariant, made explicit rather than blurred.
    """
    return "operator_attested" if entry.provenance_attested else "build_verified"


@dataclass(frozen=True, slots=True)
class CapabilitySignal:
    """A computed capability answer over an image's build-recorded provenance.

    ``operand_keys`` are the provenance keys the build must record; when any is absent the
    ``render`` returns a non-confident status (never ``capable``), so un-refreshed metadata
    cannot lie.
    """

    name: str
    operand_keys: tuple[str, ...]
    render: SignalRender


@dataclass(frozen=True, slots=True)
class PlannedSignal:
    """A capability signal that is not honestly computable yet (its operand does not exist)."""

    name: str
    tracking_issue: str
    rationale: str


def render_kdump_signal(
    entry: ImageCatalogEntry, target_kernel: KernelVersion
) -> dict[str, JsonValue]:
    """The kdump capability block for ``entry`` against ``target_kernel`` (operand-driven).

    Reads the build-recorded ``provenance["makedumpfile_version"]`` (``None`` when absent or not
    a string) and whether the image carries the ``kdump`` tooling tag, then computes the
    capability, echoing the kernel basis it was computed against. A reader never raises on image
    data — an unparseable stored version degrades to ``unverified``.
    """
    raw = entry.provenance.get(PROVENANCE_MAKEDUMPFILE_VERSION)
    has_operand = isinstance(raw, str) and bool(raw)
    cap = kdump_capability(
        makedumpfile_version=raw if has_operand else None,
        target_kernel=target_kernel,
        kdump_tooling=Capability.KDUMP in entry.capabilities,
    )
    block: dict[str, JsonValue] = {
        "makedumpfile_version": raw if isinstance(raw, str) else "",
        "target_kernel": cap.target_kernel,
        "capability": cap.status,
        "min_makedumpfile_required": cap.min_makedumpfile_required,
        "note": cap.note,
    }
    if has_operand:
        block["basis"] = _provenance_basis(entry)
    return block


KDUMP_SIGNAL = CapabilitySignal(
    name="kdump", operand_keys=("makedumpfile_version",), render=render_kdump_signal
)


def render_direct_kernel_signal(
    entry: ImageCatalogEntry, _target_kernel: KernelVersion
) -> dict[str, JsonValue]:
    """The direct-kernel provisionability block for ``entry`` (operand-driven, ADR-0295).

    Reads the build-recorded ``provenance["boot_kernel_count"]`` — the non-rescue ``vmlinuz-*``
    count in the image's ``/boot`` — and reports whether a direct-kernel provision can select a
    baseline kernel unambiguously: exactly one is ``provisionable``, zero or more-than-one is
    ``not_provisionable`` (the fail-closed selection raises at provision, ADR-0272). The answer is a
    static image property, so ``_target_kernel`` is accepted for the uniform signal signature and
    ignored. A missing or non-``int`` operand (``bool`` excluded — it is an ``int`` subclass)
    degrades to ``unverified`` with a ``None`` count, so un-refreshed metadata never lies.
    """
    raw = entry.provenance.get(PROVENANCE_BOOT_KERNEL_COUNT)
    count = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    if count is None:
        return {
            "boot_kernel_count": None,
            "status": "unverified",
            "note": "boot kernel count is not recorded; rebuild the image to characterize "
            "direct-kernel provisionability",
        }
    basis = _provenance_basis(entry)
    if count == 1:
        return {"boot_kernel_count": 1, "status": "provisionable", "note": "", "basis": basis}
    note = (
        "rootfs /boot has no bootable non-rescue kernel"
        if count == 0
        else f"rootfs /boot has {count} non-rescue kernels; direct-kernel selection is ambiguous "
        "and fails closed at provision"
    )
    return {"boot_kernel_count": count, "status": "not_provisionable", "note": note, "basis": basis}


DIRECT_KERNEL_SIGNAL = CapabilitySignal(
    name="direct_kernel", operand_keys=("boot_kernel_count",), render=render_direct_kernel_signal
)


def render_live_drgn_signal(
    entry: ImageCatalogEntry, _target_kernel: KernelVersion
) -> dict[str, JsonValue]:
    """The live-introspection capability block for ``entry`` (operand-driven).

    Reads the build-recorded ``provenance["drgn_version"]`` (``None`` when absent or not a string)
    and whether the image carries the ``drgn`` tooling tag, then computes whether the shipped drgn
    can introspect a booted kernel from the guest's own in-guest BTF without uploaded debuginfo. The
    answer is a static image property (BTF lives in the running guest, not a target-kernel matrix),
    so ``_target_kernel`` is accepted for the uniform signal signature and ignored. A reader never
    raises on image data — a missing or unparseable stored version degrades to ``unverified``.
    """
    raw = entry.provenance.get(PROVENANCE_DRGN_VERSION)
    has_operand = isinstance(raw, str) and bool(raw)
    cap = live_drgn_capability(
        drgn_version=raw if has_operand else None,
        drgn_tooling=Capability.DRGN in entry.capabilities,
    )
    block: dict[str, JsonValue] = {
        "drgn_version": raw if isinstance(raw, str) else "",
        "capability": cap.status,
        "min_drgn_required": cap.min_drgn_required,
        "note": cap.note,
    }
    if has_operand:
        block["basis"] = _provenance_basis(entry)
    return block


LIVE_DRGN_SIGNAL = CapabilitySignal(
    name="live_drgn", operand_keys=("drgn_version",), render=render_live_drgn_signal
)

#: The computed signals an agent reads from ``images.describe`` ``data.capability_signals``.
REGISTERED_SIGNALS: tuple[CapabilitySignal, ...] = (
    KDUMP_SIGNAL,
    DIRECT_KERNEL_SIGNAL,
    LIVE_DRGN_SIGNAL,
)

#: The signals the metadata audit named that are not honestly computable yet — each blocked on a
#: build operand that does not exist until its tracking issue lands. Documented, guarded to stay
#: out of the registered/capability sets, never emitted.
PLANNED_SIGNALS: tuple[PlannedSignal, ...] = (
    PlannedSignal(
        "sysrq",
        "#952",
        "SysRq availability can report false success; needs a build-recorded operand",
    ),
)

__all__ = [
    "DIRECT_KERNEL_SIGNAL",
    "KDUMP_SIGNAL",
    "LIVE_DRGN_SIGNAL",
    "PLANNED_SIGNALS",
    "REGISTERED_SIGNALS",
    "CapabilitySignal",
    "PlannedSignal",
    "render_direct_kernel_signal",
    "render_kdump_signal",
    "render_live_drgn_signal",
]
