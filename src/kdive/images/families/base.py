"""The :class:`FamilyCustomizer` seam and its :class:`CustomizeContext` (ADR-0251)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kdive.domain.catalog.images import Capability
from kdive.images.families.steps import Step
from kdive.images.rootfs.kinds import RootfsImageKind


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
    """Inputs a FamilyCustomizer needs to build the customization steps for one rootfs.

    Attributes:
        kind: The image kind (``debug`` or ``build``) the packages were selected for.
        packages: The resolved package set to install.
        readiness_unit_path: Host path of the rendered kdive-ready systemd unit to upload.
        is_cloud_image: True when the base is a cloud-image (needs cloud-init masking and a
            seeded ``/etc/machine-id``); False for a virt-builder scratch.
        distro: The base-OS distro (e.g. ``fedora`` / ``rocky`` / ``centos-stream``); with
            ``version`` it drives the family's EL-major package and EPEL decisions (#823).
        version: The base-OS release (e.g. ``44`` / ``8`` / ``10``).
        fadump_capture: The arch's resolved ``fadump_capture`` trait — True where the crash path
            re-boots the real rootfs and so can run a unit from ``/etc/systemd/system`` (ppc64le
            fadump), False where the capture kernel stays in its dracut initramfs (x86_64 kdump).
            Resolved by the caller from ``kdive.domain.platform.arch_traits``, keeping the family
            free of ``if arch == …`` branches the way ``console_device`` already is.
    """

    kind: RootfsImageKind
    packages: tuple[str, ...]
    readiness_unit_path: Path
    is_cloud_image: bool
    distro: str
    version: str
    fadump_capture: bool


class FamilyCustomizer(Protocol):
    """How an OS family turns a base image into a kdive-ready rootfs.

    Every family customizes the same way (ADR-0345): the build plane repacks and normalizes the
    base, injects the family's file-op steps and a firstboot script offline, boots the image once
    so the guest runs its own package manager for the exec-op steps, and seals the result.
    """

    family: str
    #: The family's kdump systemd unit. The shared kdive-ready unit is ordered ``After=`` this so
    #: the serial readiness signal cannot precede kdump arming (ADR-0251 point 6); a wrong/absent
    #: name silently reopens that race. ``kdump.service`` (rhel) / ``kdump-tools.service`` (debian).
    kdump_unit: str
    #: The mandatory-access-control posture the build pipeline records as provenance ``guest_mac``:
    #: ``selinux-permissive`` (rhel — repack drops xattrs, so a first-boot relabel + permissive) or
    #: ``apparmor`` (debian — profile-based, needs no relabel).
    guest_mac: str
    #: The in-guest command the firstboot script prefixes each ``InstallPackages`` step's names
    #: with — the family's package manager in its non-interactive form (``dnf -y install`` /
    #: ``DEBIAN_FRONTEND=noninteractive apt-get -y install``). Any index refresh the manager needs
    #: first is a ``RunCommand`` the family emits ahead of the install (ADR-0345).
    install_command: str

    def packages(self, kind: RootfsImageKind, distro: str, version: str) -> tuple[str, ...]:
        """Return the package set this family installs for ``kind`` on ``distro``/``version``."""
        ...

    def capabilities(
        self, kind: RootfsImageKind, distro: str, version: str
    ) -> tuple[Capability, ...]:
        """Return the capability tags this family bakes for ``kind`` on ``distro``/``version``."""
        ...

    def customize_steps(self, ctx: CustomizeContext) -> list[Step]:
        """Return the ordered customization steps that turn the base into a kdive-ready rootfs."""
        ...

    def normalize(self, qcow2: Path) -> None:
        """Normalize the repacked qcow2 (fstab/crypttab/SELinux config) in place via guestfish.

        Runs before the customization boot. A family that needs a first-boot SELinux relabel does
        not touch ``/.autorelabel`` here — the offline seal after the boot does (ADR-0345), so the
        customization boot itself never relabels.
        """
        ...
