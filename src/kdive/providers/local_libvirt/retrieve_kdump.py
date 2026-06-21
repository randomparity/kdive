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
    path: str
    mtime: float
    size_bytes: int


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
) -> Path | None:
    """Stream the newest ``/var/crash/*/vmcore`` from ``overlay`` into ``dest``.

    Returns ``dest`` (now holding the core) or ``None`` when no core is present. ``dest`` is
    owned by the caller (see ``retrieve.py``); this function only writes into it, and never
    when the size cap rejects the core or no core exists.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the core exceeds ``max_bytes`` (checked
            from the ``statns`` size before any bytes are downloaded).
    """
    chosen = select_newest(reader.list_vmcores(overlay))
    if chosen is None:
        return None
    if chosen.size_bytes > max_bytes:
        raise CategorizedError(
            "kdump core exceeds the single-object ceiling",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"size_bytes": chosen.size_bytes, "max_bytes": max_bytes},
        )
    reader.download_vmcore(overlay, chosen.path, dest)
    return dest


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
