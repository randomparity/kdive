"""Local-libvirt provisioning storage and console-file lifecycle helpers."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess  # noqa: S404 - qemu-img is invoked with a fixed argv, no shell
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.runtime_paths import console_log_path

_log = logging.getLogger(__name__)

ROOTFS_DIR = "/var/lib/kdive/rootfs"
_QEMU_IMG_TIMEOUT_S = 5 * 60
_QEMU_IMG = "qemu-img"
_QEMU_IMG_ERROR_TAIL_CHARS = 2000
_BYTES_PER_GB = 1024**3


def overlay_path(system_id: UUID | str) -> str:
    """The per-System qcow2 overlay path."""
    return f"{ROOTFS_DIR}/{system_id}-overlay.qcow2"


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
    """Create the per-System qcow2 overlay backed by ``base`` with ``qemu-img``."""
    qemu_img = shutil.which(_QEMU_IMG)
    if qemu_img is None:
        raise CategorizedError(
            "qemu-img is not installed; cannot create the per-System rootfs overlay",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG),
        )
    try:
        result = subprocess.run(  # noqa: S603 - resolved qemu-img; base/overlay are argv data
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


def _real_overlay_virtual_size(overlay: str) -> int:
    """Return the overlay's qcow2 virtual size in bytes via ``qemu-img info``."""
    qemu_img = shutil.which(_QEMU_IMG)
    if qemu_img is None:
        raise CategorizedError(
            "qemu-img is not installed; cannot read the per-System overlay virtual size",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("overlay_info", overlay, tool=_QEMU_IMG),
        )
    result = subprocess.run(  # noqa: S603 - resolved qemu-img; overlay is argv data
        [qemu_img, "info", "--output=json", overlay],
        capture_output=True,
        text=True,
        timeout=_QEMU_IMG_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        raise CategorizedError(
            "qemu-img failed to read the per-System overlay virtual size",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={
                **_overlay_error_details("overlay_info", overlay, tool=_QEMU_IMG),
                "stderr": result.stderr[-_QEMU_IMG_ERROR_TAIL_CHARS:],
            },
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
    qemu_img = shutil.which(_QEMU_IMG)
    if qemu_img is None:
        raise CategorizedError(
            "qemu-img is not installed; cannot resize the per-System rootfs overlay",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("resize_overlay", overlay, tool=_QEMU_IMG),
        )
    result = subprocess.run(  # noqa: S603 - resolved qemu-img; overlay is argv data
        [qemu_img, "resize", overlay, f"{disk_gb}G"],
        capture_output=True,
        text=True,
        timeout=_QEMU_IMG_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        raise CategorizedError(
            "qemu-img failed to resize the per-System rootfs overlay",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={
                **_overlay_error_details("resize_overlay", overlay, tool=_QEMU_IMG),
                "disk_gb": disk_gb,
                "stderr": result.stderr[-_QEMU_IMG_ERROR_TAIL_CHARS:],
            },
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


def _prepare_console_log(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o644, exist_ok=True)
        path.chmod(0o644)
    except OSError as exc:
        raise CategorizedError(
            "failed to prepare libvirt console log",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"path": str(path)},
        ) from exc


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
            self._grow_if_requested(overlay, disk_gb)
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

    def cleanup_overlay_if_created(self, overlay: PreparedOverlay) -> None:
        if not overlay.created:
            return
        try:
            self.remove_overlay(overlay.path)
        except CategorizedError:
            _log.warning("failed to remove overlay after failed provision", exc_info=True)

    def remove_overlay_for_domain(self, domain_name: str) -> None:
        self.remove_overlay(overlay_path(domain_name.removeprefix("kdive-")))

    def remove_baseline_for_domain(self, domain_name: str) -> None:
        self.remove_baseline(baseline_dir(domain_name.removeprefix("kdive-")))
