"""Provider-runtime naming and host artifact path helpers."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory

_CONSOLE_DIR = "/var/lib/kdive/console"
_PCAP_DIR = "/var/lib/kdive/pcap"

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
    """The per-System host directory QEMU writes traffic captures into (ADR-0384)."""
    return Path(_PCAP_DIR) / str(system_id)


def pcap_path(system_id: UUID, job_id: UUID) -> Path:
    """The host pcap path for one capture job (``<pcap_dir>/<job_id>.pcap``)."""
    return pcap_dir(system_id) / f"{job_id}.pcap"


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
