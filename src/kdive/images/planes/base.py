"""The `RootfsBuildPlane` port: the provider-agnostic rootfs-image build contract (ADR-0092).

A `RootfsBuildPlane` turns a declarative :class:`RootfsBuildSpec` into a built, object-ready
qcow2 plus **recorded provenance** (:class:`RootfsBuildOutput`). The image's identity is the
content digest of the produced qcow2 — a rootfs image has no kernel ``build_id`` (a vmlinux
ELF-note), and a ``virt-builder``-customized image is not bit-reproducible (mirror drift,
embedded timestamps, filesystem ordering), so **bit-reproducible rebuilds are an explicit
non-goal**. The falsifiable contract is the recorded provenance: the output names exactly the
pinned inputs (releasever, package set, source-image digest) that produced its image.

This module is the public seam every provider implements (local-libvirt in this milestone,
remote-libvirt in #284) and the publish/`IMAGE_BUILD` layers consume. Implementations inherit
nothing — they satisfy the :class:`RootfsBuildPlane` ``Protocol`` structurally — so the
dataclass field sets and the ``build(spec) -> RootfsBuildOutput`` signature here are a stable
contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from kdive.domain.catalog.images import Capability

PROVENANCE_BOOT_KERNEL_COUNT = "boot_kernel_count"
PROVENANCE_DEFAULT_KERNEL_VERSION = "default_kernel_version"
PROVENANCE_MAKEDUMPFILE_VERSION = "makedumpfile_version"
PROVENANCE_OS_RELEASE = "os_release"


@dataclass(frozen=True, slots=True)
class RootfsBuildSpec:
    """Declarative inputs for a rootfs build (the pinned inputs become provenance).

    Attributes:
        provider: The provider whose plane builds the image (e.g. ``"local-libvirt"``).
        name: The catalog image name (e.g. ``"fedora-kdive-ready-43"``).
        arch: The target architecture (e.g. ``"x86_64"``).
        releasever: The base-OS release the image is built from (e.g. ``"43"``).
        packages: The package set installed into the guest, in install order.
        source_image_digest: A digest pinning the base/template image the build customizes.
        capabilities: The tooling the build bakes into the image, from the closed
            :class:`~kdive.domain.catalog.images.Capability` vocabulary (agent, kdump, drgn,
            build) — a build fact, not a liveness guarantee.
        distro: The base-OS family the image is built from (the extensibility seam; only
            ``"fedora"`` is implemented). The build plane resolves the base source/family from
            the rootfs catalog (:mod:`kdive.images.rootfs_catalog`).
    """

    provider: str
    name: str
    arch: str
    releasever: str
    packages: tuple[str, ...]
    source_image_digest: str
    capabilities: tuple[Capability, ...]
    distro: str = "fedora"


@dataclass(frozen=True, slots=True)
class RootfsBuildProvenance:
    """Typed rootfs provenance that serializes to the catalog JSONB contract."""

    plane: str
    releasever: str
    packages: tuple[str, ...]
    source_image_digest: str
    capabilities: tuple[Capability, ...]
    arch: str
    image_size: str
    distro: str | None = None
    readiness_marker: str | None = None
    layout: str | None = None
    guest_mac: str | None = None
    boot_method: str | None = None
    guest_access_seam: str | None = None
    package_versions: dict[str, str] = field(default_factory=dict)
    makedumpfile_version: str | None = None
    boot_kernel_count: int | None = None
    default_kernel_version: str | None = None
    os_release: dict[str, str] | None = None

    @classmethod
    def local_libvirt(
        cls,
        spec: RootfsBuildSpec,
        *,
        source_image_digest: str,
        image_size: str,
        readiness_marker: str,
        layout: str,
        guest_mac: str,
        package_versions: dict[str, str],
        makedumpfile_version: str | None,
        boot_kernel_count: int | None,
        default_kernel_version: str | None,
        os_release: dict[str, str] | None,
    ) -> RootfsBuildProvenance:
        """Build local-libvirt provenance from verified provider build operands."""
        return cls(
            plane="local-libvirt",
            distro=spec.distro,
            releasever=spec.releasever,
            packages=spec.packages,
            source_image_digest=source_image_digest,
            capabilities=spec.capabilities,
            arch=spec.arch,
            image_size=image_size,
            readiness_marker=readiness_marker,
            layout=layout,
            guest_mac=guest_mac,
            package_versions=package_versions,
            makedumpfile_version=makedumpfile_version,
            boot_kernel_count=boot_kernel_count,
            default_kernel_version=default_kernel_version,
            os_release=os_release,
        )

    @classmethod
    def remote_libvirt(
        cls,
        spec: RootfsBuildSpec,
        *,
        packages: tuple[str, ...],
        image_size: str,
        boot_method: str,
        guest_access_seam: str,
        package_versions: dict[str, str],
    ) -> RootfsBuildProvenance:
        """Build remote-libvirt provenance from the remote disk-image build operands."""
        return cls(
            plane="remote-libvirt",
            boot_method=boot_method,
            releasever=spec.releasever,
            packages=packages,
            source_image_digest=spec.source_image_digest,
            capabilities=spec.capabilities,
            arch=spec.arch,
            image_size=image_size,
            guest_access_seam=guest_access_seam,
            package_versions=package_versions,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize to the existing ``image_catalog.provenance`` JSON shape."""
        record: dict[str, object] = {
            "plane": self.plane,
            "releasever": self.releasever,
            "packages": list(self.packages),
            "source_image_digest": self.source_image_digest,
            "capabilities": [str(cap) for cap in self.capabilities],
            "arch": self.arch,
            "image_size": self.image_size,
        }
        self._put_if_present(record, "distro", self.distro)
        self._put_if_present(record, "readiness_marker", self.readiness_marker)
        self._put_if_present(record, "layout", self.layout)
        self._put_if_present(record, "guest_mac", self.guest_mac)
        self._put_if_present(record, "boot_method", self.boot_method)
        self._put_if_present(record, "guest_access_seam", self.guest_access_seam)
        if self.package_versions:
            record["package_versions"] = dict(self.package_versions)
        self._put_if_present(record, PROVENANCE_MAKEDUMPFILE_VERSION, self.makedumpfile_version)
        if self.boot_kernel_count is not None:
            record[PROVENANCE_BOOT_KERNEL_COUNT] = self.boot_kernel_count
        self._put_if_present(record, PROVENANCE_DEFAULT_KERNEL_VERSION, self.default_kernel_version)
        if self.os_release:
            record[PROVENANCE_OS_RELEASE] = dict(self.os_release)
        return record

    @staticmethod
    def _put_if_present(record: dict[str, object], key: str, value: str | None) -> None:
        if value:
            record[key] = value


@dataclass(frozen=True, slots=True)
class RootfsBuildOutput:
    """The product of a rootfs build.

    Attributes:
        qcow2_path: Local path to the produced qcow2 (object-ready; publish writes it to the
            object store and content-addresses it by :attr:`digest`).
        digest: The content digest of the produced qcow2 (``"sha256:<hex>"``) — the image
            identity. Distinct from a kernel ``build_id``; a rootfs image has none.
        provenance: The serialized :class:`RootfsBuildProvenance` inputs and build args that
            produced the image, JSONB-serializable for the catalog row's ``provenance`` column.
        kernel_config: The image's extracted ``/boot/config-<ver>`` bytes, or ``None`` when there
            is no single baseline kernel, no config file, or a probe failure (ADR-0317); publish
            stores it best-effort.
    """

    qcow2_path: Path
    digest: str
    provenance: dict[str, object]
    kernel_config: bytes | None = None


@runtime_checkable
class RootfsBuildPlane(Protocol):
    """A provider's in-process rootfs build plane.

    The single operation is a synchronous, environment-bound build (libguestfs/qemu, minutes);
    callers on the worker offload it via ``asyncio.to_thread`` so it never stalls the event loop.
    """

    def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
        """Build the image declared by ``spec`` and return its path, digest, and provenance.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid/unresolvable inputs,
                ``MISSING_DEPENDENCY`` for absent build tooling, or ``PROVISIONING_FAILURE`` for
                a build-stage failure.
        """
        ...
