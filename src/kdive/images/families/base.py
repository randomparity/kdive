"""The :class:`FamilyCustomizer` seam and its :class:`CustomizeContext` (ADR-0250)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
    """

    kind: str
    packages: tuple[str, ...]
    authorized_key: Path
    readiness_unit_path: Path
    is_cloud_image: bool
    cleanup: list[Path]


class FamilyCustomizer(Protocol):
    """How an OS family turns a base image into a kdive-ready rootfs."""

    family: str

    def packages(self, kind: str) -> tuple[str, ...]:
        """Return the package set this family installs for ``kind`` (``debug``/``build``)."""
        ...

    def customize_argv(self, ctx: CustomizeContext) -> list[str]:
        """Return the virt-customize argv fragment that customizes the base image."""
        ...

    def normalize(self, qcow2: Path) -> None:
        """Normalize the repacked qcow2 (fstab/crypttab/SELinux) in place via guestfish."""
        ...
