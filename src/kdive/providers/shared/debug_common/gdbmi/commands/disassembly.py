"""Disassembly command family for :mod:`kdive.providers.shared.debug_common.gdbmi`."""

from __future__ import annotations

import re
from typing import Any, Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import (
    GdbDisassembly,
    GdbInstruction,
    GdbMiAttachment,
)
from kdive.providers.shared.debug_common.gdbmi._errors import config_error
from kdive.providers.shared.debug_common.gdbmi.commands.symbols import _resolve_target
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import (
    MiRecord,
    disassembly_rows,
    mi_int,
)
from kdive.security.secrets.redaction import Redactor

MAX_DISASSEMBLE_INSTRUCTIONS = 256
MAX_INSTRUCTION_BYTES = 16
_NO_MEMORY_RE = re.compile(r"cannot access memory", re.IGNORECASE)


class _DisassemblyHost(Protocol):
    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def _redactor(self) -> Redactor: ...

    def resolve_symbol(self, attachment: GdbMiAttachment, name: str) -> int: ...


class GdbMiDisassemblyCommands:
    """Disassembly and symbol/address target resolution commands."""

    def disassemble(
        self: _DisassemblyHost,
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
            raise config_error(
                f"instruction_count must be between 1 and {MAX_DISASSEMBLE_INSTRUCTIONS}",
                code="bad_instruction_count",
                details={"instruction_count": instruction_count},
            )
        start = _resolve_target(attachment, self.resolve_symbol, symbol=symbol, address=address)
        no_instructions = CategorizedError(
            "gdb/MI returned no instructions",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"code": "no_instructions"},
        )
        rows = disassembly_rows(
            _disassemble_command(
                self, attachment, start, instruction_count, missing=no_instructions
            )
        )
        if not rows:
            raise no_instructions
        parsed = [_instruction_from(row) for row in rows]
        truncated = len(parsed) > instruction_count
        instructions = [_redact_instruction(self, insn) for insn in parsed[:instruction_count]]
        return GdbDisassembly(instructions=instructions, truncated=truncated)


def _disassemble_command(
    host: _DisassemblyHost,
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
            return host.execute_mi_command(attachment, command)
        except CategorizedError as exc:
            if not _is_unreadable_memory(exc):
                raise
            last_exc = exc
            if span <= MAX_INSTRUCTION_BYTES:
                raise missing from last_exc
            span = max(span // 2, MAX_INSTRUCTION_BYTES)


def _is_unreadable_memory(exc: CategorizedError) -> bool:
    if exc.category is not ErrorCategory.DEBUG_ATTACH_FAILURE:
        return False
    payload = exc.details.get("payload")
    msg = payload.get("msg") if isinstance(payload, dict) else None
    return isinstance(msg, str) and bool(_NO_MEMORY_RE.search(msg))


def _instruction_from(payload: dict[str, Any]) -> GdbInstruction:
    return GdbInstruction(
        address=payload.get("address") if isinstance(payload.get("address"), str) else None,
        inst=payload.get("inst") if isinstance(payload.get("inst"), str) else None,
        func_name=(payload.get("func-name") if isinstance(payload.get("func-name"), str) else None),
        offset=mi_int(payload.get("offset")),
    )


def _redact_instruction(host: _DisassemblyHost, instruction: GdbInstruction) -> GdbInstruction:
    return GdbInstruction.model_validate(
        host._redactor().redact_value(instruction.model_dump(mode="json"))
    )
