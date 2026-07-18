"""Provider-runtime naming and host artifact path helpers."""

from __future__ import annotations

import contextlib
import logging
import os
import pwd
import re
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)

_CONSOLE_DIR = "/var/lib/kdive/console"
_PCAP_DIR = "/var/lib/kdive/pcap"

# The qemu:///system hypervisor runtime user (in preference order). QEMU's filter-dump writes the
# pcap as this unprivileged, SELinux-confined user, so the root worker owns the capture directory
# to it before attaching the filter.
_QEMU_RUNTIME_USERS = ("qemu", "libvirt-qemu")
# libvirt's dynamic-ownership label for a QEMU-writable image; the confined ``svirt_t`` domain
# (with any MCS categories) can create files under an ``svirt_image_t:s0`` directory.
_SVIRT_IMAGE_CONTEXT = "system_u:object_r:svirt_image_t:s0"

# Operator guidance when the hypervisor could not write the capture pcap (ADR-0385). The root
# worker prepares the directory automatically (owns it to the QEMU user + labels it
# ``svirt_image_t``); a non-root worker cannot, so the directory must be provisioned out of band.
PCAP_HYPERVISOR_WRITE_REMEDIATION = (
    f"the qemu:///system hypervisor could not write the capture pcap under {_PCAP_DIR}/; ensure "
    "that directory is writable by the QEMU runtime user and, on SELinux hosts, labeled "
    "svirt_image_t. The root worker prepares it automatically; run the worker as root or "
    "provision the directory out of band"
)

# Shared operator guidance for the non-root-worker-under-qemu:///system readability wall
# (ADR-0223): virtlogd writes the console log and QEMU writes the host-dump core as root, so a
# non-root worker cannot read them back. The three fixes are all operator/deployment choices.
WORKER_READABILITY_REMEDIATION = (
    "the worker cannot read this root-owned file under qemu:///system; run the worker as root, "
    "set KDIVE_LIBVIRT_URI=qemu:///session (worker-owned QEMU), or grant the worker group read "
    "access to the libvirt/virtlogd output"
)

# The deterministic System domain name carries the owning System's UUID (ADR-0111). Anchored
# so the ephemeral build-VM form (kdive-build-<uuid>) cannot match: the "build-" infix is not
# hex, so it never satisfies the leading hex group. Hex is matched case-insensitively.
_SYSTEM_DOMAIN_RE = re.compile(
    r"^kdive-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def domain_name_for(system_id: UUID) -> str:
    return f"kdive-{system_id}"


def build_domain_name(build_id: UUID) -> str:
    """The transient customization-boot domain name for a build (ADR-0345).

    The ``kdive-build-`` infix keeps this ephemeral build VM out of the System name-fallback
    reaper: :func:`system_id_from_domain_name` returns ``None`` for this form (the ``build-``
    infix is not hex, so it never satisfies ``_SYSTEM_DOMAIN_RE``), so the reconciler never
    mistakes an in-flight build for a leaked System.
    """
    return f"kdive-build-{build_id}"


def system_id_from_domain_name(name: str) -> UUID | None:
    """The owning System UUID encoded in a ``kdive-<uuid>`` domain name, or ``None``.

    The inverse of :func:`domain_name_for`. Returns ``None`` for any name that is not a bare
    System domain — foreign names, the build-VM form ``kdive-build-<uuid>``, other prefixed
    forms, and anything not UUID-shaped — so a non-matching name is treated as unmanaged and
    never reaped by the reconciler's name-fallback path.
    """
    match = _SYSTEM_DOMAIN_RE.match(name)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:  # pragma: no cover - the regex already constrains the shape
        return None


def console_log_path(system_id: UUID) -> Path:
    return Path(_CONSOLE_DIR) / f"{system_id}.log"


def pcap_dir(system_id: UUID) -> Path:
    """The per-System host directory QEMU writes traffic captures into (ADR-0385)."""
    return Path(_PCAP_DIR) / str(system_id)


def pcap_path(system_id: UUID, job_id: UUID) -> Path:
    """The host pcap path for one capture job (``<pcap_dir>/<job_id>.pcap``)."""
    return pcap_dir(system_id) / f"{job_id}.pcap"


def _qemu_runtime_owner() -> tuple[int, int] | None:
    """Resolve the qemu:///system runtime user's (uid, gid), or ``None`` if no such user exists."""
    for name in _QEMU_RUNTIME_USERS:
        try:
            record = pwd.getpwnam(name)
        except KeyError:
            continue
        return record.pw_uid, record.pw_gid
    return None


def _relabel_svirt_image(directory: Path) -> None:
    """Best-effort SELinux relabel so the confined hypervisor can create files in ``directory``."""
    try:
        import selinux  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
    except ImportError:
        return
    try:
        if selinux.is_selinux_enabled():
            selinux.setfilecon(str(directory), _SVIRT_IMAGE_CONTEXT)
    except OSError as err:  # not root / policy denies the relabel — write-failure surfaces at read
        _log.debug("pcap dir SELinux relabel skipped for %s: %s", directory, err)


def prepare_pcap_dir(system_id: UUID) -> Path:
    """Create the per-System pcap dir and make it writable by the qemu:///system hypervisor.

    QEMU's ``filter-dump`` (added via raw QMP) writes the pcap as the unprivileged, SELinux-confined
    hypervisor user, but libvirt's dynamic ownership only relabels its own managed devices — not
    this out-of-band file. So the root worker prepares the directory: owns it to the QEMU runtime
    user and, on SELinux hosts, labels it ``svirt_image_t`` so the confined domain can create the
    pcap. Every step is best-effort (a non-root worker cannot chown/relabel); a genuine write
    failure is caught loudly at readback via :data:`PCAP_HYPERVISOR_WRITE_REMEDIATION`.
    """
    directory = pcap_dir(system_id)
    directory.mkdir(parents=True, exist_ok=True)
    owner = _qemu_runtime_owner()
    if owner is not None:
        with contextlib.suppress(OSError):
            os.chown(directory, *owner)
    with contextlib.suppress(OSError):
        directory.chmod(0o0770)
    _relabel_svirt_image(directory)
    return directory


def read_pcap_bytes(path: Path) -> bytes:
    """Read a captured pcap whole; absent captures are empty.

    Like the console log, a pcap written by QEMU under ``qemu:///system`` is root-owned, so a
    non-root worker may hit the ADR-0223 readback wall — categorized with the operator remedy.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except PermissionError as err:
        raise CategorizedError(
            "failed to read captured pcap",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "operation": "read_pcap",
                "path": str(path),
                "error": type(err).__name__,
                "remediation": WORKER_READABILITY_REMEDIATION,
            },
        ) from err
    except OSError as err:
        raise CategorizedError(
            "failed to read captured pcap",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"operation": "read_pcap", "path": str(path), "error": type(err).__name__},
        ) from err


def read_console_log(path: Path) -> bytes:
    """Read a System console log whole; absent logs are treated as empty.

    The local serial ``<log>`` is rendered ``append="off"`` (ADR-0258), so libvirt truncates it
    on every domain power-cycle: the file holds only the current boot. The whole file is therefore
    this Run's boot window, with no cross-boot slicing offset (ADR-0241's local byte offset was
    superseded — its stale prior-boot size dropped this boot's early-boot head off a truncated log,
    #836).
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except PermissionError as err:
        # A non-root worker under qemu:///system cannot read virtlogd's root:0600 console log
        # (ADR-0223). This never heals on retry — it is a host config problem, not transient
        # infrastructure — so categorize it as such and name the operator fix.
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "operation": "read_console_log",
                "path": str(path),
                "error": type(err).__name__,
                "remediation": WORKER_READABILITY_REMEDIATION,
            },
        ) from err
    except OSError as err:
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "operation": "read_console_log",
                "path": str(path),
                "error": type(err).__name__,
            },
        ) from err
