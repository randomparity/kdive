"""Breakpoint command family for :mod:`kdive.providers.shared.debug_common.gdbmi`."""

from __future__ import annotations

import re
from typing import Any, Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbBreakpointRef, GdbMiAttachment
from kdive.providers.shared.debug_common.gdbmi._errors import config_error
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import (
    MiRecord,
    breakpoint_rows,
    result_payload_dict,
)
from kdive.security.secrets.redaction import Redactor

_BREAK_LOCATION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BREAK_ID_RE = re.compile(r"^[0-9]+$")


class _BreakpointHost(Protocol):
    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def _redactor(self) -> Redactor: ...


class GdbMiBreakpointCommands:
    """Software breakpoint GDB/MI commands."""

    def set_breakpoint(
        self: _BreakpointHost, attachment: GdbMiAttachment, location: str
    ) -> GdbBreakpointRef:
        if not _BREAK_LOCATION_RE.match(location):
            raise config_error(
                f"breakpoint location must be a bare C identifier, got {location!r}",
                code="bad_location",
                details={"location": location},
            )
        # Software breakpoint (no -h): QEMU's gdbstub honors a software breakpoint's 0xCC write
        # on a running guest, but does not reliably trap on hardware (debug-register) breakpoints,
        # so a `-break-insert -h` at a hot symbol could go `^running` and never `*stopped` (#711).
        return _breakpoint_ref(
            self, self.execute_mi_command(attachment, f"-break-insert {location}"), key="bkpt"
        )

    def clear_breakpoint(self: _BreakpointHost, attachment: GdbMiAttachment, number: str) -> None:
        if not _BREAK_ID_RE.match(number):
            raise config_error(
                f"breakpoint id must be numeric, got {number!r}",
                code="bad_breakpoint_id",
                details={"number": number},
            )
        self.execute_mi_command(attachment, f"-break-delete {number}")

    def list_breakpoints(
        self: _BreakpointHost, attachment: GdbMiAttachment
    ) -> list[GdbBreakpointRef]:
        return [
            _breakpoint_ref_from(self, entry)
            for entry in breakpoint_rows(self.execute_mi_command(attachment, "-break-list"))
        ]


def _breakpoint_ref(
    host: _BreakpointHost, records: list[MiRecord], *, key: str
) -> GdbBreakpointRef:
    payload = result_payload_dict(records)
    entry = payload.get(key)
    if not isinstance(entry, dict):
        raise CategorizedError(
            f"gdb/MI {key} response had no breakpoint record",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"command_key": key},
        )
    return _breakpoint_ref_from(host, entry)


def _breakpoint_ref_from(host: _BreakpointHost, entry: dict[str, Any]) -> GdbBreakpointRef:
    number = entry.get("number")
    if not isinstance(number, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", number) is None:
        raise CategorizedError(
            "gdb/MI breakpoint response has no usable number; "
            "inspect debugger state before retrying",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
        )
    return GdbBreakpointRef.model_validate(
        host._redactor().redact_value(
            {
                "number": number,
                "type": entry.get("type") if isinstance(entry.get("type"), str) else None,
                "addr": entry.get("addr") if isinstance(entry.get("addr"), str) else None,
                "func": entry.get("func") if isinstance(entry.get("func"), str) else None,
                "what": entry.get("what") if isinstance(entry.get("what"), str) else None,
            }
        )
    )
