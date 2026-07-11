"""Stack walking command family for :mod:`kdive.providers.shared.debug_common.gdbmi`."""

from __future__ import annotations

import re
from typing import Any, Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbBacktrace, GdbFrame, GdbMiAttachment
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import (
    MiRecord,
    mi_int,
    stack_frames,
)
from kdive.security.secrets.redaction import Redactor

MAX_BACKTRACE_FRAMES = 64
_RUNNING_RE = re.compile(r"running", re.IGNORECASE)
_NO_STACK_RE = re.compile(r"no (stack|frame)|not enough frames", re.IGNORECASE)


class _StackHost(Protocol):
    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def _redactor(self) -> Redactor: ...

    def _frame_from(self, payload: dict[str, Any]) -> GdbFrame: ...

    def _stack_command(
        self, attachment: GdbMiAttachment, command: str, *, missing: CategorizedError
    ) -> list[MiRecord]: ...

    def _redact_frame(self, frame: GdbFrame) -> GdbFrame: ...


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


class GdbMiStackCommands:
    """Stack/read-frame GDB/MI commands."""

    def backtrace(
        self: _StackHost,
        attachment: GdbMiAttachment,
        *,
        max_frames: int = MAX_BACKTRACE_FRAMES,
    ) -> GdbBacktrace:
        """Walk the stopped inferior's stack, bounded to ``max_frames`` (ADR-0275)."""
        if not isinstance(max_frames, int) or max_frames < 1 or max_frames > MAX_BACKTRACE_FRAMES:
            raise _config_error(
                f"max_frames must be between 1 and {MAX_BACKTRACE_FRAMES}",
                code="bad_frame_count",
                details={"max_frames": max_frames},
            )
        no_frames = CategorizedError(
            "gdb/MI returned no stack frames",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"code": "no_frames"},
        )
        rows = stack_frames(
            self._stack_command(attachment, "-stack-list-frames", missing=no_frames)
        )
        if not rows:
            raise no_frames
        parsed = [self._frame_from(row) for row in rows]
        truncated = len(parsed) > max_frames
        frames = [self._redact_frame(frame) for frame in parsed[:max_frames]]
        return GdbBacktrace(frames=frames, truncated=truncated)

    def read_frame(self: _StackHost, attachment: GdbMiAttachment, *, level: int) -> GdbFrame:
        """Inspect one selected stack frame by ``level`` (ADR-0275)."""
        if not isinstance(level, int) or level < 0:
            raise _config_error(
                f"frame level must be a non-negative integer, got {level!r}",
                code="bad_frame_level",
                details={"level": level},
            )
        no_frame = CategorizedError(
            "gdb/MI returned no frame at the requested level",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"code": "no_frame_at_level", "level": level},
        )
        rows = stack_frames(
            self._stack_command(attachment, f"-stack-list-frames {level} {level}", missing=no_frame)
        )
        if not rows:
            raise no_frame
        return self._redact_frame(self._frame_from(rows[0]))

    def _stack_command(
        self: _StackHost,
        attachment: GdbMiAttachment,
        command: str,
        *,
        missing: CategorizedError,
    ) -> list[MiRecord]:
        """Run a stack MI command, mapping gdb ``^error`` to precise debug codes."""
        try:
            return self.execute_mi_command(attachment, command)
        except CategorizedError as exc:
            if exc.category is ErrorCategory.DEBUG_ATTACH_FAILURE:
                payload = exc.details.get("payload")
                msg = payload.get("msg") if isinstance(payload, dict) else None
                if isinstance(msg, str):
                    if _RUNNING_RE.search(msg):
                        raise CategorizedError(
                            "gdb/MI cannot walk the stack while the inferior is running",
                            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                            details={"code": "inferior_running", "command": command},
                        ) from exc
                    if _NO_STACK_RE.search(msg):
                        raise missing from exc
            raise

    def _redact_frame(self: _StackHost, frame: GdbFrame) -> GdbFrame:
        return GdbFrame.model_validate(self._redactor().redact_value(frame.model_dump(mode="json")))

    def _frame_from(self, payload: dict[str, Any]) -> GdbFrame:
        return GdbFrame(
            level=mi_int(payload.get("level")),
            func=payload.get("func") if isinstance(payload.get("func"), str) else None,
            addr=payload.get("addr") if isinstance(payload.get("addr"), str) else None,
            file=payload.get("file") if isinstance(payload.get("file"), str) else None,
            line=mi_int(payload.get("line")),
        )
