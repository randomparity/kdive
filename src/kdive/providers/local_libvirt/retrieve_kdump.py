"""Local-libvirt kdump capture: host-side overlay harvest (ADR-0203).

A local QEMU domain runs on the worker host, so its guest-written
``/var/crash/<ts>/vmcore`` lands on the per-System qcow2 overlay this host owns. The pure
helpers here select the newest core and enforce the single-object ceiling over an injected
``GuestCoreReader``; ``retrieve.py`` supplies the real libguestfs-backed reader behind the
``live_vm`` gate.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.debug_common.core_file import DMESG_UNAVAILABLE
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry


class VmcoreEntry(NamedTuple):
    path: str
    mtime: float
    size_bytes: int


@runtime_checkable
class GuestCoreReader(Protocol):
    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]: ...
    def read_vmcore(self, overlay: str, path: str) -> bytes: ...


def select_newest(entries: list[VmcoreEntry]) -> VmcoreEntry | None:
    """The most recently written core (highest mtime), or ``None`` when none exist."""
    if not entries:
        return None
    return max(entries, key=lambda e: e.mtime)


def harvest_vmcore(reader: GuestCoreReader, overlay: str, *, max_bytes: int) -> bytes | None:
    """Read the newest ``/var/crash/*/vmcore`` from ``overlay``; ``None`` if none present.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the core exceeds ``max_bytes``.
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
    return reader.read_vmcore(overlay, chosen.path)


def read_via_tempfile[T](data: bytes, path_reader: Callable[[Path], T]) -> T:
    """Spool ``data`` to a temp file so a Path-based drgn reader can open it; clean up after."""
    with tempfile.NamedTemporaryFile(prefix="kdive-kdump-", suffix=".vmcore") as handle:
        handle.write(data)
        handle.flush()
        return path_reader(Path(handle.name))


def extract_dmesg_or_sentinel(data: bytes, extractor: Callable[[Path], bytes]) -> bytes:
    """Extract dmesg from the core bytes; degrade to the sentinel, but never hide a missing drgn.

    Mirrors remote host_dump: a ``MISSING_DEPENDENCY`` (drgn absent) is an operator fault that
    must surface; any other failure (printk needs debuginfo) degrades to ``DMESG_UNAVAILABLE``
    so the core + build-id still get captured.
    """
    try:
        return read_via_tempfile(data, extractor)
    except CategorizedError as exc:
        if exc.category is ErrorCategory.MISSING_DEPENDENCY:
            raise
        return DMESG_UNAVAILABLE


def redact_dmesg(
    data: bytes, extractor: Callable[[Path], bytes], registry: SecretRegistry
) -> bytes:
    """Extract dmesg (degrading on failure) and scrub registered secrets before persistence."""
    dmesg = extract_dmesg_or_sentinel(data, extractor)
    redacted = Redactor(registry=registry).redact_text(dmesg.decode("utf-8", "replace"))
    return redacted.encode("utf-8")
