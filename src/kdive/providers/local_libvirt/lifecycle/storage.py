"""Local-libvirt provisioning storage and console-file lifecycle helpers."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess  # noqa: S404 - qemu-img uses fixed argv, no shell  # nosec B404
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory

# ``ROOTFS_DIR``/``UPLOADS_DIR``/``overlay_name``/``overlay_path`` are re-exported here (the
# explicit ``as`` aliases mark the re-export) for provider consumers (provisioning, tests). Their
# canonical home — and the reconciler's provider-neutral access — is
# ``kdive.providers.shared.runtime_paths``, keeping the reconciler off
# ``kdive.providers.local_libvirt`` (the provider-boundary guard).
from kdive.providers.shared.runtime_paths import ROOTFS_DIR as ROOTFS_DIR
from kdive.providers.shared.runtime_paths import UPLOADS_DIR as UPLOADS_DIR
from kdive.providers.shared.runtime_paths import console_log_path, system_id_from_domain_name
from kdive.providers.shared.runtime_paths import overlay_name as overlay_name
from kdive.providers.shared.runtime_paths import overlay_path as overlay_path

_QEMU_IMG_TIMEOUT_S = 5 * 60
_QEMU_IMG = "qemu-img"
_QEMU_IMG_ERROR_TAIL_CHARS = 2000
_BYTES_PER_GB = 1024**3
_SHARED_LIBVIRT_FILE_MODE = 0o664


def baseline_dir(system_id: UUID | str) -> str:
    """The per-System directory holding the extracted baseline kernel/initrd (ADR-0272)."""
    return f"{ROOTFS_DIR}/{system_id}-baseline"


def _real_remove_baseline(baseline: str) -> None:
    """Remove a System's baseline directory; an absent directory is the achieved post-state."""
    try:
        shutil.rmtree(baseline)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CategorizedError(
            "failed to remove the per-System baseline kernel directory",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "remove_baseline", "baseline": Path(baseline).name},
        ) from exc


def _real_make_overlay(base: str, overlay: str) -> None:
    """Create a qcow2 overlay and make it writable by the shared session-libvirt group."""
    qemu_img = shutil.which(_QEMU_IMG)
    if qemu_img is None:
        raise CategorizedError(
            "qemu-img is not installed; cannot create the per-System rootfs overlay",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG),
        )
    try:
        result = subprocess.run(  # noqa: S603 - qemu-img argv; paths are data  # nosec B603
            [qemu_img, "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", base, overlay],
            capture_output=True,
            text=True,
            timeout=_QEMU_IMG_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "qemu-img is not installed; cannot create the per-System rootfs overlay",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG),
        ) from exc
    except OSError as exc:
        details = _overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG)
        details["error"] = type(exc).__name__
        raise CategorizedError(
            "failed to launch qemu-img to create the per-System rootfs overlay",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details=details,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "qemu-img exceeded the overlay creation timeout",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={
                **_overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG),
                "timeout_s": _QEMU_IMG_TIMEOUT_S,
            },
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "qemu-img failed to create the per-System rootfs overlay",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={
                **_overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG),
                "stderr": result.stderr[-_QEMU_IMG_ERROR_TAIL_CHARS:],
            },
        )
    try:
        os.chmod(overlay, _SHARED_LIBVIRT_FILE_MODE)
    except OSError as exc:
        details = _overlay_error_details("chmod_overlay", overlay)
        details["error"] = type(exc).__name__
        raise CategorizedError(
            "failed to grant shared-libvirt write access to the per-System rootfs overlay",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details=details,
        ) from exc


def _run_qemu_img(
    argv: list[str],
    *,
    op: str,
    action: str,
    overlay: str,
    extra_details: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``qemu-img`` subcommand against ``overlay`` with uniform fail-closed error mapping.

    Resolves the binary (``MISSING_DEPENDENCY`` if absent or a launch ``FileNotFoundError``),
    runs it under the shared timeout, and maps a launch ``OSError`` to ``INFRASTRUCTURE_FAILURE``
    and a timeout or non-zero exit to ``PROVISIONING_FAILURE``. ``action`` completes the message
    "... to {action}"; ``op`` keys the error details. Returns the completed process on success.
    """

    def _details() -> dict[str, object]:
        details = _overlay_error_details(op, overlay, tool=_QEMU_IMG)
        if extra_details:
            details.update(extra_details)
        return details

    qemu_img = shutil.which(_QEMU_IMG)
    if qemu_img is None:
        raise CategorizedError(
            f"qemu-img is not installed; cannot {action}",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_details(),
        )
    try:
        result = subprocess.run(  # noqa: S603 - qemu-img argv data, no shell  # nosec B603
            [qemu_img, *argv],
            capture_output=True,
            text=True,
            timeout=_QEMU_IMG_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            f"qemu-img is not installed; cannot {action}",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_details(),
        ) from exc
    except OSError as exc:
        details = _details()
        details["error"] = type(exc).__name__
        raise CategorizedError(
            f"failed to launch qemu-img to {action}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details=details,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            f"qemu-img exceeded the timeout to {action}",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={**_details(), "timeout_s": _QEMU_IMG_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            f"qemu-img failed to {action}",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={**_details(), "stderr": result.stderr[-_QEMU_IMG_ERROR_TAIL_CHARS:]},
        )
    return result


def _real_overlay_virtual_size(overlay: str) -> int:
    """Return the overlay's qcow2 virtual size in bytes via ``qemu-img info``."""
    result = _run_qemu_img(
        ["info", "--output=json", overlay],
        op="overlay_info",
        action="read the per-System overlay virtual size",
        overlay=overlay,
    )
    try:
        return int(json.loads(result.stdout)["virtual-size"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise CategorizedError(
            "qemu-img info returned no readable virtual-size for the per-System overlay",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details=_overlay_error_details("overlay_info", overlay, tool=_QEMU_IMG),
        ) from exc


def _real_resize_overlay(overlay: str, disk_gb: int) -> None:
    """Grow the overlay's qcow2 virtual size to ``disk_gb`` GB via ``qemu-img resize``."""
    _run_qemu_img(
        ["resize", overlay, f"{disk_gb}G"],
        op="resize_overlay",
        action="resize the per-System rootfs overlay",
        overlay=overlay,
        extra_details={"disk_gb": disk_gb},
    )


def _real_remove_overlay(overlay: str) -> None:
    """Remove a System's overlay file; an absent file is the achieved post-state."""
    try:
        Path(overlay).unlink(missing_ok=True)
    except OSError as exc:
        details = _overlay_error_details("remove_overlay", overlay)
        details["error"] = type(exc).__name__
        raise CategorizedError(
            "failed to remove the per-System rootfs overlay",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details=details,
        ) from exc


def _overlay_error_details(op: str, overlay: str, *, tool: str | None = None) -> dict[str, object]:
    details: dict[str, object] = {"op": op, "overlay": Path(overlay).name}
    if tool is not None:
        details["tool"] = tool
    return details


def _real_overlay_exists(overlay: str) -> bool:
    return Path(overlay).exists()


type MakeOverlay = Callable[[str, str], None]
type ResizeOverlay = Callable[[str, int], None]
type OverlayVirtualSize = Callable[[str], int]
type RemoveOverlay = Callable[[str], None]
type RemoveBaseline = Callable[[str], None]
type OverlayExists = Callable[[str], bool]
type PrepareConsoleLog = Callable[[Path], None]


def _console_identity_failure(path: Path, reason: str) -> CategorizedError:
    return CategorizedError(
        "console log lacks the worker-owned identity the domain start requires",
        category=ErrorCategory.PROVISIONING_FAILURE,
        details={"path": str(path), "reason": reason},
    )


def _open_failure_reason(exc: OSError) -> str:
    if exc.errno == errno.ELOOP:
        return "path is a symlink"
    if exc.errno == errno.EISDIR:
        return "path is a directory"
    if isinstance(exc, PermissionError):
        # The shared session daemon recreates the log as root:0600 when a start happens
        # out-of-band (ADR-0576); the worker cannot open it back.
        return "open failed with PermissionError; the daemon may have recreated it foreign-owned"
    return type(exc).__name__


def _prepare_console_log(path: Path) -> None:
    """Ensure ``path`` is a worker-owned regular file holding only the next boot's bytes.

    Creates it mode ``0664`` when absent, opens with ``O_NOFOLLOW``, verifies the opened
    inode's identity — a regular file owned by this worker with exactly one link — restores
    ``0664``, then truncates to zero. The group write bit lets the operator-owned session
    daemon append through the shared ``kdive-live-libvirt`` group inherited from the setgid
    console directory. This worker-side per-start truncate replaces virtlogd's ``append="off"``
    truncation (superseded by ADR-0576, #1940): rendered ``append="on"`` (``xml.py``), the
    daemon appends to this surviving worker-owned inode instead of unlinking and recreating
    the log as ``root:0600``, so fixed non-root workers keep reading their own boot and
    readiness evidence under the shared session endpoint.

    An unsafe identity fails the start instead of booting against evidence the worker cannot
    read or trust: a symlinked path, a foreign-owned replacement (the daemon-recreated log
    left by an out-of-band ``virsh start``), or a hard-linked file.

    Raises:
        CategorizedError: ``PROVISIONING_FAILURE`` naming the path when the directory cannot
            be created, the file cannot be opened by this worker, or its identity fails.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, _SHARED_LIBVIRT_FILE_MODE)
    except OSError as open_err:
        raise _console_identity_failure(path, _open_failure_reason(open_err)) from open_err
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _console_identity_failure(path, "not a regular file")
        euid = os.geteuid()
        if st.st_uid != euid:
            raise _console_identity_failure(path, f"owned by uid {st.st_uid}, not {euid}")
        if st.st_nlink != 1:
            raise _console_identity_failure(path, f"carries {st.st_nlink} links")
        os.fchmod(fd, _SHARED_LIBVIRT_FILE_MODE)
        os.ftruncate(fd, 0)
    except OSError as io_err:
        raise _console_identity_failure(path, type(io_err).__name__) from io_err
    finally:
        os.close(fd)


def prepare_console_for_domain(domain_name: str) -> None:
    """Prepare the console log of the System behind ``kdive-<uuid>`` before a host-side start.

    The name-keyed entry point for planes that hold only a domain name (control power-on,
    snapshot revert): resolves the encoded System UUID and truncates its log through
    :func:`_prepare_console_log` (ADR-0576).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for any name that is not a bare System
            domain; ``PROVISIONING_FAILURE`` from :func:`_prepare_console_log`.
    """
    system_id = system_id_from_domain_name(domain_name)
    if system_id is None:
        raise CategorizedError(
            f"cannot prepare the console log of non-System domain {domain_name!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"domain": domain_name},
        )
    _prepare_console_log(console_log_path(system_id))


@dataclass(frozen=True, slots=True)
class PreparedOverlay:
    path: str
    created: bool


@dataclass(frozen=True, slots=True)
class ProvisioningFiles:
    make_overlay: MakeOverlay = _real_make_overlay
    resize_overlay: ResizeOverlay = _real_resize_overlay
    overlay_virtual_size: OverlayVirtualSize = _real_overlay_virtual_size
    remove_overlay: RemoveOverlay = _real_remove_overlay
    remove_baseline: RemoveBaseline = _real_remove_baseline
    overlay_exists: OverlayExists = _real_overlay_exists
    # The baseline directory presence check reuses the overlay path-presence predicate.
    baseline_exists: OverlayExists = _real_overlay_exists
    prepare_console_log: PrepareConsoleLog = _prepare_console_log

    def prepare_overlay(
        self, system_id: UUID, *, base: str, disk_gb: int | None
    ) -> PreparedOverlay:
        overlay = overlay_path(system_id)
        created = not self.overlay_exists(overlay)
        if created:
            self.make_overlay(base, overlay)
            try:
                self._grow_if_requested(overlay, disk_gb)
            except CategorizedError:
                # A resize failure after creating the overlay must reclaim it, so a retry
                # re-creates and re-grows cleanly — never reusing an un-grown overlay, which
                # would silently boot the guest at the base size (the phantom-knob regression).
                self.remove_overlay(overlay)
                raise
        return PreparedOverlay(path=overlay, created=created)

    def _grow_if_requested(self, overlay: str, disk_gb: int | None) -> None:
        """Grow the just-created overlay to ``disk_gb`` (grow-only; ADR-0312, ADR-0060).

        Runs only on the create path (a running/reused overlay is never resized). Grows only
        when ``disk_gb`` exceeds the current virtual size, so a request at or below the base
        size is a no-op and the qcow2 is never shrunk below its backing file.
        """
        if disk_gb is None:
            return
        if disk_gb * _BYTES_PER_GB > self.overlay_virtual_size(overlay):
            self.resize_overlay(overlay, disk_gb)

    def prepare_console(self, system_id: UUID) -> None:
        self.prepare_console_log(console_log_path(system_id))

    def remove_overlay_for_domain(self, domain_name: str) -> None:
        self.remove_overlay(overlay_path(domain_name.removeprefix("kdive-")))

    def remove_baseline_for_domain(self, domain_name: str) -> None:
        self.remove_baseline(baseline_dir(domain_name.removeprefix("kdive-")))
