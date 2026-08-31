"""Local-libvirt external-boot recovery state machine (ADR-0586)."""

from __future__ import annotations

import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - edits trusted domain structure after safe parse
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.providers.ports.external_boot import (
    ComponentState,
    ExternalBootActivationBinding,
    OpaqueProviderRef,
)
from kdive.providers.shared.libvirt_xml import register_kdive_namespace, register_qemu_namespace


class PublicationPhase(StrEnum):
    MOVE_READY = "move-ready"
    OLD_ASIDE = "old-aside"
    ROLLBACK_READY = "rollback-ready"
    ROLLBACK_COMPLETE = "rollback-complete"
    NEW_LIVE = "new-live"
    PUBLICATION_COMPLETE = "publication-complete"
    ABSENCE_LIVE = "absence-live"
    ABSENCE_COMPLETE = "absence-complete"
    ABSENCE_CLEANED = "absence-cleaned"


@dataclass(frozen=True, slots=True)
class ModuleLayout:
    live: ComponentState | None
    staging: ComponentState | None
    old: ComponentState | None


class ModulePublicationIO(Protocol):
    def require_inactive(self) -> None: ...
    def move_live_to_old(self) -> None: ...
    def move_staging_to_live(self) -> None: ...
    def move_old_to_live(self) -> None: ...
    def remove_old(self) -> None: ...
    def guest_sync(self) -> None: ...
    def record_phase(self, phase: PublicationPhase) -> None: ...


def recovery_directory_name(
    reference: OpaqueProviderRef, binding: ExternalBootActivationBinding
) -> str:
    """Resolve a closed recovery token to its owner-derived directory name."""
    parts = reference.ref.split("/")
    if len(parts) != 3 or parts[0] != "local-recovery-v1":
        raise ValueError("external-boot recovery reference is malformed")
    if parts[1] != binding.system_id or parts[2] != binding.activation_id:
        raise ValueError("external-boot recovery reference owner does not match binding")
    return f"{parts[1]}.{parts[2]}"


def _sync_phase(io: ModulePublicationIO, phase: PublicationPhase) -> None:
    io.guest_sync()
    io.record_phase(phase)


def advance_module_publication(
    io: ModulePublicationIO,
    *,
    phase: PublicationPhase,
    layout: ModuleLayout,
    prior: ComponentState,
    desired: ComponentState,
) -> None:
    """Perform the sole ADR-0586 action allowed by a present-tree restart row."""
    io.require_inactive()
    rows = {
        (PublicationPhase.MOVE_READY, ModuleLayout(prior, desired, None)): io.move_live_to_old,
        (PublicationPhase.MOVE_READY, ModuleLayout(None, desired, prior)): lambda: _sync_phase(
            io, PublicationPhase.OLD_ASIDE
        ),
        (PublicationPhase.OLD_ASIDE, ModuleLayout(None, desired, prior)): io.move_staging_to_live,
        (PublicationPhase.OLD_ASIDE, ModuleLayout(desired, None, prior)): lambda: _sync_phase(
            io, PublicationPhase.NEW_LIVE
        ),
        (PublicationPhase.ROLLBACK_READY, ModuleLayout(None, desired, prior)): io.move_old_to_live,
        (PublicationPhase.ROLLBACK_READY, ModuleLayout(prior, desired, None)): lambda: _sync_phase(
            io, PublicationPhase.ROLLBACK_COMPLETE
        ),
        (PublicationPhase.NEW_LIVE, ModuleLayout(desired, None, prior)): io.remove_old,
        (PublicationPhase.NEW_LIVE, ModuleLayout(desired, None, None)): lambda: _sync_phase(
            io, PublicationPhase.PUBLICATION_COMPLETE
        ),
        (PublicationPhase.PUBLICATION_COMPLETE, ModuleLayout(desired, None, None)): lambda: None,
    }
    try:
        action = rows[(PublicationPhase(phase), layout)]
    except (KeyError, ValueError) as exc:
        raise ValueError("external-boot module publication conflict") from exc
    action()


def advance_absence_publication(
    io: ModulePublicationIO,
    *,
    phase: PublicationPhase,
    layout: ModuleLayout,
    prior: ComponentState,
) -> None:
    """Perform the sole ADR-0586 action allowed by an absent-tree restart row."""
    io.require_inactive()
    rows = {
        (PublicationPhase.MOVE_READY, ModuleLayout(prior, None, None)): io.move_live_to_old,
        (PublicationPhase.MOVE_READY, ModuleLayout(None, None, prior)): lambda: _sync_phase(
            io, PublicationPhase.ABSENCE_LIVE
        ),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, None, None)): lambda: io.record_phase(
            PublicationPhase.ABSENCE_COMPLETE
        ),
        (PublicationPhase.ABSENCE_LIVE, ModuleLayout(None, None, prior)): lambda: io.record_phase(
            PublicationPhase.ABSENCE_COMPLETE
        ),
        (PublicationPhase.ABSENCE_COMPLETE, ModuleLayout(None, None, prior)): io.remove_old,
        (PublicationPhase.ABSENCE_COMPLETE, ModuleLayout(None, None, None)): lambda: _sync_phase(
            io, PublicationPhase.ABSENCE_CLEANED
        ),
        (PublicationPhase.ABSENCE_CLEANED, ModuleLayout(None, None, None)): lambda: None,
    }
    try:
        action = rows[(PublicationPhase(phase), layout)]
    except (KeyError, ValueError) as exc:
        raise ValueError("external-boot module absence conflict") from exc
    action()


def render_target_xml(source: str, *, kernel: str, initrd: str | None, cmdline: str) -> str:
    """Return source domain XML with only the direct-boot projection replaced."""
    if unicodedata.normalize("NFC", source) != source:
        raise ValueError("domain XML must be NFC")
    try:
        root = _safe_fromstring(source)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError("domain XML is malformed or forbidden") from exc
    if root.tag != "domain":
        raise ValueError("domain XML must have a domain root")
    os_element = root.find("os")
    if os_element is None:
        os_element = ET.SubElement(root, "os")
    for tag in ("kernel", "initrd", "cmdline"):
        element = os_element.find(tag)
        if element is not None:
            os_element.remove(element)
    ET.SubElement(os_element, "kernel").text = kernel
    if initrd is not None:
        ET.SubElement(os_element, "initrd").text = initrd
    ET.SubElement(os_element, "cmdline").text = cmdline
    register_kdive_namespace()
    register_qemu_namespace()
    return ET.tostring(root, encoding="unicode")
