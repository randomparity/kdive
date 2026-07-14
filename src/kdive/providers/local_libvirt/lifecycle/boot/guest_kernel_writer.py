"""Guest overlay writer for local-libvirt kernel installs."""

from __future__ import annotations

import contextlib
import logging
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Protocol, cast

from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)

_MODULES_ROOT = "/lib/modules"
_BOOT_ROOT = "/boot"
_DEBUGINFO_ROOT = "/usr/lib/debug/lib/modules"
# The relative in-tar / in-guest modules root (``_MODULES_ROOT`` without the leading slash).
_MODULES_TREE = _MODULES_ROOT.lstrip("/")
_DEPMOD_STDERR_MAX = 500


class DepmodRunner(Protocol):
    """Run ``depmod`` against an extracted module tree rooted at ``basedir``."""

    def __call__(self, *, basedir: Path, version: str) -> None: ...


def _run_host_depmod(*, basedir: Path, version: str) -> None:
    """Index ``basedir/lib/modules/<version>`` with the **host** ``depmod`` (ADR-0346).

    ``depmod`` parses each module's ELF header and symbol tables to build ``modules.dep``; it never
    executes module code, so the host's ``depmod`` indexes a foreign-arch (e.g. ppc64le) tree
    correctly under an x86_64 host — the cross-arch fix for #1148. ``-b <basedir>`` points it at the
    extracted tree; the produced index is host-endianness-independent (kmod writes the ``.bin``
    files in network byte order and both x86_64 and ppc64le are little-endian anyway).

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` when no ``depmod`` binary is on ``PATH``;
            ``INFRASTRUCTURE_FAILURE`` on a non-zero exit, carrying the trimmed ``depmod`` stderr
            in ``details`` so the cause is legible from the tool envelope (the #1146 note).
    """
    try:
        result = subprocess.run(
            ["depmod", "-b", str(basedir), version],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "depmod is required on the worker host to index kernel modules for staging; "
            "install kmod (provides depmod)",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[-_DEPMOD_STDERR_MAX:]
        raise CategorizedError(
            "host depmod failed indexing the kernel modules for staging",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"version": version, "depmod_stderr": stderr},
        )


def index_modules_tar(
    modules_tar: Path, version: str, *, workdir: Path, run_depmod: DepmodRunner = _run_host_depmod
) -> Path:
    """Extract a modules tar, index it host-side, and repack the indexed tree (ADR-0346).

    ``modules_tar`` is the ``lib/modules/<version>/`` subtree repacked from the combined kernel tar
    (``repack_modules_subtree``). It is extracted under ``workdir`` (an existing dir the caller
    owns — typically a ``TemporaryDirectory``), ``run_depmod`` indexes it in place, and the whole
    ``lib/modules/`` subtree — now carrying ``modules.dep`` and the ``.bin`` indices — is repacked
    to a gzip tar the caller ``tar_in``s into the guest overlay. No guest binary is executed.

    Args:
        modules_tar: The modules-only gzip tar (members under ``lib/modules/<version>/``).
        version: The kernel release, e.g. ``6.19.10-300.fc44.ppc64le`` (arch suffix preserved).
        workdir: An existing directory to extract and repack under.
        run_depmod: The indexing seam (defaults to the host ``depmod``); injected in tests.

    Returns:
        The path to the repacked, indexed ``lib/modules/`` gzip tar under ``workdir``.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` if the extract fails or ``modules.dep`` is
            absent after indexing; the ``run_depmod`` seam's own categories propagate.
    """
    try:
        with tarfile.open(modules_tar, "r:gz") as archive:
            archive.extractall(workdir, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise CategorizedError(
            "failed to extract the modules tar for host-side indexing",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"modules_tar": str(modules_tar)},
        ) from exc
    run_depmod(basedir=workdir, version=version)
    version_dir = workdir / _MODULES_TREE / version
    if not (version_dir / "modules.dep").is_file():
        raise CategorizedError(
            "module indexing completed but modules.dep is absent after depmod",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"version_dir": str(version_dir)},
        )
    indexed = workdir / "indexed-modules.tar.gz"
    try:
        with tarfile.open(indexed, "w:gz") as out:
            out.add(workdir / _MODULES_TREE, arcname=_MODULES_TREE)
    except (OSError, tarfile.TarError) as exc:
        raise CategorizedError(
            "failed to repack the indexed modules tree for staging",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"dest": str(indexed)},
        ) from exc
    return indexed


class GuestKernelWriter(Protocol):
    """Stage a built kernel into a System overlay."""

    def inject(
        self, overlay: str, kernel_image: Path, modules_tar: Path, vmlinux: Path | None = None
    ) -> None: ...


def _vmlinux_dest(version: str) -> str:
    """The drgn-discoverable in-guest path for the running kernel's DWARF vmlinux."""
    return f"{_DEBUGINFO_ROOT}/{version}/vmlinux"


def _verify_vmlinux_size(size: int, overlay: str, dest: str) -> None:
    """Fail if a staged DWARF vmlinux is empty."""
    if size <= 0:
        raise CategorizedError(
            "vmlinux staging completed but the in-guest debuginfo file is empty after upload",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"overlay": overlay, "dest": dest},
        )


def _kernel_dest(version: str) -> str:
    """The in-guest path the from-source kernel is staged to for ``kdumpctl``."""
    return f"{_BOOT_ROOT}/vmlinuz-{version}"


def _verify_kernel_size(size: int, overlay: str, dest: str) -> None:
    """Fail if a staged kernel image is empty."""
    if size <= 0:
        raise CategorizedError(
            "kernel staging completed but /boot/vmlinuz is empty after upload",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"overlay": overlay, "dest": dest},
        )


class _GuestFS(Protocol):  # pragma: no cover - live_vm (libguestfs binding surface)
    """The subset of the libguestfs handle the kernel writer drives."""

    def add_drive_opts(
        self, filename: str, *, format: str, readonly: bool | None = None
    ) -> None: ...
    def launch(self) -> None: ...
    def inspect_os(self) -> list[str]: ...
    def mount(self, device: str, mountpoint: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...
    def tar_in(self, tarfile: str, directory: str, *, compress: str) -> None: ...
    def command(self, arguments: list[str]) -> str: ...
    # Mirrors the libguestfs binding's integer truth value; call sites wrap it as bool.
    def is_file(self, path: str) -> int: ...
    def mkdir_p(self, path: str) -> None: ...
    def upload(self, filename: str, remotefilename: str) -> None: ...
    def statns(self, path: str) -> dict[str, int]: ...
    def shutdown(self) -> None: ...
    def close(self) -> None: ...


class _RealGuestKernelWriter:  # pragma: no cover - live_vm (libguestfs)
    """Stage the built kernel into a System overlay rw via libguestfs."""

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
        guest = cast("_GuestFS", guestfs.GuestFS(python_return_dict=True))
        try:
            guest.add_drive_opts(overlay, format="qcow2", readonly=False)
            guest.launch()
            roots = guest.inspect_os()
        except Exception as exc:
            _close_guestfs_handle(guest, "after failed kernel-staging overlay open")
            raise _RealGuestKernelWriter._io_failure(
                "opening the System overlay read-write", overlay, exc
            ) from exc
        if not roots:
            _close_guestfs_handle(guest, "after empty kernel-staging inspection")
            raise CategorizedError(
                "could not inspect the System overlay to stage the built kernel",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"overlay": overlay},
            )
        guest.mount(roots[0], "/")
        return guest

    @staticmethod
    def _extract_and_index(guest: _GuestFS, overlay: str, tar: str, version: str) -> None:
        """Index the module tree host-side (`depmod -b`) and inject it into the overlay (ADR-0346).

        The tree is indexed on the host — where ``depmod`` runs natively regardless of the module
        arch — then the ready-indexed tree is ``tar_in``'d into the guest (a file op libguestfs
        does cross-arch). This replaces running the guest's own ``depmod`` inside the host-arch
        appliance, which fails for a foreign-arch (ppc64le) tree (#1146, #1148).
        """
        version_dir = f"{_MODULES_ROOT}/{version}"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                indexed = index_modules_tar(Path(tar), version, workdir=Path(tmp))
                guest.rm_rf(version_dir)
                guest.tar_in(str(indexed), "/", compress="gzip")
        except CategorizedError:
            raise
        except Exception as exc:
            raise _RealGuestKernelWriter._io_failure(
                "extracting and indexing the kernel modules", overlay, exc
            ) from exc
        if not _guest_path_is_file(guest, f"{version_dir}/modules.dep"):
            raise CategorizedError(
                "module injection completed but modules.dep is absent after depmod",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"overlay": overlay, "version_dir": version_dir},
            )

    @staticmethod
    def _stage_kernel(guest: _GuestFS, overlay: str, kernel_image: str, version: str) -> None:
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


def _guest_path_is_file(guest: _GuestFS, path: str) -> bool:
    return bool(guest.is_file(path))


def _close_guestfs_handle(guest: _GuestFS, context: str) -> None:
    try:
        guest.close()
    except Exception:
        _log.warning(
            "libguestfs close failed %s; preserving original failure", context, exc_info=True
        )
