"""Shared drgn vmcore-file helpers (ADR-0203): build-id + dmesg from a core on disk.

Used by both providers' Retrieve planes. The drgn calls are live_vm-gated; the surrounding
provider code injects these as seams so the orchestration is unit-tested with fakes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory

MAX_CORE_BYTES = 5 * 1024**3

DMESG_UNAVAILABLE = (
    b"[kdive] dmesg could not be extracted from this core "
    b"(kernel debuginfo required); see the crash postmortem for the kernel log\n"
)


def open_core_program(core: Path) -> Any:  # pragma: no cover - live_vm (drgn)
    try:
        import drgn  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
    except ImportError as exc:
        raise CategorizedError(
            "drgn is not installed on this worker host; core build-id/dmesg needs it",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc
    prog = drgn.Program()
    prog.set_core_dump(os.fspath(core))
    return prog


def read_core_build_id_from_file(core: Path) -> str:  # pragma: no cover - live_vm (drgn)
    """The crashed kernel's GNU build-id from a compressed-kdump core's VMCOREINFO."""
    prog = open_core_program(core)
    vmcoreinfo = bytes(prog["VMCOREINFO"].value_())
    match = re.search(rb"BUILD-ID=([0-9a-f]{40})", vmcoreinfo)
    if match is None:
        raise CategorizedError(
            "core carries no VMCOREINFO BUILD-ID line; cannot verify provenance",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return match.group(1).decode("ascii")


def read_core_dmesg_from_file(core: Path) -> bytes:  # pragma: no cover - live_vm (drgn)
    """The kernel log buffer from an ELF/kdump core (drgn ``get_dmesg``)."""
    from drgn.helpers.linux.printk import (  # noqa: PLC0415  # ty: ignore[unresolved-import]
        get_dmesg,
    )

    prog = open_core_program(core)
    try:
        return get_dmesg(prog)
    except Exception as exc:
        raise CategorizedError(
            "could not extract dmesg from the core; the printk ring buffer needs the "
            "guest kernel's debuginfo, which is not loaded at capture time",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc


__all__ = [
    "DMESG_UNAVAILABLE",
    "MAX_CORE_BYTES",
    "open_core_program",
    "read_core_build_id_from_file",
    "read_core_dmesg_from_file",
]
