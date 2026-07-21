"""Local-libvirt Install + boot plane: stage a direct-kernel boot, bring the System up (ADR-0030).

`LocalLibvirtInstall` realizes two handler-facing ports keyed on the System-tagged libvirt
domain (`kdive-{system_id}`, minted by the provisioning plane, ADR-0025):

- `install(request)` stages the kernel
  (and optionally an initrd) to a **per-Run** host-local path
  (`{staging_root}/{system_id}/{run_id}/{kernel[,initrd]}`) via a temp-then-rename fetch.
  The kdump capture prerequisite check fires for the kdump family (`KDUMP`/`FADUMP`, ADR-0349);
  non-capture boots skip it. When `initrd_ref` is ``None`` (e.g. an embedded-initramfs kernel) no
  initrd is fetched and no `<initrd>` element is emitted. `defineXML`s the domain with a
  direct-kernel `<os>` (`<kernel>`/[`<initrd>`]/`<cmdline>`). The `<os>` is built with
  `xml.etree.ElementTree` (no string interpolation), so a `cmdline` value cannot inject XML.
- `boot(system_id)` power-cycles the domain into the staged `<kernel>` (`destroy` if running,
  then `create`) and polls the run-readiness preflight within a bounded window: the System
  never answering is `boot_timeout`; answering-but-failing a check is `readiness_failure`; a
  libvirt error starting the domain is `install_failure`.

DB-free: it owns no Postgres — the `runs.*` install/boot handlers drive the step ledger.
The slow, host-bound seams (libvirt connect, object-store fetch, kdump/readiness checks, the
poll clock) are **injected**, so unit tests cover the orchestration/error contract without a
host; the real `libvirt.open`/object-store path is `live_vm`-only.
"""

from __future__ import annotations

import contextlib
import logging
import math
import xml.etree.ElementTree as ET  # noqa: S405 - constructs/edits self-owned domain XML only
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

import kdive.config as config
from kdive.artifacts.storage import FetchedArtifact
from kdive.config.core_settings import INSTALL_SCRATCH, INSTALL_STAGING
from kdive.config.registry import Setting
from kdive.domain.capture import KDUMP_FAMILY
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.boot.guest_kernel_writer import (
    GuestKernelWriter,
    _RealGuestKernelWriter,
)
from kdive.providers.local_libvirt.lifecycle.boot.kernel_bundle import extract_kernel_bundle
from kdive.providers.local_libvirt.lifecycle.boot.readiness import (
    _POLL_INTERVAL_SECONDS,
    ReadinessResult,
    _real_readiness,
)
from kdive.providers.local_libvirt.lifecycle.boot.staged_write import write_staged_bytes
from kdive.providers.local_libvirt.lifecycle.deadlines import tcg_deadline_multiplier
from kdive.providers.local_libvirt.lifecycle.storage import overlay_path
from kdive.providers.local_libvirt.settings import LIBVIRT_BOOT_WINDOW_S, LIBVIRT_URI
from kdive.providers.ports.lifecycle import InstallRequest
from kdive.providers.shared.libvirt_xml import register_kdive_namespace, register_qemu_namespace
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)


# The boot window is derived from KDIVE_LIBVIRT_BOOT_WINDOW_S (default 900 s) divided by the
# _POLL_INTERVAL_SECONDS cadence (5 s) — 180 polls at the default.  boot()._await_ready loops
# the poll count; _real_readiness owns the per-poll cadence.  The window accommodates the
# kdive-ready signal ordering After=kdump.service (#817): a crash-capture guest does not report
# ready until kdump.service has built the capture initramfs and kexec-loaded it, which on POWER9
# takes several minutes on the first dracut run.  It is a ceiling, not a fixed wait —
# _await_ready returns the instant the marker appears, so the wider window costs nothing on a
# fast boot and the _CRASH_SIGNATURE fail-fast still surfaces a panicked boot immediately.
# Operators on very fast hosts can tighten it; operators on slow hosts (POWER, large kdump
# initramfs) can widen it — all without rebuilding the image.
def _boot_window_polls() -> int:
    """Return the number of readiness polls for the configured boot window."""
    return math.ceil(config.require(LIBVIRT_BOOT_WINDOW_S) / _POLL_INTERVAL_SECONDS)


class _LibvirtDomain(Protocol):
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802 - mirrors the libvirt binding name
    def isActive(self) -> int: ...  # noqa: N802 - mirrors the libvirt binding name
    def create(self) -> int: ...
    def destroy(self) -> int: ...


class _LibvirtConn(Protocol):
    def lookupByName(self, name: str) -> _LibvirtDomain: ...  # noqa: N802 - libvirt name
    def defineXML(self, xml: str) -> _LibvirtDomain: ...  # noqa: N802 - libvirt name
    def close(self) -> int: ...


type Connect = Callable[[], _LibvirtConn]
type Fetch = Callable[[str, Path], None]
type Readiness = Callable[[UUID], ReadinessResult]


@dataclass(frozen=True, slots=True)
class _StagedInstallArtifacts:
    kernel_path: Path
    initrd_path: Path | None
    modules_injected: bool


def _close(conn: _LibvirtConn) -> None:
    """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


def _install_failure(verb: str, domain_name: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb} domain",
        category=ErrorCategory.INSTALL_FAILURE,
        details={"domain": domain_name},
    )


def _open(connect: Connect, purpose: str) -> _LibvirtConn:
    try:
        return connect()
    except libvirt.libvirtError as exc:
        raise _install_failure(f"connecting to libvirt {purpose}", "install") from exc


def _lookup(conn: _LibvirtConn, domain_name: str) -> _LibvirtDomain:
    try:
        return conn.lookupByName(domain_name)
    except libvirt.libvirtError as exc:
        raise _install_failure("looking up", domain_name) from exc


class LocalLibvirtBooter:
    """Power-cycle a local-libvirt domain and wait for the run-readiness signal."""

    def __init__(
        self,
        *,
        connect: Connect,
        readiness: Readiness,
        boot_window_polls: int,
    ) -> None:
        self._connect = connect
        self._readiness = readiness
        self._boot_window_polls = boot_window_polls

    def boot(self, system_id: UUID, *, accel: str | None = None) -> None:
        """Power-cycle the domain into the staged kernel and confirm run-readiness.

        ``accel`` is the System's persisted accelerator (ADR-0339). The boot-readiness window
        is scaled by ``tcg_deadline_multiplier(accel)`` (ADR-0341): a KVM guest keeps the base
        window, while a TCG or unknown/``None`` accelerator gets the generous scaled window so
        a slow emulated boot is not timed out spuriously. The window is a ceiling, not a fixed
        wait — a fast boot still returns the instant the readiness marker appears.

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` if the domain is absent or libvirt cannot
                start it; ``BOOT_TIMEOUT`` if the System never answers within the boot window;
                ``READINESS_FAILURE`` if it answers but a readiness check fails.
        """
        domain_name = domain_name_for(system_id)
        conn = _open(self._connect, "to boot")
        try:
            domain = _lookup(conn, domain_name)
            self._power_cycle(domain, domain_name)
        finally:
            _close(conn)
        polls = math.ceil(self._boot_window_polls * tcg_deadline_multiplier(accel))
        self._await_ready(system_id, polls)

    def force_off_if_active(self, system_id: UUID) -> None:
        """Destroy the System's domain if it is running before a rw overlay mount."""
        domain_name = domain_name_for(system_id)
        conn = _open(self._connect, "to force-off before module injection")
        try:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError:
                return
            try:
                if domain.isActive():
                    domain.destroy()
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    "failed to force-off the System domain before module injection",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"domain": domain_name},
                ) from exc
        finally:
            _close(conn)

    @staticmethod
    def _power_cycle(domain: _LibvirtDomain, domain_name: str) -> None:
        try:
            if domain.isActive():
                domain.destroy()
            domain.create()
        except libvirt.libvirtError as exc:
            raise _install_failure("power-cycling", domain_name) from exc

    def _await_ready(self, system_id: UUID, polls: int) -> None:
        first_probe_error: str | None = None
        for _ in range(polls):
            result = self._readiness(system_id)
            if first_probe_error is None and result.probe_error is not None:
                first_probe_error = result.probe_error
            if result.answered:
                if result.ok:
                    return
                raise CategorizedError(
                    "System booted but a run-readiness check failed",
                    category=ErrorCategory.READINESS_FAILURE,
                    details=self._boot_failure_details(system_id, first_probe_error),
                )
        raise CategorizedError(
            "System did not become ready within the boot window",
            category=ErrorCategory.BOOT_TIMEOUT,
            details=self._boot_failure_details(system_id, first_probe_error),
        )

    @staticmethod
    def _boot_failure_details(system_id: UUID, first_probe_error: str | None) -> dict[str, object]:
        details: dict[str, object] = {"system_id": str(system_id)}
        if first_probe_error is not None:
            details["probe_error"] = first_probe_error
        return details


class LocalLibvirtInstaller:
    """Stage direct-kernel artifacts and redefine the local-libvirt domain XML."""

    def __init__(
        self,
        *,
        connect: Connect,
        fetch_kernel: Fetch,
        fetch_initrd: Fetch,
        staging_root: Path,
        booter: LocalLibvirtBooter,
        scratch_root: Path | None = None,
        fetch_modules: Fetch | None = None,
        kernel_writer: GuestKernelWriter | None = None,
    ) -> None:
        self._connect = connect
        self._fetch_kernel = fetch_kernel
        self._fetch_initrd = fetch_initrd
        self._staging_root = staging_root
        # Scratch holds the large, short-lived install intermediates; unset it tracks the staging
        # root so behavior is unchanged and no second directory is created (ADR-0399).
        self._scratch_root = scratch_root if scratch_root is not None else staging_root
        self._booter = booter
        self._fetch_modules = fetch_modules or fetch_kernel
        self._kernel_writer = kernel_writer

    def install(self, request: InstallRequest) -> None:
        """Stage the kernel (and optionally initrd) and redefine the domain for direct-kernel boot.

        ``kernel_ref`` is the combined kernel+modules tar (the unified artifact, ADR-0234 §2):
        install fetches it, extracts ``boot/vmlinuz`` host-side to ``staging/kernel`` for the
        direct-kernel ``<kernel>`` element, and — when the boot is kdump or carries debuginfo —
        repacks the tar's ``lib/modules/`` subtree and feeds it to the libguestfs injector. The
        initrd fetch and ``<initrd>`` element are omitted when ``initrd_ref`` is ``None`` (e.g. an
        embedded-initramfs kernel). The kdump preflight is gated on
        the kdump family (``KDUMP``/``FADUMP``) — non-capture boots need no kdump prerequisites.

        Intermediates (the combined tar, repacked modules tar, and a debuginfo run's vmlinux)
        stage under ``KDIVE_INSTALL_SCRATCH`` when set, else under the staging root (ADR-0399);
        the persistent ``kernel``/``initrd`` always stage under ``KDIVE_INSTALL_STAGING``.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the kdump capture path is absent
                (method=kdump only, checked before any redefine) or the configured staging or
                scratch root is not writable by the run user (a ``PermissionError`` on the per-Run
                ``mkdir``, naming ``KDIVE_INSTALL_STAGING``/``KDIVE_INSTALL_SCRATCH`` + the path +
                a remedy, ADR-0204); ``INSTALL_FAILURE`` on a libvirt redefine error;
                ``INFRASTRUCTURE_FAILURE`` on any other run-dir creation fault; any fetch error
                category from the seam.
        """
        staging_dir = self._make_run_dir(self._staging_root, request, INSTALL_STAGING)
        if self._scratch_root == self._staging_root:
            scratch_dir = staging_dir
        else:
            scratch_dir = self._make_run_dir(self._scratch_root, request, INSTALL_SCRATCH)
        artifacts = self._stage_install_artifacts(request, staging_dir, scratch_dir)
        kdump_env_absent = request.method in KDUMP_FAMILY and not (
            artifacts.modules_injected or artifacts.initrd_path is not None
        )
        if kdump_env_absent:
            raise CategorizedError(
                "kdump capture environment absent (need injected modules or a staged initrd)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(request.system_id)},
            )
        domain_name = domain_name_for(request.system_id)
        conn = _open(self._connect, "for install")
        try:
            xml = self._render_direct_kernel_xml(
                conn,
                domain_name,
                artifacts.kernel_path,
                artifacts.initrd_path,
                request.cmdline,
            )
            try:
                conn.defineXML(xml)
            except libvirt.libvirtError as exc:
                raise _install_failure("redefining", domain_name) from exc
        finally:
            _close(conn)

    def _make_run_dir(self, root: Path, request: InstallRequest, setting: Setting[str]) -> Path:
        """Create the per-Run directory under ``root``, mapping a mkdir fault to a clean error.

        A ``PermissionError`` on an operator-misconfigured root is a ``CONFIGURATION_ERROR`` naming
        ``setting`` (ADR-0204); any other ``OSError`` is an ``INFRASTRUCTURE_FAILURE``.
        """
        run_dir = root / str(request.system_id) / str(request.run_id)
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise self._unwritable_run_dir_error(root, run_dir, setting) from exc
        except OSError as exc:
            raise CategorizedError(
                "failed to create the per-Run staging directory",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"op": "mkdir", "dest": str(run_dir)},
            ) from exc
        return run_dir

    def _stage_install_artifacts(
        self, request: InstallRequest, staging_dir: Path, scratch_dir: Path
    ) -> _StagedInstallArtifacts:
        combined_tar = scratch_dir / "kernel.tar.gz"
        modules_tar = scratch_dir / "modules.tar.gz"
        vmlinux = scratch_dir / "vmlinux"
        self._fetch_kernel(request.kernel_ref, combined_tar)
        kernel_path = staging_dir / "kernel"
        # kdump and debuginfo installs need the module tree in the guest; a plain boot does not.
        # A modules-needed run repacks lib/modules/ in the same pass extract_kernel_bundle makes
        # over boot/vmlinuz, so the combined tar is decompressed once, not twice (ADR-0399).
        needs_modules = request.method in KDUMP_FAMILY or request.debuginfo_ref is not None
        modules_found = extract_kernel_bundle(
            combined_tar, kernel_path, modules_tar if needs_modules else None
        )
        initrd_path = self._stage_initrd(request, staging_dir)
        modules_injected = False
        if modules_found:
            self._inject_built_modules(
                request.system_id, modules_tar, kernel_path, request.debuginfo_ref, vmlinux
            )
            modules_injected = True
        self._delete_install_intermediates(combined_tar, modules_tar, vmlinux)
        return _StagedInstallArtifacts(kernel_path, initrd_path, modules_injected)

    def _stage_initrd(self, request: InstallRequest, staging_dir: Path) -> Path | None:
        if request.initrd_ref is None:
            return None
        initrd_path = staging_dir / "initrd"
        self._fetch_initrd(request.initrd_ref, initrd_path)
        return initrd_path

    @staticmethod
    def _delete_install_intermediates(combined_tar: Path, modules_tar: Path, vmlinux: Path) -> None:
        # The combined tar, the repacked modules tar, and a debuginfo run's vmlinux are
        # intermediates: boot/vmlinuz is already extracted for the <kernel> element and the modules
        # tree and vmlinux are injected in-guest, so none is needed past this point. Reclaim them
        # best-effort so the per-Run scratch dir does not retain a redundant copy of the kernel
        # bytes for the System's lifetime — which on a tmpfs scratch is leaked RAM. A retried
        # install re-fetches (temp-then-rename), so removal is retry-safe.
        for intermediate in (combined_tar, modules_tar, vmlinux):
            with contextlib.suppress(OSError):
                intermediate.unlink(missing_ok=True)

    def _inject_built_modules(
        self,
        system_id: UUID,
        modules_tar: Path,
        kernel_image: Path,
        debuginfo_ref: str | None,
        vmlinux: Path,
    ) -> None:
        """Force-off the domain, then stage the built kernel into its overlay (ADR-0203/0207).

        Injects ``/lib/modules/<ver>`` *and* the from-source kernel image at
        ``/boot/vmlinuz-<ver>`` so the guest's ``kdumpctl`` has a crash kernel to kexec-load —
        under direct-kernel boot the running kernel is supplied by libvirt and is otherwise absent
        from the guest ``/boot`` (ADR-0207). ``modules_tar`` is the ``lib/modules/`` subtree
        repacked host-side from the combined kernel tar; ``kernel_image`` is the ``boot/vmlinuz``
        ``install`` already extracted to ``staging_dir/kernel`` for the ``<kernel>`` element;
        ``vmlinux`` is the scratch path a debuginfo run's DWARF image is fetched to before inject.

        When ``debuginfo_ref`` is set the run's DWARF ``vmlinux`` is fetched and staged in-guest
        at ``/usr/lib/debug/lib/modules/<ver>/vmlinux`` so the live ``kdive-drgn`` helper's
        ``drgn -k`` resolves typed symbols against ``/proc/kcore`` (ADR-0221); it rides this same
        rw session (the modules tarball is the ``<ver>`` source either way).

        Ordered force-off → fetch → inject: a rw libguestfs mount of a live qcow2 corrupts it,
        and ``runs.install`` can target an already-booted System (ADR-0026 §7 recovery), so the
        domain is destroyed (idempotent) before the writer touches the overlay. Injection itself
        is idempotent (clobber + re-extract; the kernel upload truncates/creates), so a retried
        install self-heals a partial write.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` if libguestfs is absent or no writer is
                configured; ``INFRASTRUCTURE_FAILURE`` on a force-off or libguestfs fault; any
                fetch error category from the debuginfo-fetch seam.
        """
        if self._kernel_writer is None:
            raise CategorizedError(
                "kernel staging requested but no GuestKernelWriter is configured",
                category=ErrorCategory.MISSING_DEPENDENCY,
                details={"system_id": str(system_id)},
            )
        self._booter.force_off_if_active(system_id)
        vmlinux_ref: Path | None = None
        if debuginfo_ref is not None:
            vmlinux_ref = vmlinux
            self._fetch_modules(debuginfo_ref, vmlinux_ref)
        self._kernel_writer.inject(overlay_path(system_id), kernel_image, modules_tar, vmlinux_ref)

    def _render_direct_kernel_xml(
        self,
        conn: _LibvirtConn,
        domain_name: str,
        kernel_path: Path,
        initrd_path: Path | None,
        cmdline: str,
    ) -> str:
        """Read the existing domain XML and add a direct-kernel `<os>` section (ADR-0030 §5).

        ``initrd_path`` is optional: when ``None`` (embedded-initramfs kernel) no ``<initrd>``
        element is emitted, so libvirt boots the kernel without a separate initrd.

        Registers the kdive + qemu prefixes before re-serializing: ``register_*`` mutates
        process-global ElementTree state, so a domain provisioned with a gdbstub (the
        ``<qemu:commandline>`` passthrough, ADR-0210) would otherwise round-trip to an
        auto-assigned ``ns0:`` prefix that libvirt rejects — silently dropping the gdbstub.
        """
        register_kdive_namespace()
        register_qemu_namespace()
        try:
            domain = _lookup(conn, domain_name)
            current = domain.XMLDesc(0)
        except libvirt.libvirtError as exc:
            raise _install_failure("looking up", domain_name) from exc
        # `XMLDesc` crosses the same libvirtd trust boundary the discovery plane parses
        # with defusedxml: parse it the same way so a DOCTYPE/entity-expansion document
        # cannot become a billion-laughs DoS here. A malformed/forbidden document is a
        # clean install_failure, not a raw parser exception out of the handler.
        try:
            root = _safe_fromstring(current)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise _install_failure("parsing the domain XML of", domain_name) from exc
        os_el = root.find("os")
        if os_el is None:
            os_el = ET.SubElement(root, "os")
        for tag in ("kernel", "initrd", "cmdline"):
            existing = os_el.find(tag)
            if existing is not None:
                os_el.remove(existing)
        ET.SubElement(os_el, "kernel").text = str(kernel_path)
        if initrd_path is not None:
            ET.SubElement(os_el, "initrd").text = str(initrd_path)
        ET.SubElement(os_el, "cmdline").text = cmdline
        return ET.tostring(root, encoding="unicode")

    @staticmethod
    def _unwritable_run_dir_error(
        root: Path, run_dir: Path, setting: Setting[str]
    ) -> CategorizedError:
        """A ``PermissionError`` on the per-Run mkdir is operator misconfiguration (ADR-0204).

        The configured ``root`` (staging or scratch) is not writable by the run user (the staging
        default's parent ``/var/lib/kdive`` is root-owned). That never becomes writable on retry,
        so it is a ``CONFIGURATION_ERROR`` (not a retry-able infrastructure failure) whose details
        name the env var, the configured root, the per-Run path tried, and an actionable remedy.
        """
        root_str = str(root)
        remedy = (
            f"pre-create {root_str} (or repoint {setting.name}) so it is writable "
            "by the run user; on SELinux hosts give it the virt_image_t label"
        )
        return CategorizedError(
            f"install {setting.name} root {root_str} is not writable by the run user",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "op": "mkdir",
                "env_var": setting.name,
                "root": root_str,
                "dest": str(run_dir),
                "remedy": remedy,
            },
        )


class LocalLibvirtInstall:
    """The local-libvirt lifecycle facade implementing the Installer and Booter ports."""

    def __init__(
        self,
        *,
        connect: Connect,
        fetch_kernel: Fetch,
        fetch_initrd: Fetch,
        readiness: Readiness,
        staging_root: Path,
        boot_window_polls: int,
        scratch_root: Path | None = None,
        fetch_modules: Fetch | None = None,
        kernel_writer: GuestKernelWriter | None = None,
    ) -> None:
        booter = LocalLibvirtBooter(
            connect=connect,
            readiness=readiness,
            boot_window_polls=boot_window_polls,
        )
        self._installer = LocalLibvirtInstaller(
            connect=connect,
            fetch_kernel=fetch_kernel,
            fetch_initrd=fetch_initrd,
            staging_root=staging_root,
            booter=booter,
            scratch_root=scratch_root,
            fetch_modules=fetch_modules,
            kernel_writer=kernel_writer,
        )
        self._booter = booter

    @classmethod
    def from_env(cls) -> LocalLibvirtInstall:
        """Build from the ``KDIVE_*`` environment; does not connect to libvirt or the store.

        The fetch seam is the real object-store read (`_real_fetch` → `_stage_object`,
        ADR-0054): it builds the store lazily from the ``KDIVE_S3_*`` env on the first call,
        so the worker registers its handlers without S3 env present, and the network I/O runs
        only when an install fetches. The real readiness preflight (`_real_readiness`) tails the
        teed console under the `live_vm` gate (it needs a running host); the kdump prerequisite
        is a host-observable initrd-presence check inside ``install`` (ADR-0055 §5), not a seam.
        """
        host_uri = config.require(LIBVIRT_URI)
        staging_root = Path(config.require(INSTALL_STAGING))
        scratch_raw = config.get(INSTALL_SCRATCH)
        scratch_root = Path(scratch_raw) if scratch_raw else None
        return cls(
            connect=lambda: libvirt.open(host_uri),
            fetch_kernel=_real_fetch,
            fetch_initrd=_real_fetch,
            readiness=_real_readiness,
            staging_root=staging_root,
            boot_window_polls=_boot_window_polls(),
            scratch_root=scratch_root,
            fetch_modules=_real_fetch,
            kernel_writer=_RealGuestKernelWriter(),
        )

    def install(self, request: InstallRequest) -> None:
        self._installer.install(request)

    def boot(self, system_id: UUID, *, accel: str | None = None) -> None:
        self._booter.boot(system_id, accel=accel)


class _ObjectReader(Protocol):
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...


def _stage_object(store: _ObjectReader, ref: str, dest: Path) -> None:
    """Read object ``ref`` from the store and write it to ``dest`` via temp-then-rename.

    ``ref`` is a key the system itself produced (the Run's ``kernel_ref``/``initrd_ref``),
    so the read is **unconditional** (``etag=None``, ADR-0054) — the install plane holds no
    client handle to validate. The bytes are written to a sibling ``.part`` file and
    atomically renamed into ``dest``, so a failure partway leaves ``dest`` untouched and no
    partial file the redefine could point at.

    Raises:
        CategorizedError: a store fault — ``STALE_HANDLE`` for a vanished key,
            ``INFRASTRUCTURE_FAILURE`` otherwise (from ``get_artifact``); or a local
            staging-write fault (disk full, permission), mapped to
            ``INFRASTRUCTURE_FAILURE`` with the destination path so the failure is not an
            opaque ``OSError`` out of the seam.
    """
    data = store.get_artifact(ref, None).data
    write_staged_bytes(dest, data)


def _real_fetch(ref: str, dest: Path) -> None:  # pragma: no cover - live_vm
    _stage_object(object_store_from_env(), ref, dest)


__all__ = [
    "LocalLibvirtInstall",
    "ReadinessResult",
    "Fetch",
    "GuestKernelWriter",
    "_RealGuestKernelWriter",
    "_real_readiness",
    "_stage_object",
    "extract_kernel_bundle",
]
