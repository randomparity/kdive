"""The :class:`FamilyCustomizer` seam and its :class:`CustomizeContext` (ADR-0251)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kdive.domain.catalog.images import Capability


def _mac_tag(guest_mac: str) -> Capability:
    """Map a family's ``guest_mac`` posture to its capability tag.

    Deriving the tag from ``guest_mac`` (rather than a second literal) keeps the declared tag
    and the recorded provenance from disagreeing.
    """
    if guest_mac.startswith("selinux"):
        return Capability.SELINUX
    if guest_mac == "apparmor":
        return Capability.APPARMOR
    raise ValueError(f"unmapped guest_mac posture: {guest_mac!r}")


@dataclass(frozen=True, slots=True)
class CustomizeContext:
    """Inputs a FamilyCustomizer needs to build the customize argv for one rootfs.

    Attributes:
        kind: The image kind (``debug`` or ``build``) the packages were selected for.
        packages: The resolved package set to install.
        authorized_key: Path to the kdive-managed SSH public key to inject for ``root``.
        readiness_unit_path: Host path of the rendered kdive-ready systemd unit to upload.
        is_cloud_image: True when the base is a cloud-image (needs cloud-init masking and a
            seeded ``/etc/machine-id``); False for a virt-builder scratch.
        cleanup: Mutable list the customizer appends tempfiles to for the caller to unlink.
        distro: The base-OS distro (e.g. ``fedora`` / ``rocky`` / ``centos-stream``); with
            ``version`` it drives the family's EL-major package and EPEL decisions (#823).
        version: The base-OS release (e.g. ``44`` / ``8`` / ``10``).
    """

    kind: str
    packages: tuple[str, ...]
    authorized_key: Path
    readiness_unit_path: Path
    is_cloud_image: bool
    cleanup: list[Path]
    distro: str
    version: str


class FamilyCustomizer(Protocol):
    """How an OS family turns a base image into a kdive-ready rootfs."""

    family: str
    #: The family's kdump systemd unit. The shared kdive-ready unit is ordered ``After=`` this so
    #: the serial readiness signal cannot precede kdump arming (ADR-0251 point 6); a wrong/absent
    #: name silently reopens that race. ``kdump.service`` (rhel) / ``kdump-tools.service`` (debian).
    kdump_unit: str
    #: The mandatory-access-control posture the build pipeline records as provenance ``guest_mac``:
    #: ``selinux-permissive`` (rhel — repack drops xattrs, so a first-boot relabel + permissive) or
    #: ``apparmor`` (debian — profile-based, needs no relabel).
    guest_mac: str

    def packages(self, kind: str, distro: str, version: str) -> tuple[str, ...]:
        """Return the package set this family installs for ``kind`` on ``distro``/``version``."""
        ...

    def capabilities(self, kind: str, distro: str, version: str) -> tuple[Capability, ...]:
        """Return the capability tags this family bakes for ``kind`` on ``distro``/``version``."""
        ...

    def customize_argv(self, ctx: CustomizeContext) -> list[str]:
        """Return the virt-customize argv fragment that customizes the base image."""
        ...

    def normalize(self, qcow2: Path) -> None:
        """Normalize the repacked qcow2 (fstab/crypttab/SELinux) in place via guestfish."""
        ...
