"""Local-libvirt Install + boot plane: stage a direct-kernel boot, bring the System up (ADR-0030).

`LocalLibvirtInstall` realizes two handler-facing ports keyed on the System-tagged libvirt
domain (`kdive-{system_id}`, minted by the provisioning plane, ADR-0025):

- `install(request)` stages the kernel
  (and optionally an initrd) to a **per-Run** host-local path
  (`{staging_root}/{system_id}/{run_id}/{kernel[,initrd]}`) via a temp-then-rename fetch.
  The kdump capture prerequisite check fires only for `method=CaptureMethod.KDUMP`; non-kdump
  boots skip it. When `initrd_ref` is ``None`` (e.g. a bzImage with embedded initramfs) no
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
from kdive.config.core_settings import INSTALL_STAGING
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.boot.guest_kernel_writer import (
    GuestKernelWriter,
    _RealGuestKernelWriter,
)
from kdive.providers.local_libvirt.lifecycle.boot.kernel_bundle import (
    extract_boot_vmlinuz,
    repack_modules_subtree,
)
from kdive.providers.local_libvirt.lifecycle.boot.readiness import (
    ReadinessResult,
    _real_readiness,
)
from kdive.providers.local_libvirt.lifecycle.boot.staged_write import write_staged_bytes
from kdive.providers.local_libvirt.lifecycle.storage import overlay_path
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.ports.lifecycle import InstallRequest
from kdive.providers.shared.libvirt_xml import register_kdive_namespace, register_qemu_namespace
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)

_DEFAULT_BOOT_WINDOW_POLLS = 60


# The boot window is _DEFAULT_BOOT_WINDOW_POLLS × _POLL_INTERVAL_SECONDS = 300s (ADR-0055 §7):
# boot()._await_ready loops the poll count; _real_readiness owns the per-poll cadence. The window
# accommodates the kdive-ready signal now ordering After=kdump.service (#817): a crash-capture
# guest does not report ready until kdump.service has built the capture initramfs and kexec-loaded
# it, which adds tens of seconds on the first dracut build. It is a timeout, not a fixed wait —
# _await_ready returns the instant the marker appears, so the wider ceiling costs nothing on a fast
# boot and the _CRASH_SIGNATURE fail-fast still surfaces a panicked boot immediately.
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


class LocalLibvirtInstall:
    """The realized `Installer` + `Booter` for the local libvirt host (ADR-0030)."""

    def __init__(
        self,
        *,
        connect: Connect,
        fetch_kernel: Fetch,
        fetch_initrd: Fetch,
        readiness: Readiness,
        staging_root: Path,
        boot_window_polls: int = _DEFAULT_BOOT_WINDOW_POLLS,
        fetch_modules: Fetch | None = None,
        kernel_writer: GuestKernelWriter | None = None,
    ) -> None:
        self._connect = connect
        self._fetch_kernel = fetch_kernel
        self._fetch_initrd = fetch_initrd
        self._readiness = readiness
        self._staging_root = staging_root
        self._boot_window_polls = boot_window_polls
        self._fetch_modules = fetch_modules or fetch_kernel
        self._kernel_writer = kernel_writer

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
        return cls(
            connect=lambda: libvirt.open(host_uri),
            fetch_kernel=_real_fetch,
            fetch_initrd=_real_fetch,
            readiness=_real_readiness,
            staging_root=staging_root,
            fetch_modules=_real_fetch,
            kernel_writer=_RealGuestKernelWriter(),
        )

    def install(self, request: InstallRequest) -> None:
        """Stage the kernel (and optionally initrd) and redefine the domain for direct-kernel boot.

        ``kernel_ref`` is the combined kernel+modules tar (the unified artifact, ADR-0234 §2):
        install fetches it, extracts ``boot/vmlinuz`` host-side to ``staging/kernel`` for the
        direct-kernel ``<kernel>`` element, and — when the boot is kdump or carries debuginfo —
        repacks the tar's ``lib/modules/`` subtree and feeds it to the libguestfs injector. The
        initrd fetch and ``<initrd>`` element are omitted when ``initrd_ref`` is ``None`` (e.g. a
        bzImage with an embedded initramfs). The kdump preflight is gated on
        ``method == CaptureMethod.KDUMP`` — non-kdump boots do not require kdump prerequisites.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the kdump capture path is absent
                (method=kdump only, checked before any redefine) or the configured staging
                root is not writable by the run user (a ``PermissionError`` on the per-Run
                ``mkdir``, naming ``KDIVE_INSTALL_STAGING`` + the path + a remedy, ADR-0204);
                ``INSTALL_FAILURE`` on a libvirt redefine error; ``INFRASTRUCTURE_FAILURE`` on
                any other staging-dir creation fault; any fetch error category from the seam.
        """
        staging_dir = self._staging_root / str(request.system_id) / str(request.run_id)
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise self._unwritable_staging_error(staging_dir) from exc
        except OSError as exc:
            raise CategorizedError(
                "failed to create the per-Run staging directory",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"op": "mkdir", "dest": str(staging_dir)},
            ) from exc
        artifacts = self._stage_install_artifacts(request, staging_dir)
        kdump_env_absent = request.method is CaptureMethod.KDUMP and not (
            artifacts.modules_injected or artifacts.initrd_path is not None
        )
        if kdump_env_absent:
            raise CategorizedError(
                "kdump capture environment absent (need injected modules or a staged initrd)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(request.system_id)},
            )
        domain_name = domain_name_for(request.system_id)
        conn = self._open("for install")
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
                raise self._install_failure("redefining", domain_name) from exc
        finally:
            _close(conn)

    def _stage_install_artifacts(
        self, request: InstallRequest, staging_dir: Path
    ) -> _StagedInstallArtifacts:
        combined_tar = staging_dir / "kernel.tar.gz"
        self._fetch_kernel(request.kernel_ref, combined_tar)
        kernel_path = staging_dir / "kernel"
        extract_boot_vmlinuz(combined_tar, kernel_path)
        initrd_path = self._stage_initrd(request, staging_dir)
        modules_tar = staging_dir / "modules.tar.gz"
        modules_injected = self._inject_modules_if_needed(
            request, staging_dir, combined_tar, modules_tar, kernel_path
        )
        self._delete_install_intermediates(combined_tar, modules_tar)
        return _StagedInstallArtifacts(kernel_path, initrd_path, modules_injected)

    def _stage_initrd(self, request: InstallRequest, staging_dir: Path) -> Path | None:
        if request.initrd_ref is None:
            return None
        initrd_path = staging_dir / "initrd"
        self._fetch_initrd(request.initrd_ref, initrd_path)
        return initrd_path

    def _inject_modules_if_needed(
        self,
        request: InstallRequest,
        staging_dir: Path,
        combined_tar: Path,
        modules_tar: Path,
        kernel_path: Path,
    ) -> bool:
        needs_modules = request.method is CaptureMethod.KDUMP or request.debuginfo_ref is not None
        if not needs_modules or not repack_modules_subtree(combined_tar, modules_tar):
            return False
        self._inject_built_modules(
            request.system_id, modules_tar, kernel_path, request.debuginfo_ref, staging_dir
        )
        return True

    @staticmethod
    def _delete_install_intermediates(combined_tar: Path, modules_tar: Path) -> None:
        # The combined tar and the repacked modules tar are intermediates: boot/vmlinuz is already
        # extracted for the <kernel> element and the modules tree is injected in-guest, so neither
        # is needed past this point. Reclaim them best-effort so the per-Run staging dir does not
        # retain a redundant copy of the kernel bytes for the System's lifetime; a retried install
        # re-fetches the combined tar (temp-then-rename), so removal is retry-safe.
        for intermediate in (combined_tar, modules_tar):
            with contextlib.suppress(OSError):
                intermediate.unlink(missing_ok=True)

    def _inject_built_modules(
        self,
        system_id: UUID,
        modules_tar: Path,
        kernel_image: Path,
        debuginfo_ref: str | None,
        staging_dir: Path,
    ) -> None:
        """Force-off the domain, then stage the built kernel into its overlay (ADR-0203/0207).

        Injects ``/lib/modules/<ver>`` *and* the from-source kernel image at
        ``/boot/vmlinuz-<ver>`` so the guest's ``kdumpctl`` has a crash kernel to kexec-load —
        under direct-kernel boot the running kernel is supplied by libvirt and is otherwise absent
        from the guest ``/boot`` (ADR-0207). ``modules_tar`` is the ``lib/modules/`` subtree
        repacked host-side from the combined kernel tar; ``kernel_image`` is the ``boot/vmlinuz``
        ``install`` already extracted to ``staging_dir/kernel`` for the ``<kernel>`` element.

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
        self._force_off_if_active(system_id)
        vmlinux: Path | None = None
        if debuginfo_ref is not None:
            vmlinux = staging_dir / "vmlinux"
            self._fetch_modules(debuginfo_ref, vmlinux)
        self._kernel_writer.inject(overlay_path(system_id), kernel_image, modules_tar, vmlinux)

    def _force_off_if_active(self, system_id: UUID) -> None:
        """Destroy the System's domain if it is running (idempotent), mirroring ``_power_cycle``.

        A rw libguestfs mount of a live overlay corrupts it, so the domain must be off before
        the module writer opens the overlay read-write (ADR-0203). An absent domain is the
        achieved post-state (nothing running to quiesce).
        """
        domain_name = domain_name_for(system_id)
        conn = self._open("to force-off before module injection")
        try:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError:
                return  # already gone — nothing running to quiesce
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

    def boot(self, system_id: UUID) -> None:
        """Power-cycle the domain into the staged kernel and confirm run-readiness.

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` if the domain is absent or libvirt cannot
                start it; ``BOOT_TIMEOUT`` if the System never answers within the boot window;
                ``READINESS_FAILURE`` if it answers but a readiness check fails.
        """
        domain_name = domain_name_for(system_id)
        conn = self._open("to boot")
        try:
            domain = self._lookup(conn, domain_name)
            self._power_cycle(domain, domain_name)
        finally:
            _close(conn)
        self._await_ready(system_id)

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
            domain = conn.lookupByName(domain_name)
            current = domain.XMLDesc(0)
        except libvirt.libvirtError as exc:
            raise self._install_failure("looking up", domain_name) from exc
        # `XMLDesc` crosses the same libvirtd trust boundary the discovery plane parses
        # with defusedxml: parse it the same way so a DOCTYPE/entity-expansion document
        # cannot become a billion-laughs DoS here. A malformed/forbidden document is a
        # clean install_failure, not a raw parser exception out of the handler.
        try:
            root = _safe_fromstring(current)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise self._install_failure("parsing the domain XML of", domain_name) from exc
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

    def _power_cycle(self, domain: _LibvirtDomain, domain_name: str) -> None:
        try:
            if domain.isActive():
                domain.destroy()
            domain.create()
        except libvirt.libvirtError as exc:
            raise self._install_failure("power-cycling", domain_name) from exc

    def _await_ready(self, system_id: UUID) -> None:
        first_probe_error: str | None = None
        for _ in range(self._boot_window_polls):
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

    def _open(self, purpose: str) -> _LibvirtConn:
        try:
            return self._connect()
        except libvirt.libvirtError as exc:
            raise self._install_failure(f"connecting to libvirt {purpose}", "install") from exc

    @staticmethod
    def _lookup(conn: _LibvirtConn, domain_name: str) -> _LibvirtDomain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise LocalLibvirtInstall._install_failure("looking up", domain_name) from exc

    @staticmethod
    def _install_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.INSTALL_FAILURE,
            details={"domain": domain_name},
        )

    def _unwritable_staging_error(self, staging_dir: Path) -> CategorizedError:
        """A ``PermissionError`` on the per-Run mkdir is operator misconfiguration (ADR-0204).

        The configured staging root is not writable by the run user (the default's parent
        ``/var/lib/kdive`` is root-owned). That never becomes writable on retry, so it is a
        ``CONFIGURATION_ERROR`` (not a retry-able infrastructure failure) whose details name
        the env var, the configured root, the per-Run path tried, and an actionable remedy.
        """
        staging_root = str(self._staging_root)
        remedy = (
            f"pre-create {staging_root} (or repoint {INSTALL_STAGING.name}) so it is writable "
            "by the run user; on SELinux hosts give it the virt_image_t label"
        )
        return CategorizedError(
            f"install staging root {staging_root} is not writable by the run user",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "op": "mkdir",
                "env_var": INSTALL_STAGING.name,
                "staging_root": staging_root,
                "dest": str(staging_dir),
                "remedy": remedy,
            },
        )


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
    "extract_boot_vmlinuz",
    "repack_modules_subtree",
]
