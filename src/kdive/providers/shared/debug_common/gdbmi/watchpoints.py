"""Watchpoint command family for :mod:`kdive.providers.shared.debug_common.gdbmi`."""

from __future__ import annotations

import re
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbMiAttachment, GdbWatchpointRef
from kdive.providers.shared.debug_common.gdbmi.host import GdbMiCommandHost
from kdive.providers.shared.debug_common.gdbmi.mi_protocol import (
    MiRecord,
    breakpoint_rows,
    result_payload_dict,
)

WATCH_BYTE_SIZES = (1, 2, 4, 8)
DEFAULT_WATCH_BYTES = 8
_BREAK_ID_RE = re.compile(r"^[0-9]+$")
_RUNNING_RE = re.compile(r"running", re.IGNORECASE)
_NO_WATCHPOINT_RE = re.compile(
    r"does not support\b.*watchpoint|cannot set hardware watchpoint", re.IGNORECASE
)


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


class GdbMiWatchpointCommands:
    """Hardware write-watchpoint GDB/MI commands."""

    def set_watchpoint(
        self: GdbMiCommandHost,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None = None,
        address: int | None = None,
        byte_count: int = DEFAULT_WATCH_BYTES,
    ) -> GdbWatchpointRef:
        """Set a hardware **write** watchpoint on a symbol/address window (ADR-0277)."""
        if not isinstance(byte_count, int) or byte_count not in WATCH_BYTE_SIZES:
            raise _config_error(
                f"byte_count must be one of {list(WATCH_BYTE_SIZES)}",
                code="bad_byte_count",
                details={"byte_count": byte_count, "supported": list(WATCH_BYTE_SIZES)},
            )
        start = self._resolve_target(attachment, symbol=symbol, address=address)
        expression = f"*(char(*)[{byte_count}])0x{start:x}"
        records = self._watchpoint_command(attachment, f"-break-watch {expression}")
        return self._watchpoint_ref(records)

    def list_watchpoints(
        self: GdbMiCommandHost, attachment: GdbMiAttachment
    ) -> list[GdbWatchpointRef]:
        """List watchpoints only (filtering out breakpoints) from ``-break-list`` (ADR-0277)."""
        return [
            self._watchpoint_ref_from(entry)
            for entry in breakpoint_rows(self.execute_mi_command(attachment, "-break-list"))
            if _is_watchpoint_row(entry)
        ]

    def clear_watchpoint(self: GdbMiCommandHost, attachment: GdbMiAttachment, number: str) -> None:
        """Delete a watchpoint by ``number`` via ``-break-delete`` (ADR-0277)."""
        if not _BREAK_ID_RE.match(number):
            raise _config_error(
                f"watchpoint id must be numeric, got {number!r}",
                code="bad_watchpoint_id",
                details={"number": number},
            )
        self.execute_mi_command(attachment, f"-break-delete {number}")

    def _watchpoint_command(
        self: GdbMiCommandHost, attachment: GdbMiAttachment, command: str
    ) -> list[MiRecord]:
        """Issue a watch command, classifying running-target then unsupported gdb ``^error``s."""
        try:
            return self.execute_mi_command(attachment, command)
        except CategorizedError as exc:
            if exc.category is ErrorCategory.DEBUG_ATTACH_FAILURE:
                payload = exc.details.get("payload")
                msg = payload.get("msg") if isinstance(payload, dict) else None
                if isinstance(msg, str):
                    if _RUNNING_RE.search(msg):
                        raise CategorizedError(
                            "gdb/MI cannot set the watchpoint while the inferior is running",
                            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                            details={"code": "inferior_running", "command": command},
                        ) from exc
                    if _NO_WATCHPOINT_RE.search(msg):
                        raise CategorizedError(
                            "gdb/MI target cannot support the requested watchpoint",
                            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                            details={"code": "watchpoint_unsupported", "command": command},
                        ) from exc
            raise

    def _watchpoint_ref(self: GdbMiCommandHost, records: list[MiRecord]) -> GdbWatchpointRef:
        entry = result_payload_dict(records).get("wpt")
        if not isinstance(entry, dict):
            raise CategorizedError(
                "gdb/MI -break-watch returned no watchpoint record",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "no_watchpoint_record"},
            )
        return self._watchpoint_ref_from(entry)

    def _watchpoint_ref_from(self: GdbMiCommandHost, entry: dict[str, Any]) -> GdbWatchpointRef:
        expression = entry.get("exp") if isinstance(entry.get("exp"), str) else None
        if expression is None and isinstance(entry.get("what"), str):
            expression = entry.get("what")
        enabled_raw = entry.get("enabled")
        enabled = enabled_raw == "y" if isinstance(enabled_raw, str) else None
        return GdbWatchpointRef.model_validate(
            self._redactor().redact_value(
                {
                    "number": str(entry.get("number")),
                    "type": entry.get("type") if isinstance(entry.get("type"), str) else None,
                    "expr": expression,
                    "addr": entry.get("addr") if isinstance(entry.get("addr"), str) else None,
                    "enabled": enabled,
                }
            )
        )


def _is_watchpoint_row(entry: dict[str, Any]) -> bool:
    kind = entry.get("type")
    return isinstance(kind, str) and "watchpoint" in kind.lower()
