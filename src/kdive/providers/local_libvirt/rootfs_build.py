"""The in-process local-libvirt rootfs build plane (M2.4/2, ADR-0092, ADR-0251).

`LocalLibvirtRootfsBuildPlane` builds a kdive-ready rootfs from the declarative rootfs catalog
(`kdive.images.rootfs.catalog`) plus a per-family customizer seam, recording **pinned-input
provenance** into the :class:`RootfsBuildOutput`. The pipeline is:

1. resolve the catalog row for ``spec.name`` (its base ``source`` + ``family``);
2. :func:`kdive.images.rootfs.base_source.acquire_base` materializes the base into a scratch
   qcow2 — a ``virt-builder`` template or a sha256-pinned cloud image;
3. ``virt-customize`` applies the family's argv (``family.customize_argv``): install the package
   set, enable ``sshd``/``kdump``, stage the kdive-ready unit, etc. — the image bakes no
   authorized key (ADR-0289, #963); the per-System bootstrap key is injected at provision time;
4. ``virt-tar-out`` + ``virt-make-fs --type=ext4 --format=qcow2`` repack the root tree into a
   **no-partition-table whole-disk ext4 qcow2** — the only layout the direct-kernel boot provider
   mounts (``root=/dev/vda``, no initramfs, ADR-0030);
5. ``family.normalize`` rewrites fstab to a lone ``/``, removes crypttab, and sets the family's
   SELinux policy (rhel: permissive + first-boot relabel) via guestfish;
6. ``verify_cloud_init`` runs an offline guestfish self-check on the staged image, asserting the
   cloud-init first-boot wiring is actually baked in (ADR-0288) — the guard against a silent no-op
   that CI cannot catch by booting.

The slow libguestfs/network seams are **injected** (:class:`RootfsBuildTools`) and default to the
real implementations, so unit tests cover the orchestration/provenance contract without libguestfs,
qemu, or the network; the real path is exercised on the operator-run live-stack path. ``build()``
is synchronous — the worker offloads the whole call via ``asyncio.to_thread`` (ADR-0092).
"""

from __future__ import annotations

import logging
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import libvirt

import kdive.config as config
from kdive.domain.catalog.resource_capabilities import (
    GUEST_ARCHES_KEY,
    ResourceCapabilities,
    resolve_accel_emulator,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES, arch_traits
from kdive.images.drgn_support import DrgnVersion
from kdive.images.families import family_for
from kdive.images.families._fedora_customize import (
    READINESS_MARKER as _READINESS_MARKER,
)
from kdive.images.families._fedora_customize import (
    readiness_unit as _readiness_unit,
)
from kdive.images.families.base import CustomizeContext, FamilyCustomizer
from kdive.images.families.renderers import (
    partition_steps,
    render_argv,
    render_firstboot_script,
    render_firstboot_unit,
)
from kdive.images.families.steps import (
    Mkdir,
    StageFile,
    Step,
    UploadFile,
    WriteFile,
)
from kdive.images.kdump_support import MakedumpfileVersion
from kdive.images.planes._build_common import (
    build_workspace,
    digest_file,
    publish_qcow2,
    run_guestfs_tool,
    validate_image_name,
)
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildProvenance, RootfsBuildSpec
from kdive.images.planes.provenance_probes import (
    DEFAULT_BOOT_ENTRIES_PROBE,
    DEFAULT_DRGN_PROBE,
    DEFAULT_KERNEL_CONFIG_PROBE,
    DEFAULT_MAKEDUMPFILE_PROBE,
    DEFAULT_OS_RELEASE_PROBE,
    DEFAULT_VERSION_INSPECT,
    BootEntriesProbeSeam,
    DrgnProbeSeam,
    KernelConfigProbeSeam,
    MakedumpfileProbeSeam,
    OsReleaseProbeSeam,
    VersionInspectSeam,
)
from kdive.images.rootfs.base_source import Downloader, _real_download, acquire_base
from kdive.images.rootfs.catalog import (
    CloudImageSource,
    RootfsCatalogEntry,
    RootfsSource,
    resolve_rootfs_entry,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.baseline_kernel import (
    ExtractBaselineKernel,
    _real_extract_baseline_kernel,
    baseline_kernel_names,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.customization_boot import (
    CUSTOMIZE_SCRIPT_PATH,
    CUSTOMIZE_UNIT,
    FAIL_MARKER,
    OK_MARKER,
    CustomizationBootSeams,
    _real_run_guestfish,
    seal_customized_image,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.customization_boot import (
    run_customization_boot as _run_customization_boot,
)
from kdive.providers.local_libvirt.lifecycle.xml import render_customization_domain_xml
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.shared.build_timeouts import SLOW_BUILD_TOOL_TIMEOUT_S
from kdive.providers.shared.libvirt_xml import parse_guest_arches

_log = logging.getLogger(__name__)

_DEFAULT_WORKSPACE = "/var/lib/kdive/build/images"
_DEFAULT_IMAGE_SIZE = "6G"
_ACQUIRE_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S
_CUSTOMIZE_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S
_REPACK_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S


def _run_libguestfs_tool(argv: list[str], *, stage: str, timeout_s: int) -> None:
    """Run a fixed-argv libguestfs tool, mapping failure onto a categorized error."""
    run_guestfs_tool(
        argv,
        stage=stage,
        timeout_s=timeout_s,
        missing_message=f"{argv[0]} is not installed; cannot build the rootfs image",
    )


def _real_virt_builder(*, template: str, output: Path) -> None:  # pragma: no cover - live_vm
    """Acquire a base scratch image from a ``virt-builder`` template (the acquire_base seam)."""
    _run_libguestfs_tool(
        ["virt-builder", template, "--format", "qcow2", "--output", str(output)],
        stage="virt-builder",
        timeout_s=_ACQUIRE_TIMEOUT_S,
    )


def _real_virt_customize(qcow2: Path, argv: list[str]) -> None:  # pragma: no cover - live_vm
    """Apply the family's customization argv to the acquired scratch via ``virt-customize``."""
    _run_libguestfs_tool(
        ["virt-customize", "-a", str(qcow2), *argv],
        stage="virt-customize",
        timeout_s=_CUSTOMIZE_TIMEOUT_S,
    )


def _real_repack_whole_disk_ext4(*, scratch: Path, qcow2: Path, size: str) -> None:
    """Repack the customized root tree into a no-partition-table whole-disk ext4 qcow2."""
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as handle:
        tar_path = Path(handle.name)
    try:
        _run_libguestfs_tool(
            ["virt-tar-out", "-a", str(scratch), "/", str(tar_path)],
            stage="virt-tar-out",
            timeout_s=_REPACK_TIMEOUT_S,
        )
        _run_libguestfs_tool(
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


def _grant_hypervisor_traversal(work_dir: Path) -> None:
    """Let the ``qemu:///system`` hypervisor reach the boot disk + kernel in the build workspace.

    The customization boot attaches the in-progress disk and the extracted baseline
    kernel/initrd straight from the per-build workspace directory, which
    ``tempfile.TemporaryDirectory`` creates mode ``0700``. libvirt's dynamic ownership relabels
    and chowns the disk *file* at domain start, but never widens parent directories, so without
    this the non-root ``qemu`` process cannot traverse the scratch directory and ``createXML``
    fails with an opaque ``Cannot access storage file … Permission denied (as uid:107)``. Add
    ``o+x`` to every directory in the tree (path traversal) and ``o+r`` to every file (the
    read-only kernel/initrd; libvirt makes the disk writable via chown). Owner-only directory
    listing is preserved — no ``o+r`` on directories.
    """
    for path in (work_dir, *work_dir.rglob("*")):
        mode = path.stat().st_mode
        widened = mode | (stat.S_IXOTH if path.is_dir() else stat.S_IROTH)
        if widened != mode:
            path.chmod(widened)


type AcquireBase = Callable[..., None]
type VirtBuilder = Callable[..., None]
type Customize = Callable[[Path, list[str]], None]
type RepackWholeDiskExt4 = Callable[..., None]
type FamilyResolver = Callable[[str], FamilyCustomizer]
type VerifyCloudInit = Callable[[Path], None]
type InjectOffline = Callable[[Path, list[Step], str, str], None]
type RunCustomizationBoot = Callable[..., None]
type SealCustomizedImage = Callable[..., None]
type ResolveAccel = Callable[[str], tuple[str, str | None]]

_INJECT_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S


def _real_resolve_accel(arch: str) -> tuple[str, str | None]:  # pragma: no cover - live_vm
    """Resolve ``(accel, emulator)`` for ``arch`` from live libvirt capabilities (ADR-0340/0345).

    Mirrors the provisioning resolver: reads ``getCapabilities``, routes it through the shared
    :func:`resolve_accel_emulator` branch, and fails **open** to ``("kvm", None)`` when the host
    advertises no guest arches (not re-discovered since ADR-0338) — the legacy x86-KVM path. A
    connection / ``getCapabilities`` ``libvirtError`` is categorized ``INFRASTRUCTURE_FAILURE``
    (mirroring the provisioning resolver) rather than propagating raw.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` for a libvirt fault connecting to or reading
            capabilities from the host.
    """
    try:
        conn = libvirt.open(config.require(LIBVIRT_URI))
        try:
            caps_xml = conn.getCapabilities()
        finally:
            conn.close()
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "libvirt error reading host capabilities to resolve the customization-boot accelerator",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"operation": "resolve_accel"},
        ) from exc
    guest_arches = ResourceCapabilities.from_mapping(
        {GUEST_ARCHES_KEY: parse_guest_arches(caps_xml, SUPPORTED_ARCHES)}
    ).guest_arches()
    resolved = resolve_accel_emulator(guest_arches, arch)
    return resolved if resolved is not None else ("kvm", None)


def _guestfish_file_op(step: Step, cleanup: list[Path]) -> list[str]:  # pragma: no cover - live_vm
    """Render one file-op ``Step`` to guestfish commands, staging host content as needed."""
    match step:
        case Mkdir(path):
            return [f"mkdir-p {path}"]
        case WriteFile(path, content) | StageFile(path, content):
            return [f"upload {_stage_content(content, cleanup)} {path}"]
        case UploadFile(host_src, dest, mode):
            lines = [f"upload {host_src} {dest}"]
            if mode is not None:
                lines.append(f"chmod {mode} {dest}")
            return lines
    return []


def _stage_content(content: str, cleanup: list[Path]) -> Path:  # pragma: no cover - live_vm
    """Write ``content`` to a delete-on-cleanup host tempfile for guestfish upload."""
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        handle.write(content)
        staged = Path(handle.name)
    cleanup.append(staged)
    return staged


def _inject_offline_script(
    file_ops: list[Step], firstboot_script: str, firstboot_unit: str, cleanup: list[Path]
) -> str:  # pragma: no cover - live_vm
    """Render the guestfish script that offline-injects + enables the firstboot customization."""
    unit_path = f"/etc/systemd/system/{CUSTOMIZE_UNIT}"
    wants_link = f"/etc/systemd/system/multi-user.target.wants/{CUSTOMIZE_UNIT}"
    lines: list[str] = []
    for step in file_ops:
        lines += _guestfish_file_op(step, cleanup)
    lines += [
        f"upload {_stage_content(firstboot_script, cleanup)} {CUSTOMIZE_SCRIPT_PATH}",
        f"chmod 0755 {CUSTOMIZE_SCRIPT_PATH}",
        f"upload {_stage_content(firstboot_unit, cleanup)} {unit_path}",
        "mkdir-p /etc/systemd/system/multi-user.target.wants",
        f"ln-s ../{CUSTOMIZE_UNIT} {wants_link}",
        "rm-f /etc/cloud/cloud-init.disabled",
    ]
    return "\n".join(lines) + "\n"


def _real_inject_offline(
    qcow2: Path, file_ops: list[Step], firstboot_script: str, firstboot_unit: str
) -> None:  # pragma: no cover - live_vm
    """Offline-inject the file-ops, firstboot script/unit, and bootstrap symlink via guestfish.

    Applies the partitioned file-ops, writes the firstboot script (mode ``0755``) and its systemd
    unit, enables the unit **offline** with a ``multi-user.target.wants`` symlink (no in-guest
    ``systemctl`` — arch-safe under a cross-arch appliance), and removes
    ``/etc/cloud/cloud-init.disabled`` so cloud-init runs and DHCPs the egress NIC (ADR-0345).
    """
    cleanup: list[Path] = []
    script = _inject_offline_script(file_ops, firstboot_script, firstboot_unit, cleanup)
    try:
        run_guestfs_tool(
            ["guestfish", "--rw", "-a", str(qcow2), "-i"],
            stage="customize-inject",
            timeout_s=_INJECT_TIMEOUT_S,
            missing_message="guestfish is not installed; cannot inject the firstboot customization",
            failure_message="offline firstboot injection failed",
            input_text=script,
        )
    finally:
        for path in cleanup:
            path.unlink(missing_ok=True)


def _real_run_customization_boot(
    build_id: UUID, domain_xml: str, *, accel: str
) -> None:  # pragma: no cover - live_vm
    """Boot the transient customization domain with the live host seams (ADR-0345)."""
    _run_customization_boot(
        build_id, domain_xml, accel=accel, seams=CustomizationBootSeams.from_env()
    )


def _real_seal_customized_image(
    qcow2: Path, *, unit_name: str, selinux: bool
) -> None:  # pragma: no cover - live_vm
    """Offline-seal the customized image with the live guestfish runner (ADR-0345)."""
    seal_customized_image(
        qcow2, unit_name=unit_name, selinux=selinux, run_guestfish=_real_run_guestfish
    )


_CLOUD_INIT_CHECK_SENTINEL = "__KDIVE_CI__"


def _run_cloud_init_guestfish(qcow2: Path, script: str) -> str:
    """Run a read-only native guestfish check script against ``qcow2``, returning stdout."""
    return run_guestfs_tool(
        ["guestfish", "--ro", "-a", str(qcow2), "-i"],
        stage="cloud-init-self-check",
        timeout_s=_REPACK_TIMEOUT_S,
        missing_message="guestfish is not installed; cannot verify cloud-init in the rootfs",
        input_text=script,
    )


def _fail_cloud_init(detail: str) -> CategorizedError:
    return CategorizedError(
        f"built image failed the cloud-init first-boot self-check (ADR-0288): {detail}",
        category=ErrorCategory.PROVISIONING_FAILURE,
        details={"check": detail},
    )


def _real_verify_cloud_init(qcow2: Path) -> None:  # pragma: no cover - live_vm
    """Assert cloud-init first-boot is correctly baked into the built image (ADR-0288).

    A version-robust offline guard for silent no-ops CI cannot catch by booting. Every check is a
    libguestfs-**native** predicate (``exists``/``is-file``/``grep`` — appliance operations on
    guest *data*), not a ``sh`` guest command: a ``sh 'test'``/``sh 'grep'`` would exec the guest's
    own binaries in the host-arch appliance and fail ``Exec format error`` on a foreign-arch image
    (the ADR-0345 boot path builds ppc64le on an x86_64 host). The checks: the kdive drop-in and
    NoCloud seed exist, cloud-init is installed, nothing re-disables it, the drop-in keeps
    ``resize_rootfs`` on (ADR-0312), and no ``cloud.cfg.d`` drop-in disables cloud-init networking.
    It does **not** assert specific unit-enable state — unit names vary across cloud-init versions —
    nor cloud-init's executable bit (existence is the arch-safe signal; the vendor ships it +x).
    """
    from kdive.images.families._fedora_customize import KDIVE_CLOUD_CFG_PATH, NOCLOUD_SEED_DIR

    s = _CLOUD_INIT_CHECK_SENTINEL
    script = "\n".join(
        f"echo {s}\n{cmd}"
        for cmd in (
            f"exists {KDIVE_CLOUD_CFG_PATH}",
            f"exists {NOCLOUD_SEED_DIR}/meta-data",
            "exists /etc/cloud/cloud-init.disabled",
            "is-file /usr/bin/cloud-init",
            f"grep resize_rootfs {KDIVE_CLOUD_CFG_PATH}",
            "ls /etc/cloud/cloud.cfg.d/",
        )
    )
    segs = [seg.strip() for seg in _run_cloud_init_guestfish(qcow2, script + "\n").split(s)][1:]
    cfg_exists, seed_exists, disabled, cloud_init, resize, cfgd = segs
    if cfg_exists != "true":
        raise _fail_cloud_init(f"kdive cloud drop-in {KDIVE_CLOUD_CFG_PATH} is missing")
    if seed_exists != "true":
        raise _fail_cloud_init("NoCloud seed meta-data is missing")
    if disabled != "false":
        raise _fail_cloud_init("/etc/cloud/cloud-init.disabled re-disables cloud-init")
    if cloud_init != "true":
        raise _fail_cloud_init("/usr/bin/cloud-init is not installed")
    if "true" not in resize:
        raise _fail_cloud_init("drop-in does not keep resize_rootfs on (ADR-0312)")
    _assert_no_network_disable(qcow2, cfgd)


def _assert_no_network_disable(qcow2: Path, cfgd: str) -> None:  # pragma: no cover - live_vm
    """Fail if any ``cloud.cfg.d`` ``*.cfg`` drop-in disables cloud-init networking.

    The former ``grep -rqs 'config: disabled'`` was a guest ``sh`` command (cross-arch-broken);
    this greps each drop-in with libguestfs-native ``grep`` (a match means a drop-in carries
    ``network: {config: disabled}``). ``.cfg`` only mirrors cloud-init's own drop-in selection.
    """
    cfgs = [f for f in cfgd.split() if f.endswith(".cfg")]
    if not cfgs:
        return
    grep_script = "\n".join(
        f"grep config:.*disabled /etc/cloud/cloud.cfg.d/{name}" for name in cfgs
    )
    if _run_cloud_init_guestfish(qcow2, grep_script + "\n").strip():
        raise _fail_cloud_init("a cloud.cfg.d drop-in disables cloud-init networking")


@dataclass(frozen=True, slots=True)
class RootfsBuildTools:
    """The injectable build seams; default to the real libguestfs/network implementations."""

    acquire_base: AcquireBase = acquire_base
    virt_builder: VirtBuilder = _real_virt_builder
    downloader: Downloader = _real_download
    customize: Customize = _real_virt_customize
    repack_whole_disk_ext4: RepackWholeDiskExt4 = _real_repack_whole_disk_ext4
    family_for: FamilyResolver = family_for
    inspect_versions: VersionInspectSeam = DEFAULT_VERSION_INSPECT
    probe_makedumpfile: MakedumpfileProbeSeam = DEFAULT_MAKEDUMPFILE_PROBE
    probe_drgn: DrgnProbeSeam = DEFAULT_DRGN_PROBE
    probe_boot_entries: BootEntriesProbeSeam = DEFAULT_BOOT_ENTRIES_PROBE
    probe_os_release: OsReleaseProbeSeam = DEFAULT_OS_RELEASE_PROBE
    probe_kernel_config: KernelConfigProbeSeam = DEFAULT_KERNEL_CONFIG_PROBE
    verify_cloud_init: VerifyCloudInit = _real_verify_cloud_init
    inject_offline: InjectOffline = _real_inject_offline
    run_customization_boot: RunCustomizationBoot = _real_run_customization_boot
    seal_customized_image: SealCustomizedImage = _real_seal_customized_image
    extract_baseline_kernel: ExtractBaselineKernel = _real_extract_baseline_kernel
    resolve_accel: ResolveAccel = _real_resolve_accel


def _resolve_entry(spec: RootfsBuildSpec) -> RootfsCatalogEntry:
    return resolve_rootfs_entry(spec.name)


def _source_digest(source: RootfsSource) -> str:
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
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown family or an
                unreachable/mismatched base; ``MISSING_DEPENDENCY`` for absent libguestfs
                tooling; ``PROVISIONING_FAILURE`` for a build-stage failure.
        """
        validate_image_name(spec.name)
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
            staged = work_dir / f"{spec.name}.qcow2"
            probe_src = self._customize_and_stage(
                scratch, staged, family, work_dir, spec=spec, entry=entry
            )
            installed = self._inspect_installed(probe_src)
            package_versions = {n: installed[n] for n in spec.packages if n in installed}
            makedumpfile_version = self._capture_makedumpfile(probe_src, installed)
            drgn_version = self._capture_drgn(probe_src, installed)
            boot_facts = self._capture_boot_facts(probe_src)
            os_release = self._capture_os_release(probe_src)
            qcow2 = publish_qcow2(self._workspace, image_name=spec.name, scratch=staged)
        digest = digest_file(qcow2)
        return RootfsBuildOutput(
            qcow2_path=qcow2,
            digest=digest,
            kernel_config=boot_facts.kernel_config,
            provenance=RootfsBuildProvenance.local_libvirt(
                spec,
                source_image_digest=_source_digest(entry.source),
                image_size=self._size,
                readiness_marker=_READINESS_MARKER,
                layout="whole-disk-ext4-qcow2",
                guest_mac=family.guest_mac,
                package_versions=package_versions,
                makedumpfile_version=makedumpfile_version,
                drgn_version=drgn_version,
                boot_kernel_count=boot_facts.boot_kernel_count,
                default_kernel_version=boot_facts.default_kernel_version,
                os_release=os_release,
            ).to_dict(),
        )

    def _customize_and_stage(
        self,
        scratch: Path,
        staged: Path,
        family: FamilyCustomizer,
        work_dir: Path,
        *,
        spec: RootfsBuildSpec,
        entry: RootfsCatalogEntry,
    ) -> Path:
        """Customize + stage the image per the family's ``customize_via``; return the probe source.

        ``virt_customize`` (debian) keeps the historical order and probes provenance from the
        customized ``scratch``; ``boot`` (rhel) repacks + normalizes first, then boots the image to
        self-customize and seals it, so provenance is probed from the ``staged`` image (ADR-0345).
        """
        if family.customize_via == "boot":
            return self._build_via_boot(scratch, staged, family, work_dir, spec=spec, entry=entry)
        return self._build_via_virt_customize(scratch, staged, family, spec=spec, entry=entry)

    def _build_via_virt_customize(
        self,
        scratch: Path,
        staged: Path,
        family: FamilyCustomizer,
        *,
        spec: RootfsBuildSpec,
        entry: RootfsCatalogEntry,
    ) -> Path:
        """The virt-customize path: customize the scratch, repack, normalize; probe from scratch."""
        self._customize(scratch, family, spec=spec, entry=entry)
        self._tools.repack_whole_disk_ext4(scratch=scratch, qcow2=staged, size=self._size)
        family.normalize(staged)
        self._tools.verify_cloud_init(staged)
        return scratch

    def _build_via_boot(
        self,
        scratch: Path,
        staged: Path,
        family: FamilyCustomizer,
        work_dir: Path,
        *,
        spec: RootfsBuildSpec,
        entry: RootfsCatalogEntry,
    ) -> Path:
        """The boot path: repack + normalize (no relabel), boot to self-customize, seal.

        Provenance is probed from ``staged`` (the returned path), not ``scratch``.
        The order is reversed from the virt-customize path: the base is repacked to whole-disk ext4
        and normalized (leaving ``/.autorelabel`` to the seal) *before* customization, then the
        image boots its own kernel to install packages and run the firstboot script (ADR-0345).
        """
        self._tools.repack_whole_disk_ext4(scratch=scratch, qcow2=staged, size=self._size)
        family.normalize(staged, relabel=False)
        self._boot_customize(staged, family, work_dir, spec=spec, entry=entry)
        self._tools.seal_customized_image(
            staged,
            unit_name=CUSTOMIZE_UNIT,
            selinux=family.guest_mac.startswith("selinux"),
        )
        self._tools.verify_cloud_init(staged)
        return staged

    def _boot_customize(
        self,
        staged: Path,
        family: FamilyCustomizer,
        work_dir: Path,
        *,
        spec: RootfsBuildSpec,
        entry: RootfsCatalogEntry,
    ) -> None:
        """Inject the firstboot customization offline, then boot the transient domain (ADR-0345)."""
        cleanup: list[Path] = []
        unit_path = self._render_readiness_unit(family, spec, cleanup)
        try:
            ctx = self._context(unit_path, spec=spec, entry=entry)
            file_ops, exec_ops = partition_steps(family.customize_steps(ctx))
            script = render_firstboot_script(
                exec_ops,
                console_device=arch_traits(spec.arch).console_device,
                unit_name=CUSTOMIZE_UNIT,
                script_path=CUSTOMIZE_SCRIPT_PATH,
                ok_marker=OK_MARKER,
                fail_marker=FAIL_MARKER,
            )
            unit = render_firstboot_unit(script_path=CUSTOMIZE_SCRIPT_PATH)
            self._tools.inject_offline(staged, file_ops, script, unit)
            self._run_boot(staged, work_dir, spec.arch)
        finally:
            for path in cleanup:
                path.unlink(missing_ok=True)

    def _run_boot(self, staged: Path, work_dir: Path, arch: str) -> None:
        """Extract the baseline kernel, render the build domain XML, and drive the boot to ok."""
        baseline = self._tools.extract_baseline_kernel(staged, work_dir / "baseline", None)
        _grant_hypervisor_traversal(work_dir)
        accel, emulator = self._tools.resolve_accel(arch)
        build_id = uuid4()
        xml = render_customization_domain_xml(
            build_id,
            arch=arch,
            disk_path=str(staged),
            kernel_path=baseline.kernel,
            initrd_path=baseline.initrd,
            accel=accel,
            emulator=emulator,
        )
        self._tools.run_customization_boot(build_id, xml, accel=accel)

    def _inspect_installed(self, scratch: Path) -> dict[str, str]:
        """The full installed ``{name: version}`` map; ``{}`` (logged) on inspector failure.

        Inspects the customized scratch image (a normal bootable OS disk, still present in the
        workspace). Version capture is advisory: a categorized inspector failure degrades to an
        empty map so the build still publishes (ADR-0252). Callers filter to the requested set for
        ``package_versions`` and consult the full map for the makedumpfile fallback (ADR-0253).
        """
        try:
            return self._tools.inspect_versions(scratch)
        except CategorizedError:
            _log.warning(
                "package-version capture failed; provenance omits package_versions", exc_info=True
            )
            return {}

    def _capture_makedumpfile(self, scratch: Path, installed: dict[str, str]) -> str | None:
        """The image's makedumpfile version: the binary probe, else the package-version fallback.

        Reads the build-written ``makedumpfile --version`` marker (authoritative across families,
        EL8/EL9 included); if that yields nothing, falls back to a standalone ``makedumpfile``
        package version from the full installed map (Fedora/Debian). Either source is parsed to a
        canonical dotted version. Advisory like package capture: any failure / unparseable / absent
        source degrades to ``None`` so the build still publishes (ADR-0253).
        """
        try:
            raw = self._tools.probe_makedumpfile(scratch)
        except CategorizedError:
            _log.warning(
                "makedumpfile probe failed; trying package-version fallback", exc_info=True
            )
            raw = None
        for candidate in (raw, installed.get("makedumpfile")):
            if not candidate:
                continue
            try:
                return str(MakedumpfileVersion.parse(candidate))
            except ValueError:
                _log.warning("makedumpfile version %r did not parse; skipping", candidate)
        return None

    def _capture_drgn(self, scratch: Path, installed: dict[str, str]) -> str | None:
        """The image's drgn version: the binary marker probe, else the package-version fallback.

        Reads the build-written ``drgn --version`` marker (authoritative — the binary's own
        report); if that yields nothing, falls back to the installed drgn package version (``drgn``
        on rhel/fedora, ``python3-drgn`` on debian) from the full inspected map. Either source is
        parsed to a canonical dotted version — the ADR-0328 ``live_drgn`` operand. Advisory like the
        makedumpfile capture: any failure / unparseable / absent source degrades to ``None`` so the
        build still publishes and ``live_drgn`` honestly reports ``unverified`` (ADR-0334).
        """
        try:
            raw = self._tools.probe_drgn(scratch)
        except CategorizedError:
            _log.warning("drgn probe failed; trying package-version fallback", exc_info=True)
            raw = None
        for candidate in (raw, installed.get("drgn"), installed.get("python3-drgn")):
            if not candidate:
                continue
            try:
                return str(DrgnVersion.parse(candidate))
            except ValueError:
                _log.warning("drgn version %r did not parse; skipping", candidate)
        return None

    def _capture_os_release(self, scratch: Path) -> dict[str, str] | None:
        """The built image's OS identity from ``/etc/os-release``, or ``None`` (ADR-0311).

        Reads the raw os-release text via the injected probe and parses ``ID``/``VERSION_ID``/
        ``PRETTY_NAME``. Advisory like the makedumpfile/boot-kernel captures: any probe failure
        (``CategorizedError``), an absent file (``None`` text), or a body with no ``ID`` degrades to
        ``None`` so the build still publishes and the operand is simply omitted.
        """
        try:
            raw = self._tools.probe_os_release(scratch)
        except CategorizedError:
            _log.warning("os-release probe failed; provenance omits os_release")
            return None
        return _parse_os_release(raw) if raw is not None else None

    def _capture_boot_facts(self, scratch: Path) -> _BootFacts:
        """Boot facts from one ``/boot`` listing: kernel count, default version, and config.

        Lists ``/boot`` once via the injected probe and derives (a) ``boot_kernel_count`` via
        ``baseline_kernel_names`` — the same rule the fail-closed provision selection uses, so the
        count predicts whether a direct-kernel provision will succeed (exactly one is provisionable,
        ADR-0295); (b) the ``default_kernel_version`` — the lone non-rescue kernel, else ``None``
        when zero/many (ambiguous); and (c) the ``/boot/config-<ver>`` bytes for that version via
        ``probe_kernel_config`` (ADR-0317). Advisory like the makedumpfile capture: any probe
        failure (``CategorizedError``) or an unproduceable listing (``None``) degrades every fact to
        absent so the build still publishes. ``boot_kernel_count`` is ``0`` for a kernel-less
        ``/boot`` (a meaningful "not provisionable" operand), distinct from ``None`` (unknown).
        """
        try:
            entries = self._tools.probe_boot_entries(scratch)
        except CategorizedError:
            _log.warning("boot-entries probe failed; provenance omits boot facts")
            return _BootFacts(None, None, None)
        if entries is None:
            return _BootFacts(None, None, None)
        kernels = baseline_kernel_names(entries)
        # The default kernel is unambiguous only when /boot holds exactly one non-rescue kernel;
        # zero/many omit the version (and skip the config probe), matching boot_kernel_count.
        version = kernels[0][len("vmlinuz-") :] if len(kernels) == 1 else None
        config = self._capture_kernel_config(scratch, version)
        return _BootFacts(len(kernels), version, config)

    def _capture_kernel_config(self, scratch: Path, version: str | None) -> bytes | None:
        """The image's ``/boot/config-<version>`` bytes verbatim, or ``None`` (ADR-0317).

        Only probed when ``version`` is known (a single baseline kernel). The probe returns raw
        bytes so the offered config is byte-identical to the on-image file. Advisory: a probe
        failure (``CategorizedError``) or an absent config degrades to ``None`` so the build ships.
        """
        if version is None:
            return None
        try:
            return self._tools.probe_kernel_config(scratch, version)
        except CategorizedError:
            _log.warning(
                "kernel-config probe failed for %s; provenance omits the config offer",
                version,
                exc_info=True,
            )
            return None

    def _customize(
        self,
        scratch: Path,
        family: FamilyCustomizer,
        *,
        spec: RootfsBuildSpec,
        entry: RootfsCatalogEntry,
    ) -> None:
        """Render the kdive-ready unit and the family steps to argv, then run ``virt-customize``."""
        cleanup: list[Path] = []
        unit_path = self._render_readiness_unit(family, spec, cleanup)
        try:
            ctx = self._context(unit_path, spec=spec, entry=entry)
            argv = render_argv(family.customize_steps(ctx), cleanup=cleanup)
            self._tools.customize(scratch, argv)
        finally:
            for path in cleanup:
                path.unlink(missing_ok=True)

    def _render_readiness_unit(
        self, family: FamilyCustomizer, spec: RootfsBuildSpec, cleanup: list[Path]
    ) -> Path:
        """Render the kdive-ready serial-readiness unit to a host tempfile (appended to cleanup)."""
        with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as unit:
            unit.write(_readiness_unit(family.kdump_unit, arch_traits(spec.arch).console_device))
            unit_path = Path(unit.name)
        cleanup.append(unit_path)
        return unit_path

    def _context(
        self,
        unit_path: Path,
        *,
        spec: RootfsBuildSpec,
        entry: RootfsCatalogEntry,
    ) -> CustomizeContext:
        """Build the :class:`CustomizeContext` both customize paths feed the family."""
        return CustomizeContext(
            kind=entry.kind,
            packages=spec.packages,
            readiness_unit_path=unit_path,
            is_cloud_image=isinstance(entry.source, CloudImageSource),
            distro=entry.distro,
            version=entry.version,
        )


_OS_RELEASE_KEYS = {"ID": "id", "VERSION_ID": "version_id", "PRETTY_NAME": "pretty_name"}


def _parse_os_release(text: str) -> dict[str, str] | None:
    """Parse os-release ``KEY=VALUE`` text into the recorded subset, or ``None`` (ADR-0311).

    Keeps ``ID``/``VERSION_ID``/``PRETTY_NAME`` (as ``id``/``version_id``/``pretty_name``),
    stripping matching single/double quotes and skipping blank and ``#``-comment lines. Returns
    ``None`` unless ``ID`` is present — a record without a distro id is not a usable identity —
    while ``version_id``/``pretty_name`` are included only when present (a rolling distro such as
    Debian testing may omit ``VERSION_ID``).
    """
    record: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        mapped = _OS_RELEASE_KEYS.get(key.strip())
        if mapped is not None:
            record[mapped] = value.strip().strip("\"'")
    return record if record.get("id") else None


@dataclass(frozen=True, slots=True)
class _BootFacts:
    """Facts derived from one read-only ``/boot`` listing (ADR-0295/0317)."""

    boot_kernel_count: int | None
    default_kernel_version: str | None
    kernel_config: bytes | None
