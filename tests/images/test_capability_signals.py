"""Computed capability-signal registry and its enforcement guards (ADR-0286)."""

from __future__ import annotations

from kdive.domain.catalog.images import Capability, ImageCatalogEntry, ImageVisibility
from kdive.images.capability_signals import (
    PLANNED_SIGNALS,
    REGISTERED_SIGNALS,
    render_direct_kernel_signal,
    render_kdump_signal,
)
from kdive.images.kdump_support import DEFAULT_KERNEL_BASIS


def _entry(
    caps: list[Capability], provenance: dict[str, object], *, attested: bool = False
) -> ImageCatalogEntry:
    return ImageCatalogEntry.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "provider": "local-libvirt",
            "name": "img",
            "arch": "x86_64",
            "format": "qcow2",
            "root_device": "/dev/vda",
            "capabilities": caps,
            "provenance": provenance,
            "provenance_attested": attested,
            "visibility": ImageVisibility.PUBLIC,
            "pending_since": "2026-06-30T00:00:00Z",
            "created_at": "2026-06-30T00:00:00Z",
            "updated_at": "2026-06-30T00:00:00Z",
        }
    )


def test_registered_signals_have_names_and_operands() -> None:
    for sig in REGISTERED_SIGNALS:
        assert sig.name
        assert sig.operand_keys  # every signal declares at least one build operand


def test_planned_disjoint_from_registered_and_not_capabilities() -> None:
    registered = {s.name for s in REGISTERED_SIGNALS}
    planned = {p.name for p in PLANNED_SIGNALS}
    assert registered.isdisjoint(planned)
    assert planned.isdisjoint({c.value for c in Capability})


def test_ssh_reachable_is_not_an_image_signal_after_972() -> None:
    """#972 resolved the ssh_reachable fork to a runtime probe (systems.check_ssh_reachable,

    ADR-0298), not a static image-capability signal — so it is neither planned nor registered here.
    """
    names = {s.name for s in REGISTERED_SIGNALS} | {p.name for p in PLANNED_SIGNALS}
    assert "ssh_reachable" not in names


def test_kdump_signal_degrades_to_unverified_when_operand_absent() -> None:
    # kdump tooling present but the makedumpfile_version operand missing -> never "capable".
    block = render_kdump_signal(_entry([Capability.KDUMP], {}), DEFAULT_KERNEL_BASIS)
    assert block["capability"] != "capable"
    assert block["capability"] in {"unverified", "not_applicable"}


def test_kdump_signal_not_applicable_without_tooling() -> None:
    block = render_kdump_signal(_entry([], {"makedumpfile_version": "1.7.9"}), DEFAULT_KERNEL_BASIS)
    assert block["capability"] == "not_applicable"


def test_kdump_signal_capable_with_operand_and_tooling() -> None:
    block = render_kdump_signal(
        _entry([Capability.KDUMP], {"makedumpfile_version": "1.7.9"}), DEFAULT_KERNEL_BASIS
    )
    assert block["capability"] == "capable"


def test_direct_kernel_registered_and_off_the_planned_list() -> None:
    assert "direct_kernel" in {s.name for s in REGISTERED_SIGNALS}
    assert "direct_kernel_bootable" not in {p.name for p in PLANNED_SIGNALS}
    assert "direct_kernel" not in {p.name for p in PLANNED_SIGNALS}


def test_direct_kernel_provisionable_for_single_kernel() -> None:
    block = render_direct_kernel_signal(_entry([], {"boot_kernel_count": 1}), DEFAULT_KERNEL_BASIS)
    assert set(block) == {"boot_kernel_count", "status", "note", "basis"}
    assert block["boot_kernel_count"] == 1
    assert block["status"] == "provisionable"
    assert block["note"] == ""
    assert block["basis"] == "build_verified"


def test_direct_kernel_not_provisionable_for_multiple_kernels() -> None:
    block = render_direct_kernel_signal(_entry([], {"boot_kernel_count": 2}), DEFAULT_KERNEL_BASIS)
    assert block["status"] == "not_provisionable"
    assert block["note"]  # an actionable note


def test_direct_kernel_not_provisionable_for_zero_kernels() -> None:
    block = render_direct_kernel_signal(_entry([], {"boot_kernel_count": 0}), DEFAULT_KERNEL_BASIS)
    assert block["boot_kernel_count"] == 0
    assert block["status"] == "not_provisionable"


def test_direct_kernel_unverified_when_operand_absent() -> None:
    block = render_direct_kernel_signal(_entry([], {}), DEFAULT_KERNEL_BASIS)
    assert block["boot_kernel_count"] is None
    assert block["status"] == "unverified"


def test_direct_kernel_treats_bool_operand_as_absent() -> None:
    # bool is an int subclass; True must not be read as a count of 1.
    block = render_direct_kernel_signal(
        _entry([], {"boot_kernel_count": True}), DEFAULT_KERNEL_BASIS
    )
    assert block["status"] == "unverified"
    assert block["boot_kernel_count"] is None


def test_direct_kernel_operator_attested_basis() -> None:
    # A present operand from an operator attestation must read as a claim, not a verified fact.
    block = render_direct_kernel_signal(
        _entry([], {"boot_kernel_count": 1}, attested=True), DEFAULT_KERNEL_BASIS
    )
    assert block["status"] == "provisionable"
    assert block["basis"] == "operator_attested"


def test_direct_kernel_unverified_has_no_basis() -> None:
    # An absent operand degrades to unverified with no basis — attestation never changes when
    # unverified is emitted, only how a present operand is labelled (ADR-0323).
    for attested in (False, True):
        block = render_direct_kernel_signal(_entry([], {}, attested=attested), DEFAULT_KERNEL_BASIS)
        assert block["status"] == "unverified"
        assert "basis" not in block


def test_kdump_signal_basis_operator_attested_when_present_and_attested() -> None:
    block = render_kdump_signal(
        _entry([Capability.KDUMP], {"makedumpfile_version": "1.7.9"}, attested=True),
        DEFAULT_KERNEL_BASIS,
    )
    assert block["capability"] == "capable"
    assert block["basis"] == "operator_attested"


def test_kdump_signal_basis_build_verified_when_present_and_not_attested() -> None:
    block = render_kdump_signal(
        _entry([Capability.KDUMP], {"makedumpfile_version": "1.7.9"}), DEFAULT_KERNEL_BASIS
    )
    assert block["basis"] == "build_verified"


def test_kdump_signal_absent_operand_has_no_basis() -> None:
    block = render_kdump_signal(_entry([Capability.KDUMP], {}), DEFAULT_KERNEL_BASIS)
    assert "basis" not in block
