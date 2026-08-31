"""Local-libvirt external-boot recovery state-machine tests (ADR-0586)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    ModuleLayout,
    PublicationPhase,
    advance_absence_publication,
    advance_module_publication,
    recovery_directory_name,
    render_target_xml,
)
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    OpaqueProviderRef,
    PresentComponentState,
)

_SOURCE_XML = """<domain xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0">
  <name>kdive-system</name>
  <metadata><owner system="00000000-0000-0000-0000-000000000001" /></metadata>
  <memory unit="MiB">2048</memory>
  <os firmware="efi"><type arch="x86_64">hvm</type><kernel>/old</kernel>
    <initrd>/old-i</initrd><cmdline>old</cmdline></os>
  <devices><disk type="file"><target dev="vda" /></disk></devices>
  <qemu:commandline><qemu:arg value="-S" /></qemu:commandline>
</domain>"""


def test_render_target_xml_changes_only_owned_boot_projection() -> None:
    rendered = render_target_xml(
        _SOURCE_XML,
        kernel="artifacts/kernel",
        initrd="artifacts/initrd",
        cmdline="root=/dev/vda1 console=ttyS0",
    )

    assert '<memory unit="MiB">2048</memory>' in rendered
    assert '<disk type="file"><target dev="vda" /></disk>' in rendered
    assert '<qemu:arg value="-S"' in rendered
    assert "<kernel>artifacts/kernel</kernel>" in rendered
    assert "<initrd>artifacts/initrd</initrd>" in rendered
    assert "<cmdline>root=/dev/vda1 console=ttyS0</cmdline>" in rendered


def test_render_target_xml_omits_optional_initrd() -> None:
    rendered = render_target_xml(
        _SOURCE_XML, kernel="artifacts/kernel", initrd=None, cmdline="root=/dev/vda1"
    )
    assert "<initrd>" not in rendered


@pytest.mark.parametrize(
    "source",
    [
        "<domain>",
        '<!DOCTYPE domain [<!ENTITY x "x">]><domain>&x;</domain>',
        "<domain><name>e\u0301</name></domain>",
    ],
)
def test_render_target_xml_rejects_malformed_forbidden_or_non_nfc(source: str) -> None:
    with pytest.raises(ValueError, match="domain XML"):
        render_target_xml(source, kernel="kernel", initrd=None, cmdline="root=/dev/vda1")


_PRIOR = PresentComponentState(manifest="sha256:" + "1" * 64)
_DESIRED = PresentComponentState(manifest="sha256:" + "2" * 64)


class _PublicationIO:
    def __init__(self, layout: ModuleLayout) -> None:
        self.layout = layout
        self.actions: list[str] = []

    def require_inactive(self) -> None:
        self.actions.append("inactive")

    def move_live_to_old(self) -> None:
        self.actions.append("live-to-old")

    def move_staging_to_live(self) -> None:
        self.actions.append("staging-to-live")

    def move_old_to_live(self) -> None:
        self.actions.append("old-to-live")

    def remove_old(self) -> None:
        self.actions.append("remove-old")

    def guest_sync(self) -> None:
        self.actions.append("guest-sync")

    def record_phase(self, phase: PublicationPhase) -> None:
        self.actions.append(f"phase:{phase}")


@pytest.mark.parametrize(
    ("phase", "layout", "action"),
    [
        ("move-ready", ModuleLayout(_PRIOR, _DESIRED, None), "live-to-old"),
        ("move-ready", ModuleLayout(None, _DESIRED, _PRIOR), "phase:old-aside"),
        ("old-aside", ModuleLayout(None, _DESIRED, _PRIOR), "staging-to-live"),
        ("old-aside", ModuleLayout(_DESIRED, None, _PRIOR), "phase:new-live"),
        ("rollback-ready", ModuleLayout(None, _DESIRED, _PRIOR), "old-to-live"),
        ("rollback-ready", ModuleLayout(_PRIOR, _DESIRED, None), "phase:rollback-complete"),
        ("new-live", ModuleLayout(_DESIRED, None, _PRIOR), "remove-old"),
        ("new-live", ModuleLayout(_DESIRED, None, None), "phase:publication-complete"),
    ],
)
def test_present_restart_table_has_one_permitted_action(
    phase: PublicationPhase, layout: ModuleLayout, action: str
) -> None:
    io = _PublicationIO(layout)
    advance_module_publication(io, phase=phase, layout=layout, prior=_PRIOR, desired=_DESIRED)
    assert io.actions[-1] == action


def test_unlisted_restart_layout_conflicts_without_mutation() -> None:
    layout = ModuleLayout(_DESIRED, _DESIRED, _PRIOR)
    io = _PublicationIO(layout)
    with pytest.raises(ValueError, match="conflict"):
        advance_module_publication(
            io,
            phase=PublicationPhase.OLD_ASIDE,
            layout=layout,
            prior=_PRIOR,
            desired=_DESIRED,
        )
    assert io.actions == ["inactive"]


@pytest.mark.parametrize(
    ("phase", "layout", "action"),
    [
        ("move-ready", ModuleLayout(_PRIOR, None, None), "live-to-old"),
        ("move-ready", ModuleLayout(None, None, _PRIOR), "phase:absence-live"),
        ("move-ready", ModuleLayout(None, None, None), "phase:absence-complete"),
        ("absence-live", ModuleLayout(None, None, _PRIOR), "phase:absence-complete"),
        ("absence-complete", ModuleLayout(None, None, _PRIOR), "remove-old"),
        ("absence-complete", ModuleLayout(None, None, None), "phase:absence-cleaned"),
    ],
)
def test_absence_restart_table_has_one_permitted_action(
    phase: PublicationPhase, layout: ModuleLayout, action: str
) -> None:
    io = _PublicationIO(layout)
    advance_absence_publication(io, phase=phase, layout=layout, prior=_PRIOR)
    assert io.actions[-1] == action


def test_absence_terminal_rejects_reappeared_tree() -> None:
    layout = ModuleLayout(_PRIOR, None, None)
    io = _PublicationIO(layout)
    with pytest.raises(ValueError, match="conflict"):
        advance_absence_publication(
            io, phase=PublicationPhase.ABSENCE_CLEANED, layout=layout, prior=_PRIOR
        )
    assert io.actions == ["inactive"]


_BINDING = ExternalBootActivationBinding(
    system_id="00000000-0000-0000-0000-000000000001",
    run_id="00000000-0000-0000-0000-000000000002",
    activation_id="00000000-0000-0000-0000-000000000003",
)


def test_recovery_reference_resolves_only_exact_binding() -> None:
    reference = OpaqueProviderRef(
        ref=(
            "local-recovery-v1/00000000-0000-0000-0000-000000000001/"
            "00000000-0000-0000-0000-000000000003"
        )
    )
    assert recovery_directory_name(reference, _BINDING) == (
        "00000000-0000-0000-0000-000000000001.00000000-0000-0000-0000-000000000003"
    )


@pytest.mark.parametrize(
    "reference",
    [
        "local-recovery-v1/00000000-0000-0000-0000-000000000009/"
        "00000000-0000-0000-0000-000000000003",
        "local-recovery-v1/00000000-0000-0000-0000-000000000001/not-a-uuid",
        "other/00000000-0000-0000-0000-000000000001/00000000-0000-0000-0000-000000000003",
    ],
)
def test_recovery_reference_rejects_cross_owner_or_malformed(reference: str) -> None:
    with pytest.raises((ValueError, ValidationError), match="recovery"):
        recovery_directory_name(OpaqueProviderRef(ref=reference), _BINDING)
