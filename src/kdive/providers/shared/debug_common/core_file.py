"""Shared drgn vmcore-file helpers (ADR-0203, ADR-0857): build-id + dmesg from disk.

Used by both providers' Retrieve planes. Unit tests replace the native dependency;
valid-vmcore capture proofs remain live_vm-gated.
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


def open_core_program(core: Path) -> Any:
    try:
        import drgn  # noqa: PLC0415  # ty: ignore[import-not-found]  # operator-provided
    except ImportError as exc:
        raise CategorizedError(
            "drgn is not installed on this worker host; core build-id/dmesg needs it",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc
    try:
        prog = drgn.Program()  # ty: ignore[unresolved-attribute]  # optional native module
        prog.set_core_dump(os.fspath(core))
    except (OSError, ValueError, drgn.BadDataError, drgn.FaultError) as exc:
        raise CategorizedError(
            "could not open the core dump with drgn; verify that it is readable and valid",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
    return prog


def read_core_build_id_from_file(core: Path) -> str:
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


def read_core_dmesg_from_file(core: Path) -> bytes:
    """The kernel log buffer from an ELF/kdump core (drgn ``get_dmesg``)."""
    try:
        import drgn  # noqa: PLC0415  # ty: ignore[import-not-found]  # operator-provided
        from drgn.helpers.linux.printk import (  # noqa: PLC0415  # ty: ignore[import-not-found]
            get_dmesg,
        )
    except ImportError as exc:
        raise CategorizedError(
            "drgn printk helpers are unavailable on this worker host; reinstall the live "
            "dependencies to extract core dmesg",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc

    prog = open_core_program(core)
    try:
        return get_dmesg(prog)
    except (
        OSError,
        ValueError,
        LookupError,
        drgn.BadDataError,
        drgn.FaultError,
        drgn.MissingDebugInfoError,
        drgn.ObjectAbsentError,
        drgn.OutOfBoundsError,
    ) as exc:
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
