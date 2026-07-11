"""Breakpoint command family for :mod:`kdive.providers.shared.debug_common.gdbmi`."""

from __future__ import annotations

import re
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbBreakpointRef, GdbMiAttachment
from kdive.providers.shared.debug_common.gdbmi.host import GdbMiCommandHost
from kdive.providers.shared.debug_common.gdbmi.mi_protocol import (
    MiRecord,
    breakpoint_rows,
    result_payload_dict,
)

_BREAK_LOCATION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BREAK_ID_RE = re.compile(r"^[0-9]+$")


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


class GdbMiBreakpointCommands:
    """Software breakpoint GDB/MI commands."""

    def set_breakpoint(
        self: GdbMiCommandHost, attachment: GdbMiAttachment, location: str
    ) -> GdbBreakpointRef:
        if not _BREAK_LOCATION_RE.match(location):
            raise _config_error(
                f"breakpoint location must be a bare C identifier, got {location!r}",
                code="bad_location",
                details={"location": location},
            )
        # Software breakpoint (no -h): QEMU's gdbstub honors a software breakpoint's 0xCC write
        # on a running guest, but does not reliably trap on hardware (debug-register) breakpoints,
        # so a `-break-insert -h` at a hot symbol could go `^running` and never `*stopped` (#711).
        return self._breakpoint_ref(
            self.execute_mi_command(attachment, f"-break-insert {location}"), key="bkpt"
        )

    def clear_breakpoint(self: GdbMiCommandHost, attachment: GdbMiAttachment, number: str) -> None:
        if not _BREAK_ID_RE.match(number):
            raise _config_error(
                f"breakpoint id must be numeric, got {number!r}",
                code="bad_breakpoint_id",
                details={"number": number},
            )
        self.execute_mi_command(attachment, f"-break-delete {number}")

    def list_breakpoints(
        self: GdbMiCommandHost, attachment: GdbMiAttachment
    ) -> list[GdbBreakpointRef]:
        return [
            self._breakpoint_ref_from(entry)
            for entry in breakpoint_rows(self.execute_mi_command(attachment, "-break-list"))
        ]

    def _breakpoint_ref(
        self: GdbMiCommandHost, records: list[MiRecord], *, key: str
    ) -> GdbBreakpointRef:
        payload = result_payload_dict(records)
        entry = payload.get(key)
        if not isinstance(entry, dict):
            raise CategorizedError(
                f"gdb/MI {key} response had no breakpoint record",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"command_key": key},
            )
        return self._breakpoint_ref_from(entry)

    def _breakpoint_ref_from(self: GdbMiCommandHost, entry: dict[str, Any]) -> GdbBreakpointRef:
        return GdbBreakpointRef.model_validate(
            self._redactor().redact_value(
                {
                    "number": str(entry.get("number")),
                    "type": entry.get("type") if isinstance(entry.get("type"), str) else None,
                    "addr": entry.get("addr") if isinstance(entry.get("addr"), str) else None,
                    "func": entry.get("func") if isinstance(entry.get("func"), str) else None,
                    "what": entry.get("what") if isinstance(entry.get("what"), str) else None,
                }
            )
        )
