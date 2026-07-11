"""Image provenance probes used by rootfs build planes."""

from __future__ import annotations

import subprocess  # noqa: S404 - libguestfs tools invoked with fixed argv, no shell
from collections.abc import Callable
from pathlib import Path
from xml.etree.ElementTree import fromstring as _xml_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory

# The in-guest marker file a debug build writes ``makedumpfile --version`` into, read back into
# ``provenance["makedumpfile_version"]`` (ADR-0253). Lives outside a family module so the build
# plane and the family customizers share one definition without a families->build cycle.
MAKEDUMPFILE_MARKER_GUEST_PATH = "/usr/lib/kdive/makedumpfile-version"

_VIRT_INSPECTOR_TIMEOUT_S = 5 * 60
_GUESTFISH_TIMEOUT_S = 5 * 60

type VersionInspectSeam = Callable[[Path], dict[str, str]]
type MakedumpfileProbeSeam = Callable[[Path], str | None]
type KernelConfigProbeSeam = Callable[[Path, str], bytes | None]
type BootEntriesProbeSeam = Callable[[Path], list[str] | None]
type OsReleaseProbeSeam = Callable[[Path], str | None]


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
        return None
    return result.stdout.strip() or None


DEFAULT_MAKEDUMPFILE_PROBE: MakedumpfileProbeSeam = probe_makedumpfile_marker


def probe_kernel_config(  # pragma: no cover - live_vm
    qcow2_path: Path, version: str
) -> bytes | None:
    """Read ``/boot/config-<version>`` from ``qcow2_path``, read-only via ``guestfish``.

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
        return None
    return result.stdout or None


DEFAULT_KERNEL_CONFIG_PROBE: KernelConfigProbeSeam = probe_kernel_config


def probe_boot_entries(qcow2_path: Path) -> list[str] | None:  # pragma: no cover - live_vm
    """List the ``/boot`` entry basenames in ``qcow2_path``, read-only via ``guestfish``.

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
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


DEFAULT_BOOT_ENTRIES_PROBE: BootEntriesProbeSeam = probe_boot_entries


def probe_os_release(qcow2_path: Path) -> str | None:  # pragma: no cover - live_vm
    """Read ``/etc/os-release`` from ``qcow2_path`` (``/usr/lib`` fallback), read-only.

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
    return None


DEFAULT_OS_RELEASE_PROBE: OsReleaseProbeSeam = probe_os_release
