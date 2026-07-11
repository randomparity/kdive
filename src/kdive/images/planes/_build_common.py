"""Shared mechanics for rootfs image build planes."""

from __future__ import annotations

import hashlib
import re
import subprocess  # noqa: S404 - libguestfs tools use fixed argv, no shell  # nosec B404
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.provenance_probes import (
    DEFAULT_BOOT_ENTRIES_PROBE,
    DEFAULT_KERNEL_CONFIG_PROBE,
    DEFAULT_MAKEDUMPFILE_PROBE,
    DEFAULT_OS_RELEASE_PROBE,
    DEFAULT_VERSION_INSPECT,
    MAKEDUMPFILE_MARKER_GUEST_PATH,
    BootEntriesProbeSeam,
    KernelConfigProbeSeam,
    MakedumpfileProbeSeam,
    OsReleaseProbeSeam,
    VersionInspectSeam,
    inspect_package_versions,
    parse_virt_inspector_versions,
    probe_boot_entries,
    probe_kernel_config,
    probe_makedumpfile_marker,
    probe_os_release,
)

__all__ = [
    "DEFAULT_BOOT_ENTRIES_PROBE",
    "DEFAULT_KERNEL_CONFIG_PROBE",
    "DEFAULT_MAKEDUMPFILE_PROBE",
    "DEFAULT_OS_RELEASE_PROBE",
    "DEFAULT_VERSION_INSPECT",
    "MAKEDUMPFILE_MARKER_GUEST_PATH",
    "BootEntriesProbeSeam",
    "KernelConfigProbeSeam",
    "MakedumpfileProbeSeam",
    "OsReleaseProbeSeam",
    "VersionInspectSeam",
    "build_workspace",
    "digest_file",
    "inspect_package_versions",
    "parse_virt_inspector_versions",
    "probe_boot_entries",
    "probe_kernel_config",
    "probe_makedumpfile_marker",
    "probe_os_release",
    "publish_qcow2",
    "run_guestfs_tool",
    "validate_image_name",
]

_DIGEST_CHUNK = 1024 * 1024
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# ADR-0222 (#694): two libguestfs stderr signatures get an actionable CONFIGURATION_ERROR
# instead of the generic PROVISIONING_FAILURE. The kernel pattern binds the permission/read
# failure to a vmlinuz path on one match so an unrelated permission error (an unwritable output
# qcow2, an SELinux denial on the scratch image, a workspace problem) is NOT misattributed to the
# host kernel; supermin's own "cannot read .../vmlinuz..." phrasing is covered by the same anchor.
_KERNEL_UNREADABLE_RE = re.compile(
    r"(?:/boot/)?vmlinuz[^\n'\"]*['\"]?[^\n]*?(?:Permission denied|cannot (?:open|read))"
    r"|(?:Permission denied|cannot (?:open|read))[^\n]*?vmlinuz",
)
_PASST_FAILURE_RE = re.compile(r"passt exited with status")

_KERNEL_REMEDIATION = (
    "the libguestfs appliance cannot read the host kernel — Debian/Ubuntu ship "
    "/boot/vmlinuz-* as root:0600. Make them readable (run as the worker user): "
    "`sudo chmod 0644 /boot/vmlinuz-*` (re-apply after a kernel upgrade, or use dpkg-statoverride)"
)
_PASST_REMEDIATION = (
    "the libguestfs appliance network (passt) failed. Unload the passt AppArmor profile "
    "(`sudo apparmor_parser -R /etc/apparmor.d/usr.bin.passt`); if it still fails (a "
    "libguestfs/passt version mismatch on Ubuntu 24.04), build the rootfs on a host with a "
    "working libguestfs appliance or stage a prebuilt bootable qcow2"
)


def _remediation_for_stderr(stderr: str) -> tuple[str, str] | None:
    """Return ``(message, remediation)`` for a recognized libguestfs failure, else ``None``."""
    if _KERNEL_UNREADABLE_RE.search(stderr):
        return ("libguestfs cannot read the host kernel /boot/vmlinuz-*", _KERNEL_REMEDIATION)
    if _PASST_FAILURE_RE.search(stderr):
        return ("the libguestfs appliance network (passt) failed", _PASST_REMEDIATION)
    return None


def validate_image_name(name: str) -> None:
    """Reject image names that could escape the build workspace."""
    if _NAME_RE.fullmatch(name):
        return
    raise CategorizedError(
        "image name must match ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ (it becomes a filename)",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"name": name},
    )


def run_guestfs_tool(
    argv: list[str],
    *,
    stage: str,
    timeout_s: int,
    missing_message: str,
    failure_message: str | None = None,
    input_text: str | None = None,
) -> None:
    """Run a fixed-argv libguestfs tool, mapping failures onto categorized errors."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell  # nosec B603
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            missing_message,
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"stage": stage, "tool": argv[0]},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            f"{stage} exceeded its timeout",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"stage": stage, "tool": argv[0], "timeout_s": timeout_s},
        ) from exc
    except OSError as exc:
        raise CategorizedError(
            f"failed to launch {argv[0]} for {stage}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stage": stage, "tool": argv[0], "error": type(exc).__name__},
        ) from exc
    if result.returncode != 0:
        known = _remediation_for_stderr(result.stderr)
        if known is not None:
            message, remediation = known
            raise CategorizedError(
                f"{stage}: {message}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "stage": stage,
                    "tool": argv[0],
                    "remediation": remediation,
                    "stderr": result.stderr[-2000:],
                },
            )
        raise CategorizedError(
            failure_message or f"{stage} failed",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"stage": stage, "tool": argv[0], "stderr": result.stderr[-2000:]},
        )


@contextmanager
def build_workspace(workspace: Path, *, prefix: str) -> Iterator[Path]:
    """Create the persistent workspace and yield a temporary per-build directory."""
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace, prefix=prefix) as work:
        yield Path(work)


def publish_qcow2(workspace: Path, *, image_name: str, scratch: Path) -> Path:
    """Atomically publish a scratch qcow2 into the persistent workspace."""
    qcow2 = workspace / f"{image_name}.qcow2"
    scratch.replace(qcow2)
    return qcow2


def digest_file(path: Path) -> str:
    """Return the ``sha256:<hex>`` content digest of ``path``."""
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_DIGEST_CHUNK), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise CategorizedError(
            "failed to read artifact for digest",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"path": str(path), "error": type(exc).__name__},
        ) from exc
    return f"sha256:{hasher.hexdigest()}"
