"""Computed capability-signal registry and its enforcement guards (ADR-0286)."""

from __future__ import annotations

from kdive.domain.catalog.images import Capability, ImageCatalogEntry, ImageVisibility
from kdive.images.capability_signals import (
    PLANNED_SIGNALS,
    REGISTERED_SIGNALS,
    render_kdump_signal,
)
from kdive.images.kdump_support import DEFAULT_KERNEL_BASIS


def _entry(caps: list[Capability], provenance: dict[str, object]) -> ImageCatalogEntry:
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


def test_ssh_reachable_signal_repointed_off_resolved_956() -> None:
    """post-#962 (ADR-0288) reachability works; #956 is closed, so the ssh_reachable planned

    signal must not still cite #956 or the stale "broken" blocker — it tracks the #972 follow-up
    (the static-signal-vs-runtime-probe fork) instead (ADR-0294).
    """
    ssh = next(p for p in PLANNED_SIGNALS if p.name == "ssh_reachable")
    assert "#956" not in ssh.tracking_issue
    assert "#972" in ssh.tracking_issue
    assert "broken" not in ssh.rationale.lower()


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
