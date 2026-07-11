"""Protocol shared by GDB/MI command-family collaborators."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from kdive.domain.errors import CategorizedError
from kdive.providers.ports.debug import (
    GdbFrame,
    GdbInstruction,
    GdbMiAttachment,
    GdbModule,
    GdbWatchpointRef,
)
from kdive.providers.shared.debug_common.gdbmi.debuginfo import (
    ModuleDebuginfo,
    ModuleDebuginfoResolverSeam,
)
from kdive.providers.shared.debug_common.gdbmi.mi_protocol import MiRecord
from kdive.security.secrets.redaction import Redactor


class GdbMiCommandHost(Protocol):
    """Facade services used by extracted GDB/MI command families."""

    _module_resolver: ModuleDebuginfoResolverSeam

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def _redactor(self) -> Redactor: ...

    def resolve_symbol(self, attachment: GdbMiAttachment, name: str) -> int: ...

    def read_memory(
        self, attachment: GdbMiAttachment, *, address: int, byte_count: int
    ) -> bytes: ...

    def _mi_path(self, path: Path) -> str: ...

    def _resolve_target(
        self, attachment: GdbMiAttachment, *, symbol: str | None, address: int | None
    ) -> int: ...

    def _frame_from(self, payload: dict[str, Any]) -> GdbFrame: ...

    def _stack_command(
        self, attachment: GdbMiAttachment, command: str, *, missing: CategorizedError
    ) -> list[MiRecord]: ...

    def _redact_frame(self, frame: GdbFrame) -> GdbFrame: ...

    def _disassemble_command(
        self,
        attachment: GdbMiAttachment,
        start: int,
        instruction_count: int,
        *,
        missing: CategorizedError,
    ) -> list[MiRecord]: ...

    def _is_unreadable_memory(self, exc: CategorizedError) -> bool: ...

    def _instruction_from(self, payload: dict[str, Any]) -> GdbInstruction: ...

    def _redact_instruction(self, instruction: GdbInstruction) -> GdbInstruction: ...

    def _watchpoint_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def _watchpoint_ref(self, records: list[MiRecord]) -> GdbWatchpointRef: ...

    def _watchpoint_ref_from(self, entry: dict[str, Any]) -> GdbWatchpointRef: ...

    def _module_walk(
        self, attachment: GdbMiAttachment, *, limit: int
    ) -> tuple[list[tuple[str, int, int]], bool, int]: ...

    def _module_eval_required(self, attachment: GdbMiAttachment, expr: str) -> int: ...

    def _module_eval_optional(self, attachment: GdbMiAttachment, expr: str) -> int | None: ...

    def _module_name(self, attachment: GdbMiAttachment, module_ptr: int) -> str | None: ...

    def _module_base(
        self, attachment: GdbMiAttachment, module_ptr: int, base_field: str
    ) -> tuple[int | None, str]: ...

    def _module_walk_error(self, exc: CategorizedError) -> CategorizedError: ...

    def _redact_module(self, module: GdbModule) -> GdbModule: ...

    def _module_locate(
        self, attachment: GdbMiAttachment, module: str
    ) -> tuple[int, int] | None: ...

    def _verify_identity(
        self, attachment: GdbMiAttachment, module: str, module_ptr: int, info: ModuleDebuginfo
    ) -> bool: ...

    def _identity_match(self, module: str, ok: bool) -> bool: ...

    def _module_text_field(
        self, attachment: GdbMiAttachment, module_ptr: int, field: str
    ) -> str | None: ...

    def _module_build_id(self, attachment: GdbMiAttachment, module_ptr: int) -> str | None: ...

    def _add_symbol_file(
        self, attachment: GdbMiAttachment, module: str, path: Path, base: int
    ) -> None: ...
