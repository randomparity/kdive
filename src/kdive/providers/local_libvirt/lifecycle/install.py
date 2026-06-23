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
import re
import shutil
import subprocess  # noqa: S404 - virsh domstate is invoked with a fixed argv, no shell
import tarfile
import time
import xml.etree.ElementTree as ET  # noqa: S405 - constructs/edits self-owned domain XML only
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple, Protocol
from uuid import UUID

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

import kdive.config as config
from kdive.artifacts.storage import FetchedArtifact
from kdive.config.core_settings import INSTALL_STAGING
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.storage import overlay_path
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.ports import InstallRequest
from kdive.providers.shared.libvirt_xml import register_kdive_namespace, register_qemu_namespace
from kdive.providers.shared.runtime_paths import console_log_path, domain_name_for, read_console_log
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)

_DEFAULT_BOOT_WINDOW_POLLS = 30
# The boot window is _DEFAULT_BOOT_WINDOW_POLLS × _POLL_INTERVAL_SECONDS = 150s (ADR-0055 §7):
# boot()._await_ready loops the poll count; _real_readiness owns the per-poll cadence.
_POLL_INTERVAL_SECONDS = 5.0
_DOMSTATE_PROBE_TIMEOUT = 10
_TERMINAL_DOMSTATES = frozenset({"shut off", "crashed"})
_VIRSH = "virsh"

_READINESS_MARKER = "kdive-ready"
# Fatal/stall-grade kernel crash signatures (ADR-0055 §4). Fail-closed and additive.
# The lookbehinds keep `BUG:`/`Oops:` from matching benign substrings (e.g. `DEBUG:`).
_CRASH_SIGNATURE = re.compile(
    r"Kernel panic"
    r"|(?<![A-Za-z])BUG:"
    r"|(?<![A-Za-z])Oops:"
    r"|general protection fault"
    r"|[Uu]nable to handle kernel"
    r"|KASAN:"
    r"|KFENCE:"
    r"|detected stall"
)


class ConsoleVerdict(StrEnum):
    READY = "ready"
    CRASHED = "crashed"
    PENDING = "pending"


class ReadinessResult(NamedTuple):
    """The run-readiness preflight result: did the System answer, and did its checks pass."""

    answered: bool
    ok: bool
    probe_error: str | None = None


class _DomainExitProbe(NamedTuple):
    """The domstate probe result plus a bounded probe-failure diagnostic."""

    exited: bool
    error: str | None = None


class _LibvirtDomain(Protocol):
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802 - mirrors the libvirt binding name
    def isActive(self) -> int: ...  # noqa: N802 - mirrors the libvirt binding name
    def create(self) -> int: ...
    def destroy(self) -> int: ...


class _LibvirtConn(Protocol):
    def lookupByName(self, name: str) -> _LibvirtDomain: ...  # noqa: N802 - libvirt name
    def defineXML(self, xml: str) -> _LibvirtDomain: ...  # noqa: N802 - libvirt name
    def close(self) -> int: ...


class GuestKernelWriter(Protocol):
    """Stage a built kernel into a System overlay: ``/lib/modules/<ver>`` + ``/boot/vmlinuz-<ver>``.

    ``kernel_image`` is the from-source kernel already fetched for the direct-kernel ``<kernel>``
    element; the writer also lands it at ``/boot/vmlinuz-<ver>`` so the guest's ``kdumpctl`` can
    kexec-load a crash kernel (ADR-0207). ``modules_tar`` is the ``/lib/modules/<ver>`` tree.
    ``vmlinux``, when given, is the run's DWARF ``vmlinux`` staged at
    ``/usr/lib/debug/lib/modules/<ver>/vmlinux`` for in-guest live drgn (ADR-0221).
    """

    def inject(
        self, overlay: str, kernel_image: Path, modules_tar: Path, vmlinux: Path | None = None
    ) -> None: ...


type Connect = Callable[[], _LibvirtConn]
type Fetch = Callable[[str, Path], None]
type Readiness = Callable[[UUID], ReadinessResult]


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

        The initrd fetch and ``<initrd>`` element are omitted when ``initrd_ref`` is ``None``
        (e.g. a bzImage with an embedded initramfs). The kdump preflight is gated on
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
        kernel_path = staging_dir / "kernel"
        self._fetch_kernel(request.kernel_ref, kernel_path)
        initrd_path: Path | None = None
        if request.initrd_ref is not None:
            initrd_path = staging_dir / "initrd"
            self._fetch_initrd(request.initrd_ref, initrd_path)
        if request.modules_ref is not None and (
            request.method is CaptureMethod.KDUMP or request.debuginfo_ref is not None
        ):
            self._inject_built_modules(
                request.system_id, request.modules_ref, request.debuginfo_ref, staging_dir
            )
        if request.method is CaptureMethod.KDUMP and not (
            request.modules_ref is not None or initrd_path is not None
        ):
            raise CategorizedError(
                "kdump capture environment absent (need injected modules or a staged initrd)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(request.system_id)},
            )
        domain_name = domain_name_for(request.system_id)
        conn = self._open("for install")
        try:
            xml = self._render_direct_kernel_xml(
                conn, domain_name, kernel_path, initrd_path, request.cmdline
            )
            try:
                conn.defineXML(xml)
            except libvirt.libvirtError as exc:
                raise self._install_failure("redefining", domain_name) from exc
        finally:
            _close(conn)

    def _inject_built_modules(
        self, system_id: UUID, modules_ref: str, debuginfo_ref: str | None, staging_dir: Path
    ) -> None:
        """Force-off the domain, then stage the built kernel into its overlay (ADR-0203/0207).

        Injects ``/lib/modules/<ver>`` *and* the from-source kernel image at
        ``/boot/vmlinuz-<ver>`` so the guest's ``kdumpctl`` has a crash kernel to kexec-load —
        under direct-kernel boot the running kernel is supplied by libvirt and is otherwise absent
        from the guest ``/boot`` (ADR-0207). The kernel image is the one ``install`` already
        fetched to ``staging_dir/kernel`` for the ``<kernel>`` element; no extra fetch.

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
                fetch error category from the modules-fetch seam.
        """
        if self._kernel_writer is None:
            raise CategorizedError(
                "kernel staging requested but no GuestKernelWriter is configured",
                category=ErrorCategory.MISSING_DEPENDENCY,
                details={"system_id": str(system_id)},
            )
        self._force_off_if_active(system_id)
        modules_tar = staging_dir / "modules.tar.gz"
        self._fetch_modules(modules_ref, modules_tar)
        vmlinux: Path | None = None
        if debuginfo_ref is not None:
            vmlinux = staging_dir / "vmlinux"
            self._fetch_modules(debuginfo_ref, vmlinux)
        kernel_image = staging_dir / "kernel"
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


def classify_console(data: bytes, *, marker: str = _READINESS_MARKER) -> ConsoleVerdict:
    """Classify a console capture: did the System reach the marker, crash, or neither?

    The marker is matched as a whole line — the readiness unit echoes the bare line
    ``kdive-ready`` to the console, while systemd's ``Starting kdive-ready.service`` line
    (same substring) is not the signal (ADR-0055 §3). A crash signature (§4) in the
    pre-marker region wins (crash-wins, fail-closed). Bytes are decoded utf-8 with
    ``errors="replace"`` so a partial multibyte tail or non-UTF-8 console never raises.

    Returns:
        ``"crashed"`` if a crash signature precedes the marker (or the marker is absent),
        ``"ready"`` if a bare marker line is present with no crash before it, else
        ``"pending"``.
    """
    text = data.decode("utf-8", errors="replace")
    marker_re = re.compile(rf"^[^\S\n]*{re.escape(marker)}[^\S\n]*$", re.MULTILINE)
    marker_match = marker_re.search(text)
    region = text if marker_match is None else text[: marker_match.start()]
    if _CRASH_SIGNATURE.search(region):
        return ConsoleVerdict.CRASHED
    return ConsoleVerdict.READY if marker_match is not None else ConsoleVerdict.PENDING


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
    _write_staged_bytes(dest, data)


def _write_staged_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` through a sibling temp file, then atomically replace ``dest``."""
    tmp = dest.with_name(dest.name + ".part")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
        tmp.replace(dest)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()  # best-effort: drop any partial temp; never mask the real error
        raise CategorizedError(
            "failed to write the staged object to the per-Run path",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "stage", "dest": str(dest)},
        ) from exc


def _real_fetch(ref: str, dest: Path) -> None:  # pragma: no cover - live_vm
    _stage_object(object_store_from_env(), ref, dest)


_MODULES_ROOT = "/lib/modules"
_BOOT_ROOT = "/boot"
_DEBUGINFO_ROOT = "/usr/lib/debug/lib/modules"


def _vmlinux_dest(version: str) -> str:
    """The drgn-discoverable in-guest path for the running kernel's DWARF vmlinux (ADR-0221).

    drgn's ``-k`` debuginfo finder searches ``/usr/lib/debug/lib/modules/<uname -r>/vmlinux``;
    under direct-kernel boot ``uname -r`` is ``version`` (the ``/lib/modules/<ver>`` release), so
    the DWARF vmlinux must land there for the in-guest ``kdive-drgn`` helper to resolve typed
    symbols against ``/proc/kcore``.
    """
    return f"{_DEBUGINFO_ROOT}/{version}/vmlinux"


def _verify_vmlinux_size(size: int, overlay: str, dest: str) -> None:
    """Sentinel for the staged DWARF vmlinux: a zero-byte upload is always a failure (ADR-0221).

    A vmlinux is never legitimately empty, so a zero size means the upload was truncated or never
    landed and the in-guest drgn would fail to resolve types.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` (overlay + dest in details) if ``size`` is 0.
    """
    if size <= 0:
        raise CategorizedError(
            "vmlinux staging completed but the in-guest debuginfo file is empty after upload",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"overlay": overlay, "dest": dest},
        )


def _kernel_dest(version: str) -> str:
    """The in-guest path the from-source kernel is staged to for ``kdumpctl`` (ADR-0207).

    ``kdumpctl`` kexec-loads the crash kernel from ``/boot/vmlinuz-$(uname -r)``; under
    direct-kernel boot ``uname -r`` is ``version`` (the ``/lib/modules/<ver>`` release), so the
    kernel must land at exactly ``/boot/vmlinuz-<ver>``.
    """
    return f"{_BOOT_ROOT}/vmlinuz-{version}"


def _verify_kernel_size(size: int, overlay: str, dest: str) -> None:
    """Sentinel for the staged kernel: a zero-byte upload is always a failure (ADR-0207).

    Unlike the modules ``modules.dep`` sentinel (which must accept a valid empty file for an
    all-builtin kernel), a kernel image is never legitimately empty, so a zero size means the
    upload was truncated or never landed.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` (overlay + dest in details) if ``size`` is 0.
    """
    if size <= 0:
        raise CategorizedError(
            "kernel staging completed but /boot/vmlinuz is empty after upload",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"overlay": overlay, "dest": dest},
        )


class _GuestFS(Protocol):  # pragma: no cover - live_vm (libguestfs binding surface)
    """The subset of the libguestfs handle the kernel writer drives (typing only)."""

    def add_drive_opts(self, filename: str, *, format: str, readonly: int) -> None: ...
    def launch(self) -> None: ...
    def inspect_os(self) -> list[str]: ...
    def mount(self, device: str, mountpoint: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...
    def tar_in(self, tarfile: str, directory: str, *, compress: str) -> None: ...
    def command(self, arguments: list[str]) -> str: ...
    def is_file(self, path: str) -> int: ...
    def mkdir_p(self, path: str) -> None: ...
    def upload(self, filename: str, remotefilename: str) -> None: ...
    def statns(self, path: str) -> dict[str, int]: ...
    def shutdown(self) -> None: ...
    def close(self) -> None: ...


class _RealGuestKernelWriter:  # pragma: no cover - live_vm (libguestfs)
    """Stage the built kernel into a System overlay rw via libguestfs (ADR-0203/0207).

    Mirrors ``retrieve.py``'s ``_LibguestfsCoreReader`` idioms but mounts read-WRITE
    (``readonly=0``). One rw session writes both the modules tree and the kernel image so the
    kernel can never pair with a stale module tree (or vice versa). Injection is idempotent: the
    module version directory is clobbered before the tarball is extracted, ``depmod`` is run, and
    a ``modules.dep``-present sentinel is verified (an all-builtin kdump kernel leaves a valid
    *empty* ``modules.dep``, so that sentinel checks existence, not size); then the kernel is
    uploaded to ``/boot/vmlinuz-<ver>`` (``upload`` truncates/creates, so a retry self-heals a
    partial write) and a *non-empty* size sentinel is verified. A missing ``guestfs`` binding is a
    ``MISSING_DEPENDENCY``; any libguestfs/depmod fault is an ``INFRASTRUCTURE_FAILURE`` carrying
    the overlay path.
    """

    def inject(
        self, overlay: str, kernel_image: Path, modules_tar: Path, vmlinux: Path | None = None
    ) -> None:
        version = self._read_release(modules_tar, overlay)
        guest = self._mount_rw(overlay)
        try:
            self._extract_and_index(guest, overlay, str(modules_tar), version)
            self._stage_kernel(guest, overlay, str(kernel_image), version)
            if vmlinux is not None:
                self._stage_vmlinux(guest, overlay, str(vmlinux), version)
        finally:
            with contextlib.suppress(Exception):
                guest.shutdown()
            with contextlib.suppress(Exception):
                guest.close()

    @staticmethod
    def _mount_rw(overlay: str) -> _GuestFS:
        try:
            import guestfs  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
        except ImportError as exc:
            raise CategorizedError(
                "libguestfs (the guestfs Python binding) is required to stage the built kernel",
                category=ErrorCategory.MISSING_DEPENDENCY,
            ) from exc
        guest = guestfs.GuestFS(python_return_dict=True)
        try:
            guest.add_drive_opts(overlay, format="qcow2", readonly=0)
            guest.launch()
            roots = guest.inspect_os()
        except Exception as exc:
            guest.close()
            raise _RealGuestKernelWriter._io_failure(
                "opening the System overlay read-write", overlay, exc
            ) from exc
        if not roots:
            guest.close()
            raise CategorizedError(
                "could not inspect the System overlay to stage the built kernel",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"overlay": overlay},
            )
        guest.mount(roots[0], "/")
        return guest

    @staticmethod
    def _extract_and_index(guest: _GuestFS, overlay: str, tar: str, version: str) -> None:
        version_dir = f"{_MODULES_ROOT}/{version}"
        try:
            guest.rm_rf(version_dir)  # clobber any partial prior write (idempotent re-extract)
            guest.tar_in(tar, "/", compress="gzip")  # members are lib/modules/<ver>/...
            guest.command(["depmod", "-a", version])
        except Exception as exc:
            raise _RealGuestKernelWriter._io_failure(
                "extracting and indexing the kernel modules", overlay, exc
            ) from exc
        if not guest.is_file(f"{version_dir}/modules.dep"):
            raise CategorizedError(
                "module injection completed but modules.dep is absent after depmod",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"overlay": overlay, "version_dir": version_dir},
            )

    @staticmethod
    def _stage_kernel(guest: _GuestFS, overlay: str, kernel_image: str, version: str) -> None:
        """Upload the from-source kernel to ``/boot/vmlinuz-<ver>`` (ADR-0207), then verify size.

        Idempotent: ``mkdir_p`` and ``upload`` (truncate/create) self-heal a partial prior write.
        """
        dest = _kernel_dest(version)
        try:
            guest.mkdir_p(_BOOT_ROOT)
            guest.upload(kernel_image, dest)
            size = guest.statns(dest)["st_size"]
        except Exception as exc:
            raise _RealGuestKernelWriter._io_failure(
                "staging the from-source kernel into /boot", overlay, exc
            ) from exc
        _verify_kernel_size(size, overlay, dest)

    @staticmethod
    def _stage_vmlinux(guest: _GuestFS, overlay: str, vmlinux: str, version: str) -> None:
        """Upload the DWARF vmlinux to the drgn debuginfo path, then verify size (ADR-0221).

        Lands the run's ``vmlinux`` at ``/usr/lib/debug/lib/modules/<ver>/vmlinux`` so the
        in-guest ``kdive-drgn`` helper's ``drgn -k`` resolves typed symbols against
        ``/proc/kcore``. Idempotent: ``mkdir_p`` + truncating ``upload`` self-heal a partial write.
        """
        dest = _vmlinux_dest(version)
        try:
            guest.mkdir_p(f"{_DEBUGINFO_ROOT}/{version}")
            guest.upload(vmlinux, dest)
            size = guest.statns(dest)["st_size"]
        except Exception as exc:
            raise _RealGuestKernelWriter._io_failure(
                "staging the DWARF vmlinux for live drgn", overlay, exc
            ) from exc
        _verify_vmlinux_size(size, overlay, dest)

    @staticmethod
    def _read_release(modules_tar: Path, overlay: str) -> str:
        """The injected modules version: the dir name under ``lib/modules/`` in the host tarball.

        The build seam stages members as ``lib/modules/<ver>/...`` (the remote-consistent layout),
        so the version is the first path component after the ``lib/modules/`` prefix. It is read
        from the host-side archive (the tarball is on the worker filesystem, not yet in the
        appliance), avoiding a brittle appliance shell-out; the version drives the clobber target,
        the ``depmod`` argument, and the ``/boot/vmlinuz-<ver>`` kernel destination (ADR-0207).
        """
        prefix = _MODULES_ROOT.strip("/") + "/"
        try:
            with tarfile.open(modules_tar, "r:gz") as archive:
                for name in archive.getnames():
                    normalized = name.strip("/")
                    if normalized.startswith(prefix):
                        version = normalized[len(prefix) :].split("/", 1)[0]
                        if version:
                            return version
        except (OSError, tarfile.TarError) as exc:
            raise _RealGuestKernelWriter._io_failure(
                "reading the modules tarball version", overlay, exc
            ) from exc
        raise CategorizedError(
            "the modules tarball is empty; cannot determine the kernel version",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"overlay": overlay},
        )

    @staticmethod
    def _io_failure(op: str, overlay: str, exc: Exception) -> CategorizedError:
        return CategorizedError(
            f"libguestfs failed {op} for kernel staging",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"overlay": overlay, "error": type(exc).__name__},
        )


def _bounded_probe_error(message: str) -> str:
    return message[:200]


def _domain_exit_probe(domain_name: str) -> _DomainExitProbe:  # pragma: no cover - live_vm
    """Return whether ``virsh domstate`` reports terminal state plus probe diagnostics.

    A probe error/timeout or a transient non-running state (``paused``, ``in shutdown``)
    is not proof of exit (v1: a flaky/slow probe keeps waiting), so ``exited`` is
    ``False`` and the caller keeps polling (ADR-0055 §7). Probe failures keep a bounded
    diagnostic so a final boot timeout can distinguish a silent guest from a broken host
    probe.
    """
    uri = config.require(LIBVIRT_URI)
    virsh = shutil.which(_VIRSH)
    if virsh is None:
        return _DomainExitProbe(False, "virsh executable not found")
    try:
        proc = subprocess.run(  # noqa: S603 - resolved virsh; URI/domain are argv data, no shell
            [virsh, "-c", uri, "domstate", domain_name],
            capture_output=True,
            text=True,
            timeout=_DOMSTATE_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _DomainExitProbe(
            False,
            f"virsh domstate timed out after {exc.timeout:g}s",
        )
    except FileNotFoundError:
        return _DomainExitProbe(False, "virsh executable not found")
    except (subprocess.SubprocessError, OSError) as exc:
        return _DomainExitProbe(False, _bounded_probe_error(f"virsh domstate probe failed: {exc}"))
    if proc.stdout.strip().lower() in _TERMINAL_DOMSTATES:
        return _DomainExitProbe(True)
    stderr = proc.stderr.strip().lower()
    exited = (
        proc.returncode != 0
        and domain_name.startswith("kdive-")
        and "failed to get domain" in stderr
    )
    if exited:
        return _DomainExitProbe(True)
    if proc.returncode != 0:
        error = stderr or f"virsh domstate exited {proc.returncode}"
        return _DomainExitProbe(False, _bounded_probe_error(error))
    return _DomainExitProbe(False)


def _domain_exited(domain_name: str) -> bool:  # pragma: no cover - live_vm
    """True only if ``virsh domstate`` reports a terminal state (shut off / crashed)."""
    return _domain_exit_probe(domain_name).exited


def _verdict_to_result(verdict: ConsoleVerdict, *, exited: bool) -> ReadinessResult | None:
    """Map a console verdict (+ domain-exited flag) to a readiness result, or ``None``.

    Pure (host-free, the unit-tested core of the live probe, ADR-0055 §6/§7):

    - ``ready`` → answered + ok (the marker line was reached).
    - ``crashed`` → answered + not ok (a pre-marker crash signature — the demo's failure signal).
    - ``pending`` with the guest **exited** → answered + not ok (v1's ``exited``: it stopped
      without reaching the marker).
    - ``pending`` with the guest still running → ``None``, meaning "no answer yet, keep polling".
    """
    if verdict is ConsoleVerdict.READY:
        return ReadinessResult(answered=True, ok=True)
    if verdict is ConsoleVerdict.CRASHED:
        return ReadinessResult(answered=True, ok=False)
    if exited:
        return ReadinessResult(answered=True, ok=False)
    return None


def _real_readiness(system_id: UUID) -> ReadinessResult:  # pragma: no cover - live_vm
    """One run-readiness probe of the System's truncated console (ADR-0055 §6/§7).

    A single per-poll probe — ``boot()._await_ready`` drives the repetition. Reads the
    console, classifies it (`classify_console`), and maps the verdict (`_verdict_to_result`).
    On a ``pending`` verdict it re-reads once after a `virsh domstate` exit check so a marker
    or crash that landed just before the guest stopped is honored; a still-running guest
    sleeps one poll interval and stays unanswered, so the boot window (poll count × interval)
    elapses as ``boot_timeout`` if the System never comes up.
    """
    log_path = console_log_path(system_id)
    result = _verdict_to_result(classify_console(read_console_log(log_path)), exited=False)
    if result is not None:
        return result
    probe = _domain_exit_probe(domain_name_for(system_id))
    if probe.exited:
        return _verdict_to_result(
            classify_console(read_console_log(log_path)), exited=True
        ) or ReadinessResult(answered=True, ok=False)
    time.sleep(_POLL_INTERVAL_SECONDS)
    return ReadinessResult(answered=False, ok=False, probe_error=probe.error)


__all__ = [
    "LocalLibvirtInstall",
    "ReadinessResult",
    "classify_console",
    "read_console_log",
]
