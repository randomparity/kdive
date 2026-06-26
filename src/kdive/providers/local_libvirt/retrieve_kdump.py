"""Local-libvirt kdump capture: host-side overlay harvest (ADR-0203).

A local QEMU domain runs on the worker host, so its guest-written
``/var/crash/<ts>/vmcore`` lands on the per-System qcow2 overlay this host owns. The pure
helpers here select the newest core and enforce the single-object ceiling over an injected
``GuestCoreReader``, then stream the chosen core to a caller-owned temp file (#657) rather
than reading it whole into RAM; ``retrieve.py`` supplies the real libguestfs-backed reader
behind the ``live_vm`` gate.
"""

from __future__ import annotations

import base64
import hashlib
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.debug_common.core_file import DMESG_UNAVAILABLE
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

_SHA256_CHUNK_BYTES = 1024 * 1024


class VmcoreEntry(NamedTuple):
    """A ``/var/crash/<ts>`` core the reader saw.

    ``incomplete`` marks a ``vmcore-incomplete`` — kdump's name for a core it has not finished
    writing (its transient mid-save name, also left behind when ``makedumpfile`` aborts). Such a
    core is unreliable for crash/drgn and is never promoted to the harvested core.
    """

    path: str
    mtime: float
    size_bytes: int
    incomplete: bool = False


class HarvestOutcome(NamedTuple):
    """The result of a ``/var/crash`` harvest.

    ``core`` is the spooled complete ``vmcore`` (``dest``) when one was harvested, else ``None``.
    ``incomplete_found`` is ``True`` when the overlay held a ``vmcore-incomplete`` but no complete
    ``vmcore`` could be returned — the cause-neutral disclosure signal the caller maps to a
    ``READINESS_FAILURE``.
    """

    core: Path | None
    incomplete_found: bool


@runtime_checkable
class GuestCoreReader(Protocol):
    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]: ...
    def download_vmcore(self, overlay: str, path: str, dest: Path) -> None: ...


def select_newest(entries: list[VmcoreEntry]) -> VmcoreEntry | None:
    """The most recently written core (highest mtime), or ``None`` when none exist."""
    if not entries:
        return None
    return max(entries, key=lambda e: e.mtime)


def harvest_vmcore(
    reader: GuestCoreReader, overlay: str, *, dest: Path, max_bytes: int
) -> HarvestOutcome:
    """Stream the newest complete ``/var/crash/*/vmcore`` from ``overlay`` into ``dest``.

    Prefers a complete ``vmcore``; a ``vmcore-incomplete`` is never downloaded or returned (a
    truncated/unfiltered core is unreliable for crash/drgn). When no complete core exists the
    outcome carries ``incomplete_found`` so the caller can disclose an incomplete-core readiness
    failure separately from a genuinely empty ``/var/crash``. ``dest`` is owned by the caller
    (see ``retrieve.py``); this function writes into it only when a complete core is harvested.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the core exceeds ``max_bytes`` (checked
            from the ``statns`` size before any bytes are downloaded).
    """
    entries = reader.list_vmcores(overlay)
    incomplete_found = any(entry.incomplete for entry in entries)
    chosen = select_newest([entry for entry in entries if not entry.incomplete])
    if chosen is None:
        return HarvestOutcome(core=None, incomplete_found=incomplete_found)
    if chosen.size_bytes > max_bytes:
        raise CategorizedError(
            "kdump core exceeds the single-object ceiling",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"size_bytes": chosen.size_bytes, "max_bytes": max_bytes},
        )
    reader.download_vmcore(overlay, chosen.path, dest)
    return HarvestOutcome(core=dest, incomplete_found=incomplete_found)


def file_sha256_b64(path: Path) -> str:
    """Stream ``path`` through SHA-256 and return the base64 digest the object store signs."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_SHA256_CHUNK_BYTES), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def read_via_tempfile[T](data: bytes, path_reader: Callable[[Path], T]) -> T:
    """Spool ``data`` to a temp file so a Path-based drgn reader can open it; clean up after.

    Used by the crash-postmortem build-id check, which receives the core as bytes fetched
    from the object store (the kdump harvest path already has the core on disk).
    """
    with tempfile.NamedTemporaryFile(prefix="kdive-kdump-", suffix=".vmcore") as handle:
        handle.write(data)
        handle.flush()
        return path_reader(Path(handle.name))


def extract_dmesg_or_sentinel(core: Path, extractor: Callable[[Path], bytes]) -> bytes:
    """Extract dmesg from the core file; degrade to the sentinel, but never hide a missing drgn.

    Mirrors remote host_dump: a ``MISSING_DEPENDENCY`` (drgn absent) is an operator fault that
    must surface; any other failure (printk needs debuginfo) degrades to ``DMESG_UNAVAILABLE``
    so the core + build-id still get captured.
    """
    try:
        return extractor(core)
    except CategorizedError as exc:
        if exc.category is ErrorCategory.MISSING_DEPENDENCY:
            raise
        return DMESG_UNAVAILABLE


def redact_dmesg(core: Path, extractor: Callable[[Path], bytes], registry: SecretRegistry) -> bytes:
    """Extract dmesg (degrading on failure) and scrub registered secrets before persistence."""
    dmesg = extract_dmesg_or_sentinel(core, extractor)
    redacted = Redactor(registry=registry).redact_text(dmesg.decode("utf-8", "replace"))
    return redacted.encode("utf-8")
