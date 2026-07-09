"""Shared mechanics for rootfs image build planes."""

from __future__ import annotations

import hashlib
import re
import subprocess  # noqa: S404 - libguestfs tools invoked with fixed argv, no shell
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from xml.etree.ElementTree import fromstring as _xml_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory

_DIGEST_CHUNK = 1024 * 1024
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# The in-guest marker file a debug build writes ``makedumpfile --version`` into, read back into
# ``provenance["makedumpfile_version"]`` (ADR-0253). Lives here (not in a family module) so the
# build plane and the family customizers share one definition without a families->build cycle.
MAKEDUMPFILE_MARKER_GUEST_PATH = "/usr/lib/kdive/makedumpfile-version"

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
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted inputs
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


_VIRT_INSPECTOR_TIMEOUT_S = 5 * 60

type VersionInspectSeam = Callable[[Path], dict[str, str]]


def parse_virt_inspector_versions(xml: str) -> dict[str, str]:
    """Map each ``<application>`` with a ``<name>`` and ``<version>`` to ``{name: version}``.

    Applications missing a name or version are skipped. A DOCTYPE is rejected up front so a
    crafted package name cannot trigger entity expansion (stdlib ElementTree expands internal
    entities only when a DTD is present).
    """
    if "<!DOCTYPE" in xml:
        raise ValueError("DOCTYPE is not allowed in virt-inspector output")
    root = _xml_fromstring(xml)  # noqa: S314 - trusted virt-inspector output; DOCTYPE rejected above
    versions: dict[str, str] = {}
    for app in root.iter("application"):
        name = app.findtext("name")
        version = app.findtext("version")
        if name and version:
            versions[name] = version
    return versions


def inspect_package_versions(qcow2_path: Path) -> dict[str, str]:  # pragma: no cover - live_vm
    """Return the full installed ``{name: version}`` map for ``qcow2_path`` via ``virt-inspector``.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` if ``virt-inspector`` is absent;
            ``INFRASTRUCTURE_FAILURE`` on timeout or a non-zero exit.
    """
    argv = ["virt-inspector", "--no-icon", "-a", str(qcow2_path)]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv; image path is a data arg
            argv,
            capture_output=True,
            text=True,
            timeout=_VIRT_INSPECTOR_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "virt-inspector is not installed; cannot capture package versions",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": "virt-inspector"},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "virt-inspector exceeded its timeout",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _VIRT_INSPECTOR_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "virt-inspector failed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": result.stderr[-2000:]},
        )
    return parse_virt_inspector_versions(result.stdout)


DEFAULT_VERSION_INSPECT: VersionInspectSeam = inspect_package_versions

_GUESTFISH_TIMEOUT_S = 5 * 60

type MakedumpfileProbeSeam = Callable[[Path], str | None]


def probe_makedumpfile_marker(qcow2_path: Path) -> str | None:  # pragma: no cover - live_vm
    """Read the build-written ``makedumpfile --version`` marker from ``qcow2_path``, read-only.

    The debug build records the in-guest ``makedumpfile --version`` banner into
    :data:`MAKEDUMPFILE_MARKER_GUEST_PATH`; this reads it back with a read-only ``guestfish``
    ``download`` so the version is the binary's own report (authoritative on EL8/EL9, where
    makedumpfile is bundled in ``kexec-tools`` and invisible to ``virt-inspector``).

    Returns:
        The marker's stripped text, or ``None`` when the marker is absent/empty (a non-debug or
        makedumpfile-less image) — never raising for a missing marker.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` if ``guestfish`` is absent;
            ``INFRASTRUCTURE_FAILURE`` on timeout.
    """
    argv = ["guestfish", "--ro", "-a", str(qcow2_path), "-i", "cat", MAKEDUMPFILE_MARKER_GUEST_PATH]
    try:
        result = subprocess.run(  # noqa: S603 - fixed guestfish argv; image path is a data arg
            argv,
            capture_output=True,
            text=True,
            timeout=_GUESTFISH_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "guestfish is not installed; cannot read the makedumpfile-version marker",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": "guestfish"},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "guestfish exceeded its timeout reading the makedumpfile-version marker",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _GUESTFISH_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        return None  # marker absent (non-debug / makedumpfile-less image) is not an error
    return result.stdout.strip() or None


DEFAULT_MAKEDUMPFILE_PROBE: MakedumpfileProbeSeam = probe_makedumpfile_marker

type KernelConfigProbeSeam = Callable[[Path, str], bytes | None]


def probe_kernel_config(  # pragma: no cover - live_vm
    qcow2_path: Path, version: str
) -> bytes | None:
    """Read ``/boot/config-<version>`` from ``qcow2_path``, read-only via ``guestfish`` (ADR-0317).

    The build-time operand of the kernel-config offer: the caller writes the returned **bytes**
    verbatim to the object store so the agent can fetch its selected image's known-good starting
    config unaltered. Reads raw bytes (no ``text=True`` decode or newline translation) with a
    read-only ``guestfish -i cat /boot/config-<version>``, so the stored object is byte-identical to
    the on-image file (ADR-0317 §Decision 4, no validation).

    Returns:
        The config file bytes, or ``None`` when it is absent (a non-zero ``guestfish`` exit or an
        empty body) — never raising for a merely-missing config; the caller treats ``None`` as "no
        config offered" and omits it.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` if ``guestfish`` is absent;
            ``INFRASTRUCTURE_FAILURE`` on timeout. Both are caught by the advisory caller and
            degrade to an omitted config, so a probe failure never fails a build.
    """
    guest_path = f"/boot/config-{version}"
    argv = ["guestfish", "--ro", "-a", str(qcow2_path), "-i", "cat", guest_path]
    try:
        result = subprocess.run(  # noqa: S603 - fixed guestfish argv; image path is a data arg
            argv,
            capture_output=True,
            timeout=_GUESTFISH_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "guestfish is not installed; cannot read the kernel config",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": "guestfish"},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "guestfish exceeded its timeout reading the kernel config",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _GUESTFISH_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        return None  # an absent /boot/config-<ver> is not an error; caller omits the config
    return result.stdout or None


DEFAULT_KERNEL_CONFIG_PROBE: KernelConfigProbeSeam = probe_kernel_config

type BootEntriesProbeSeam = Callable[[Path], list[str] | None]


def probe_boot_entries(qcow2_path: Path) -> list[str] | None:  # pragma: no cover - live_vm
    """List the ``/boot`` entry basenames in ``qcow2_path``, read-only via ``guestfish`` (ADR-0295).

    The build-time operand of the ``direct_kernel`` capability signal: the caller classifies the
    listing with the same non-rescue ``vmlinuz-*`` rule provisioning uses, so the recorded
    ``boot_kernel_count`` predicts the fail-closed baseline-kernel selection. Reads with a read-only
    ``guestfish -i ls /boot`` so the count is the built image's own ``/boot``.

    Returns:
        The ``/boot`` basenames (empty list when ``/boot`` is empty), or ``None`` when the listing
        could not be produced (a non-zero ``guestfish`` exit — e.g. an unmountable image). Never
        raises for a merely-empty or unreadable ``/boot``; the caller treats ``None`` as "operand
        absent" and omits it from provenance.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` if ``guestfish`` is absent;
            ``INFRASTRUCTURE_FAILURE`` on timeout. Both are caught by the advisory caller and
            degrade to an omitted operand, so a probe failure never fails a build.
    """
    argv = ["guestfish", "--ro", "-a", str(qcow2_path), "-i", "ls", "/boot"]
    try:
        result = subprocess.run(  # noqa: S603 - fixed guestfish argv; image path is a data arg
            argv,
            capture_output=True,
            text=True,
            timeout=_GUESTFISH_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "guestfish is not installed; cannot list /boot to count baseline kernels",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": "guestfish"},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "guestfish exceeded its timeout listing /boot",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _GUESTFISH_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        return None  # an unmountable/absent /boot is not an error; caller omits the operand
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


DEFAULT_BOOT_ENTRIES_PROBE: BootEntriesProbeSeam = probe_boot_entries

type OsReleaseProbeSeam = Callable[[Path], str | None]


def probe_os_release(qcow2_path: Path) -> str | None:  # pragma: no cover - live_vm
    """Read ``/etc/os-release`` from ``qcow2_path`` (``/usr/lib`` fallback), read-only (ADR-0311).

    The build-time operand of the verified OS-identity provenance: the caller parses
    ``ID``/``VERSION_ID``/``PRETTY_NAME`` into ``provenance["os_release"]``, so the record carries
    the built image's own release rather than the operator-assigned catalog name. Tries
    ``/etc/os-release`` first (a guest symlink to ``/usr/lib/os-release`` is followed inside the
    image), then ``/usr/lib/os-release`` for a distro that ships only the vendor copy.

    Returns:
        The raw os-release file text, or ``None`` when neither path could be read (a non-zero
        ``guestfish`` exit for both, or an empty body). Never raises for a merely-absent file; the
        caller treats ``None`` as "operand absent" and omits it from provenance.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` if ``guestfish`` is absent;
            ``INFRASTRUCTURE_FAILURE`` on timeout. Both are caught by the advisory caller and
            degrade to an omitted operand, so a probe failure never fails a build.
    """
    for guest_path in ("/etc/os-release", "/usr/lib/os-release"):
        argv = ["guestfish", "--ro", "-a", str(qcow2_path), "-i", "cat", guest_path]
        try:
            result = subprocess.run(  # noqa: S603 - fixed guestfish argv; image path is a data arg
                argv,
                capture_output=True,
                text=True,
                timeout=_GUESTFISH_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CategorizedError(
                "guestfish is not installed; cannot read /etc/os-release",
                category=ErrorCategory.MISSING_DEPENDENCY,
                details={"tool": "guestfish"},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "guestfish exceeded its timeout reading os-release",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"timeout_s": _GUESTFISH_TIMEOUT_S},
            ) from exc
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    return None  # neither path present; caller omits the operand


DEFAULT_OS_RELEASE_PROBE: OsReleaseProbeSeam = probe_os_release
