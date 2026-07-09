"""The in-process local-libvirt rootfs build plane (M2.4/2, ADR-0092, ADR-0251).

`LocalLibvirtRootfsBuildPlane` builds a kdive-ready rootfs from the declarative rootfs catalog
(`kdive.images.rootfs_catalog`) plus a per-family customizer seam, recording **pinned-input
provenance** into the :class:`RootfsBuildOutput`. The pipeline is:

1. resolve the catalog row for ``spec.name`` (its base ``source`` + ``family``);
2. :func:`kdive.images.base_source.acquire_base` materializes the base into a scratch qcow2 — a
   ``virt-builder`` template or a sha256-pinned cloud image;
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
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kdive.domain.errors import CategorizedError
from kdive.images.base_source import Downloader, _real_download, acquire_base
from kdive.images.families import family_for
from kdive.images.families._fedora_customize import (
    READINESS_MARKER as _READINESS_MARKER,
)
from kdive.images.families._fedora_customize import (
    readiness_unit as _readiness_unit,
)
from kdive.images.families.base import CustomizeContext, FamilyCustomizer
from kdive.images.kdump_support import MakedumpfileVersion
from kdive.images.planes._build_common import (
    DEFAULT_BOOT_ENTRIES_PROBE,
    DEFAULT_KERNEL_CONFIG_PROBE,
    DEFAULT_MAKEDUMPFILE_PROBE,
    DEFAULT_OS_RELEASE_PROBE,
    DEFAULT_VERSION_INSPECT,
    BootEntriesProbeSeam,
    KernelConfigProbeSeam,
    MakedumpfileProbeSeam,
    OsReleaseProbeSeam,
    VersionInspectSeam,
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
    resolve_rootfs_entry,
)
from kdive.providers.local_libvirt.lifecycle.baseline_kernel import baseline_kernel_names
from kdive.providers.shared.build_timeouts import SLOW_BUILD_TOOL_TIMEOUT_S

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


type AcquireBase = Callable[..., None]
type VirtBuilder = Callable[..., None]
type Customize = Callable[[Path, list[str]], None]
type RepackWholeDiskExt4 = Callable[..., None]
type FamilyResolver = Callable[[str], FamilyCustomizer]
type VerifyCloudInit = Callable[[Path], None]


def _real_verify_cloud_init(qcow2: Path) -> None:  # pragma: no cover - live_vm
    """Assert cloud-init first-boot is correctly baked into the built image (ADR-0288).

    A version-robust offline guard for silent no-ops CI cannot catch by booting. Each check runs
    in the guest via guestfish ``sh`` (which aborts the script non-zero on any failed check,
    verified empirically): the kdive drop-in and NoCloud seed exist, cloud-init is installed,
    nothing re-disables it, no cloud.cfg.d drop-in disables cloud-init networking, and the drop-in
    keeps ``resize_rootfs`` on (ADR-0312) so a built image cannot silently ship the disk-grow knob
    disabled. It does **not** assert specific unit-enable state — unit names vary across cloud-init
    versions (24.x renamed ``cloud-init.service`` to ``cloud-init-network.service``, live-found on
    Debian 13); the vendor cloud base ships cloud-init enabled and ``--install`` enables it via the
    package preset, so enumerating names would be fragile without adding safety.
    """
    from kdive.images.families._fedora_customize import (
        KDIVE_CLOUD_CFG_PATH,
        NOCLOUD_SEED_DIR,
    )

    checks = (
        f"test -e {KDIVE_CLOUD_CFG_PATH}",
        f"test -e {NOCLOUD_SEED_DIR}/meta-data",
        "test ! -e /etc/cloud/cloud-init.disabled",
        "test -x /usr/bin/cloud-init",
        '! grep -rqs "config:[[:space:]]*disabled" /etc/cloud/cloud.cfg.d/',
        f'grep -qs "resize_rootfs:[[:space:]]*true" {KDIVE_CLOUD_CFG_PATH}',
    )
    script = "".join(f"sh '{check}'\n" for check in checks)
    run_guestfs_tool(
        ["guestfish", "--ro", "-a", str(qcow2), "-i"],
        stage="cloud-init-self-check",
        timeout_s=_REPACK_TIMEOUT_S,
        missing_message="guestfish is not installed; cannot verify cloud-init in the rootfs",
        failure_message="built image failed the cloud-init first-boot self-check (ADR-0288)",
        input_text=script,
    )


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
    probe_boot_entries: BootEntriesProbeSeam = DEFAULT_BOOT_ENTRIES_PROBE
    probe_os_release: OsReleaseProbeSeam = DEFAULT_OS_RELEASE_PROBE
    probe_kernel_config: KernelConfigProbeSeam = DEFAULT_KERNEL_CONFIG_PROBE
    verify_cloud_init: VerifyCloudInit = _real_verify_cloud_init


def _resolve_entry(spec: RootfsBuildSpec) -> RootfsCatalogEntry:
    """Resolve the catalog row for ``spec.name``; uncataloged builds are rejected."""
    return resolve_rootfs_entry(spec.name)


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
            self._customize(scratch, family, spec=spec, entry=entry)
            staged = work_dir / f"{spec.name}.qcow2"
            self._tools.repack_whole_disk_ext4(scratch=scratch, qcow2=staged, size=self._size)
            family.normalize(staged)
            self._tools.verify_cloud_init(staged)
            installed = self._inspect_installed(scratch)
            package_versions = {n: installed[n] for n in spec.packages if n in installed}
            makedumpfile_version = self._capture_makedumpfile(scratch, installed)
            boot_facts = self._capture_boot_facts(scratch)
            qcow2 = publish_qcow2(self._workspace, image_name=spec.name, scratch=staged)
        digest = digest_file(qcow2)
        return RootfsBuildOutput(
            qcow2_path=qcow2,
            digest=digest,
            kernel_config=boot_facts.kernel_config,
            provenance=_provenance(
                spec,
                entry,
                family,
                size=self._size,
                package_versions=package_versions,
                makedumpfile_version=makedumpfile_version,
                boot_kernel_count=boot_facts.boot_kernel_count,
                default_kernel_version=boot_facts.default_kernel_version,
                os_release=self._capture_os_release(scratch),
            ),
        )

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
        count = len(baseline_kernel_names(entries))
        version = _default_kernel_version(entries)
        config = self._capture_kernel_config(scratch, version)
        return _BootFacts(count, version, config)

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
        """Render the kdive-ready unit, build the family argv, and run ``virt-customize``."""
        cleanup: list[Path] = []
        with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as unit:
            unit.write(_readiness_unit(family.kdump_unit))
            unit_path = Path(unit.name)
        cleanup.append(unit_path)
        try:
            ctx = CustomizeContext(
                kind=entry.kind,
                packages=spec.packages,
                readiness_unit_path=unit_path,
                is_cloud_image=isinstance(entry.source, CloudImageSource),
                cleanup=cleanup,
                distro=entry.distro,
                version=entry.version,
            )
            self._tools.customize(scratch, family.customize_argv(ctx))
        finally:
            for path in cleanup:
                path.unlink(missing_ok=True)


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


def _default_kernel_version(entries: list[str]) -> str | None:
    """The lone non-rescue ``vmlinuz-<ver>`` version in ``entries``, else ``None`` (ambiguous)."""
    kernels = baseline_kernel_names(entries)
    if len(kernels) != 1:
        return None
    return kernels[0][len("vmlinuz-") :]


def _provenance(
    spec: RootfsBuildSpec,
    entry: RootfsCatalogEntry,
    family: FamilyCustomizer,
    *,
    size: str,
    package_versions: dict[str, str],
    makedumpfile_version: str | None,
    boot_kernel_count: int | None,
    default_kernel_version: str | None,
    os_release: dict[str, str] | None,
) -> dict[str, object]:
    """Record the pinned inputs and build args that produced the image (falsifiable contract).

    ``source_image_digest`` names the resolved catalog base source: ``virt-builder:<template>`` or
    ``cloud-image:<url>@sha256:<digest>`` (the latter a verified pin). The image's verifiable
    identity is the output qcow2 content digest
    (:func:`kdive.images.planes._build_common.digest_file`), per ADR-0092. ``guest_mac`` is the
    family's mandatory-access-control posture (``selinux-permissive`` for rhel, ``apparmor`` for
    debian), so the record stays falsifiable across families (#824). ``package_versions`` (the
    installed version of each requested package) is added only when capture succeeded — an empty
    map is omitted so a degraded build's row is byte-identical to a pre-feature one (ADR-0252).
    ``makedumpfile_version`` (the installed makedumpfile binary's version) is the per-image operand
    of the computed kdump-capability predicate; added only when captured, omitted otherwise so a
    degraded build's row stays byte-identical to a pre-feature one (ADR-0253).
    ``boot_kernel_count`` (the non-rescue ``vmlinuz-*`` count in ``/boot``) is the operand of the
    computed ``direct_kernel`` provisionability signal; added when the count is known — including
    ``0`` — and omitted only when the probe could not produce a listing (``None``), so a degraded
    build's row stays byte-identical to a pre-feature one (ADR-0295).
    ``os_release`` (the built image's ``ID``/``VERSION_ID``/``PRETTY_NAME`` from
    ``/etc/os-release``) is the verified OS identity surfaced by ``images.list``/``describe``; added
    only when captured, omitted otherwise so a degraded build's row stays byte-identical to a
    pre-feature one (ADR-0311).
    ``default_kernel_version`` (the lone non-rescue ``vmlinuz-<ver>`` version in ``/boot``) is the
    image's default kernel surfaced by ``images.list``/``describe`` for informed agent selection;
    added only when a single baseline kernel is present, omitted when zero/many (ambiguous) so a
    degraded build's row stays byte-identical to a pre-feature one (ADR-0317).
    """
    record: dict[str, object] = {
        "plane": "local-libvirt",
        "distro": spec.distro,
        "releasever": spec.releasever,
        "packages": list(spec.packages),
        "source_image_digest": _source_digest(entry.source),
        "capabilities": list(spec.capabilities),
        "arch": spec.arch,
        "image_size": size,
        "readiness_marker": _READINESS_MARKER,
        "layout": "whole-disk-ext4-qcow2",
        "guest_mac": family.guest_mac,
    }
    if package_versions:
        record["package_versions"] = package_versions
    if makedumpfile_version:
        record["makedumpfile_version"] = makedumpfile_version
    if boot_kernel_count is not None:
        record["boot_kernel_count"] = boot_kernel_count
    if default_kernel_version:
        record["default_kernel_version"] = default_kernel_version
    if os_release:
        record["os_release"] = os_release
    return record
