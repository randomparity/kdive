"""Local-libvirt kdump capture: host-side overlay harvest (ADR-0203).

A local QEMU domain runs on the worker host, so its guest-written
``/var/crash/<ts>/vmcore`` lands on the per-System qcow2 overlay this host owns. The pure
helpers here select the newest core and enforce the single-object ceiling over an injected
``GuestCoreReader``; ``retrieve.py`` supplies the real libguestfs-backed reader behind the
``live_vm`` gate.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable

from kdive.domain.errors import CategorizedError, ErrorCategory


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
