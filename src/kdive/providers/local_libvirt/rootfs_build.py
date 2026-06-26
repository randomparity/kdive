"""The in-process local-libvirt rootfs build plane (M2.4/2, ADR-0052, ADR-0092, ADR-0250).

`LocalLibvirtRootfsBuildPlane` builds a kdive-ready rootfs from the declarative rootfs catalog
(`kdive.images.rootfs_catalog`) plus a per-family customizer seam, recording **pinned-input
provenance** into the :class:`RootfsBuildOutput`. The pipeline is:

1. resolve the kdive-managed SSH public key (ADR-0052 — the single source of truth shared with
   the connect-time ``ssh -i`` identity);
2. resolve the catalog row for ``spec.name`` (its base ``source`` + ``family``); an uncataloged
   old-style spec falls back to a ``virt-builder:<distro>-<releasever>`` template + the rhel family
   so the legacy ``build-fs`` CLI keeps working until it moves to ``--image`` (Task 6, ADR-0250);
3. :func:`kdive.images.base_source.acquire_base` materializes the base into a scratch qcow2 — a
   ``virt-builder`` template or a sha256-pinned cloud image;
4. ``virt-customize`` applies the family's argv (``family.customize_argv``): install the package
   set, enable ``sshd``/``kdump``, inject the authorized key, stage the kdive-ready unit, etc.;
5. ``virt-tar-out`` + ``virt-make-fs --type=ext4 --format=qcow2`` repack the root tree into a
   **no-partition-table whole-disk ext4 qcow2** — the only layout the direct-kernel boot provider
   mounts (``root=/dev/vda``, no initramfs, ADR-0030);
6. ``family.normalize`` rewrites fstab to a lone ``/``, removes crypttab, and sets the family's
   SELinux policy (rhel: permissive + first-boot relabel) via guestfish.

The slow libguestfs/network seams are **injected** (:class:`RootfsBuildTools`) and default to the
real implementations, so unit tests cover the orchestration/provenance contract without libguestfs,
qemu, or the network; the real path is exercised on the operator-run live-stack path. ``build()``
is synchronous — the worker offloads the whole call via ``asyncio.to_thread`` (ADR-0092).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.base_source import Downloader, _real_download, acquire_base
from kdive.images.families._fedora_customize import (
    READINESS_MARKER as _READINESS_MARKER,
)
from kdive.images.families._fedora_customize import (
    READINESS_UNIT as _READINESS_UNIT,
)
from kdive.images.families.base import CustomizeContext, FamilyCustomizer
from kdive.images.families.rhel import RhelFamily
from kdive.images.planes._build_common import (
    build_workspace,
    digest_file,
    publish_qcow2,
    run_guestfs_tool,
    validate_image_name,
)
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.images.rootfs_catalog import (
    CloudImageSource,
    RootfsCatalogEntry,
    RootfsSource,
    VirtBuilderSource,
    load_rootfs_catalog,
)
from kdive.prereqs.managed_ssh_key import (
    ManagedKeyError,
    ensure_managed_keypair,
    managed_public_key_path,
)
from kdive.providers.shared.build_timeouts import SLOW_BUILD_TOOL_TIMEOUT_S

_DEFAULT_WORKSPACE = "/var/lib/kdive/build/images"
_DEFAULT_IMAGE_SIZE = "6G"
_ACQUIRE_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S
_CUSTOMIZE_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S
_REPACK_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S

# The rhel FamilyCustomizer (ADR-0250) sets SELinux permissive + a first-boot relabel; the
# repacked image is permissive, recorded as the provenance ``guest_selinux``.
_GUEST_SELINUX = "permissive"

_FAMILIES: dict[str, FamilyCustomizer] = {"rhel": RhelFamily()}


def family_for(name_or_family: str) -> FamilyCustomizer:
    """Resolve a FamilyCustomizer by family name.

    Args:
        name_or_family: The catalog row's ``family`` (e.g. ``"rhel"``).

    Returns:
        The matching :class:`~kdive.images.families.base.FamilyCustomizer`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` naming the family and the available families
            when ``name_or_family`` is not implemented.
    """
    family = _FAMILIES.get(name_or_family)
    if family is None:
        raise CategorizedError(
            f"unknown rootfs family: {name_or_family}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"family": name_or_family, "available": sorted(_FAMILIES)},
        )
    return family


def _resolve_managed_public_key() -> Path:
    """Resolve the kdive-managed SSH public key, generating the keypair if absent (ADR-0052)."""
    try:
        ensure_managed_keypair()
        return managed_public_key_path()
    except ManagedKeyError as exc:
        raise CategorizedError(
            "could not resolve the kdive-managed SSH public key to install into the rootfs",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"error": type(exc).__name__},
        ) from exc


def _run(argv: list[str], *, stage: str, timeout_s: int) -> None:
    """Run a fixed-argv libguestfs tool, mapping failure onto a categorized error."""
    run_guestfs_tool(
        argv,
        stage=stage,
        timeout_s=timeout_s,
        missing_message=f"{argv[0]} is not installed; cannot build the rootfs image",
    )


def _real_virt_builder(*, template: str, output: Path) -> None:  # pragma: no cover - live_vm
    """Acquire a base scratch image from a ``virt-builder`` template (the acquire_base seam)."""
    _run(
        ["virt-builder", template, "--format", "qcow2", "--output", str(output)],
        stage="virt-builder",
        timeout_s=_ACQUIRE_TIMEOUT_S,
    )


def _real_virt_customize(qcow2: Path, argv: list[str]) -> None:  # pragma: no cover - live_vm
    """Apply the family's customization argv to the acquired scratch via ``virt-customize``."""
    _run(
        ["virt-customize", "-a", str(qcow2), *argv],
        stage="virt-customize",
        timeout_s=_CUSTOMIZE_TIMEOUT_S,
    )


def _real_repack_whole_disk_ext4(*, scratch: Path, qcow2: Path, size: str) -> None:
    """Repack the customized root tree into a no-partition-table whole-disk ext4 qcow2."""
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as handle:
        tar_path = Path(handle.name)
    try:
        _run(
            ["virt-tar-out", "-a", str(scratch), "/", str(tar_path)],
            stage="virt-tar-out",
            timeout_s=_REPACK_TIMEOUT_S,
        )
        _run(
            [
                "virt-make-fs",
                "--type=ext4",
                "--format=qcow2",
                f"--size={size}",
                str(tar_path),
                str(qcow2),
            ],
            stage="virt-make-fs",
            timeout_s=_REPACK_TIMEOUT_S,
        )
    finally:
        tar_path.unlink(missing_ok=True)


type ResolveAuthorizedKey = Callable[[], Path]
type AcquireBase = Callable[..., None]
type VirtBuilder = Callable[..., None]
type Customize = Callable[[Path, list[str]], None]
type RepackWholeDiskExt4 = Callable[..., None]
type FamilyResolver = Callable[[str], FamilyCustomizer]


@dataclass(frozen=True, slots=True)
class RootfsBuildTools:
    """The injectable build seams; default to the real libguestfs/network implementations."""

    resolve_authorized_key: ResolveAuthorizedKey = _resolve_managed_public_key
    acquire_base: AcquireBase = acquire_base
    virt_builder: VirtBuilder = _real_virt_builder
    downloader: Downloader = _real_download
    customize: Customize = _real_virt_customize
    repack_whole_disk_ext4: RepackWholeDiskExt4 = _real_repack_whole_disk_ext4
    family_for: FamilyResolver = family_for


def _resolve_entry(spec: RootfsBuildSpec) -> RootfsCatalogEntry:
    """Resolve the catalog row for ``spec.name``, synthesizing a virt-builder fallback if absent.

    A spec built the old way (``build-fs`` without ``--image``, until Task 6) carries a name that
    may not be a catalog row; it falls back to a ``virt-builder:<distro>-<releasever>`` template +
    the rhel family so the legacy CLI keeps building. A malformed catalog still raises.
    """
    entry = load_rootfs_catalog().get(spec.name)
    if entry is not None:
        return entry
    return RootfsCatalogEntry(
        name=spec.name,
        distro=spec.distro,
        version=spec.releasever,
        family="rhel",
        arch=spec.arch,
        kind=_kind_for(spec.capabilities),
        source=VirtBuilderSource(template=f"{spec.distro}-{spec.releasever}"),
    )


def _kind_for(capabilities: tuple[str, ...]) -> str:
    """Derive the image ``kind`` for a synthesized fallback row from its capability tags."""
    return "build" if "build" in capabilities else "debug"


def _source_digest(source: RootfsSource) -> str:
    """Render the provenance ``source_image_digest`` for a resolved base source."""
    if isinstance(source, CloudImageSource):
        return f"cloud-image:{source.url}@sha256:{source.sha256}"
    return f"virt-builder:{source.template}"


class LocalLibvirtRootfsBuildPlane:
    """The realized local-libvirt :class:`~kdive.images.planes.base.RootfsBuildPlane`."""

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        size: str = _DEFAULT_IMAGE_SIZE,
        tools: RootfsBuildTools | None = None,
    ) -> None:
        self._workspace = workspace or Path(_DEFAULT_WORKSPACE)
        self._size = size
        self._tools = tools or RootfsBuildTools()

    @classmethod
    def from_env(cls, *, workspace: Path | None = None) -> LocalLibvirtRootfsBuildPlane:
        """Build with the real libguestfs seams; does not run any tool or touch the network.

        Args:
            workspace: Override the default build/publish workspace (``build-fs --workspace``)
                so an operator can build under a user-writable path without a privileged
                ``mkdir`` of the root-owned default.
        """
        return cls(workspace=workspace)

    def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
        """Build the kdive-ready rootfs qcow2 for ``spec``; record pinned-input provenance.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unresolvable authorized key, an
                unknown family, or an unreachable/mismatched base; ``MISSING_DEPENDENCY`` for
                absent libguestfs tooling; ``PROVISIONING_FAILURE`` for a build-stage failure.
        """
        validate_image_name(spec.name)
        authorized_key = self._tools.resolve_authorized_key()
        if not authorized_key.is_file():
            raise CategorizedError(
                "resolved SSH public key is not a readable file; cannot build the rootfs image",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"authorized_key": str(authorized_key)},
            )
        entry = _resolve_entry(spec)
        family = self._tools.family_for(entry.family)
        with build_workspace(self._workspace, prefix="rootfs-build-") as work_dir:
            scratch = work_dir / "scratch.qcow2"
            self._tools.acquire_base(
                entry.source,
                scratch,
                releasever=spec.releasever,
                arch=spec.arch,
                virt_builder=self._tools.virt_builder,
                downloader=self._tools.downloader,
            )
            self._customize(scratch, family, spec=spec, entry=entry, authorized_key=authorized_key)
            staged = work_dir / f"{spec.name}.qcow2"
            self._tools.repack_whole_disk_ext4(scratch=scratch, qcow2=staged, size=self._size)
            family.normalize(staged)
            qcow2 = publish_qcow2(self._workspace, image_name=spec.name, scratch=staged)
        digest = digest_file(qcow2)
        return RootfsBuildOutput(
            qcow2_path=qcow2,
            digest=digest,
            provenance=_provenance(spec, entry, size=self._size, authorized_key=authorized_key),
        )

    def _customize(
        self,
        scratch: Path,
        family: FamilyCustomizer,
        *,
        spec: RootfsBuildSpec,
        entry: RootfsCatalogEntry,
        authorized_key: Path,
    ) -> None:
        """Render the kdive-ready unit, build the family argv, and run ``virt-customize``."""
        cleanup: list[Path] = []
        with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as unit:
            unit.write(_READINESS_UNIT)
            unit_path = Path(unit.name)
        cleanup.append(unit_path)
        try:
            ctx = CustomizeContext(
                kind=entry.kind,
                packages=spec.packages,
                authorized_key=authorized_key,
                readiness_unit_path=unit_path,
                is_cloud_image=isinstance(entry.source, CloudImageSource),
                cleanup=cleanup,
            )
            self._tools.customize(scratch, family.customize_argv(ctx))
        finally:
            for path in cleanup:
                path.unlink(missing_ok=True)


def _provenance(
    spec: RootfsBuildSpec, entry: RootfsCatalogEntry, *, size: str, authorized_key: Path
) -> dict[str, object]:
    """Record the pinned inputs and build args that produced the image (falsifiable contract).

    ``source_image_digest`` names the resolved catalog base source: ``virt-builder:<template>`` or
    ``cloud-image:<url>@sha256:<digest>`` (the latter a verified pin). The image's verifiable
    identity is the output qcow2 content digest
    (:func:`kdive.images.planes._build_common.digest_file`), per ADR-0092.
    """
    return {
        "plane": "local-libvirt",
        "distro": spec.distro,
        "releasever": spec.releasever,
        "packages": list(spec.packages),
        "source_image_digest": _source_digest(entry.source),
        "capabilities": list(spec.capabilities),
        "arch": spec.arch,
        "image_size": size,
        "authorized_key_name": authorized_key.name,
        "readiness_marker": _READINESS_MARKER,
        "layout": "whole-disk-ext4-qcow2",
        "guest_selinux": _GUEST_SELINUX,
    }
