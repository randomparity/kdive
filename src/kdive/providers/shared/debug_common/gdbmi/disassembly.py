"""Disassembly command family for :mod:`kdive.providers.shared.debug_common.gdbmi`."""

from __future__ import annotations

import re
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import (
    GdbDisassembly,
    GdbInstruction,
    GdbMiAttachment,
)
from kdive.providers.shared.debug_common.gdbmi.host import GdbMiCommandHost
from kdive.providers.shared.debug_common.gdbmi.mi_protocol import (
    MiRecord,
    disassembly_rows,
    mi_int,
)

MAX_DISASSEMBLE_INSTRUCTIONS = 256
MAX_INSTRUCTION_BYTES = 16
_NO_MEMORY_RE = re.compile(r"cannot access memory", re.IGNORECASE)


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


class GdbMiDisassemblyCommands:
    """Disassembly and symbol/address target resolution commands."""

    def disassemble(
        self: GdbMiCommandHost,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None = None,
        address: int | None = None,
        instruction_count: int = 64,
    ) -> GdbDisassembly:
        """Disassemble a bounded forward window around ``symbol`` or ``address`` (ADR-0276)."""
        if (
            not isinstance(instruction_count, int)
            or instruction_count < 1
            or instruction_count > MAX_DISASSEMBLE_INSTRUCTIONS
        ):
            raise _config_error(
                f"instruction_count must be between 1 and {MAX_DISASSEMBLE_INSTRUCTIONS}",
                code="bad_instruction_count",
                details={"instruction_count": instruction_count},
            )
        start = self._resolve_target(attachment, symbol=symbol, address=address)
        no_instructions = CategorizedError(
            "gdb/MI returned no instructions",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"code": "no_instructions"},
        )
        rows = disassembly_rows(
            self._disassemble_command(attachment, start, instruction_count, missing=no_instructions)
        )
        if not rows:
            raise no_instructions
        parsed = [self._instruction_from(row) for row in rows]
        truncated = len(parsed) > instruction_count
        instructions = [self._redact_instruction(insn) for insn in parsed[:instruction_count]]
        return GdbDisassembly(instructions=instructions, truncated=truncated)

    def _resolve_target(
        self: GdbMiCommandHost,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None,
        address: int | None,
    ) -> int:
        """Resolve exactly one of ``symbol`` / ``address`` to a numeric address."""
        has_symbol = symbol is not None
        has_address = address is not None
        if has_symbol == has_address:
            raise _config_error(
                "exactly one of symbol or address is required",
                code="bad_target",
                details={"symbol": symbol, "address": address},
            )
        if symbol is not None:
            return self.resolve_symbol(attachment, symbol)
        if not isinstance(address, int) or address < 0 or address > 0xFFFFFFFFFFFFFFFF:
            raise _config_error(
                "address out of range", code="bad_address", details={"address": address}
            )
        return address

    def _disassemble_command(
        self: GdbMiCommandHost,
        attachment: GdbMiAttachment,
        start: int,
        instruction_count: int,
        *,
        missing: CategorizedError,
    ) -> list[MiRecord]:
        """Issue the range disassemble, shrinking the window on a memory-access ``^error``."""
        span = instruction_count * MAX_INSTRUCTION_BYTES
        last_exc: CategorizedError | None = None
        while True:
            command = f"-data-disassemble -s 0x{start:x} -e 0x{start + span:x} -- 0"
            try:
                return self.execute_mi_command(attachment, command)
            except CategorizedError as exc:
                if not self._is_unreadable_memory(exc):
                    raise
                last_exc = exc
                if span <= MAX_INSTRUCTION_BYTES:
                    raise missing from last_exc
                span = max(span // 2, MAX_INSTRUCTION_BYTES)

    def _is_unreadable_memory(self, exc: CategorizedError) -> bool:
        if exc.category is not ErrorCategory.DEBUG_ATTACH_FAILURE:
            return False
        payload = exc.details.get("payload")
        msg = payload.get("msg") if isinstance(payload, dict) else None
        return isinstance(msg, str) and bool(_NO_MEMORY_RE.search(msg))

    def _instruction_from(self, payload: dict[str, Any]) -> GdbInstruction:
        return GdbInstruction(
            address=payload.get("address") if isinstance(payload.get("address"), str) else None,
            inst=payload.get("inst") if isinstance(payload.get("inst"), str) else None,
            func_name=(
                payload.get("func-name") if isinstance(payload.get("func-name"), str) else None
            ),
            offset=mi_int(payload.get("offset")),
        )

    def _redact_instruction(self: GdbMiCommandHost, instruction: GdbInstruction) -> GdbInstruction:
        return GdbInstruction.model_validate(
            self._redactor().redact_value(instruction.model_dump(mode="json"))
        )
