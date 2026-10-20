"""Stack walking command family for :mod:`kdive.providers.shared.debug_common.gdbmi`."""

from __future__ import annotations

import re
from typing import Any, Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import (
    GdbBacktrace,
    GdbFrame,
    GdbFrameDetail,
    GdbMiAttachment,
    GdbVariable,
)
from kdive.providers.shared.debug_common.gdbmi._errors import config_error
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import (
    MiRecord,
    frame_variables,
    mi_int,
    result_payload_dict,
    stack_frames,
)
from kdive.security.secrets.redaction import Redactor

MAX_BACKTRACE_FRAMES = 64
MAX_FRAME_VARIABLES = 64
MAX_VARIABLE_TEXT = 256
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

    def _current_thread(self, attachment: GdbMiAttachment) -> str: ...


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
            raise config_error(
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

    def read_frame(self: _StackHost, attachment: GdbMiAttachment, *, level: int) -> GdbFrameDetail:
        """Inspect one selected stack frame by ``level`` with its variables (ADR-0275, 0781)."""
        if not isinstance(level, int) or level < 0:
            raise config_error(
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
        thread = self._current_thread(attachment)
        records = self._stack_command(
            attachment,
            f"-stack-list-variables --thread {thread} --frame {level} --simple-values",
            missing=no_frame,
        )
        variables = frame_variables(records)
        if variables is None:
            raise CategorizedError(
                "gdb/MI returned no variables for the frame",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "no_frame_variables", "level": level},
            )
        named = [row for row in variables if isinstance(row.get("name"), str)]
        detail = GdbFrameDetail(
            **self._frame_from(rows[0]).model_dump(),
            args=[_variable(row) for row in named if row.get("arg") == "1"],
            locals=[_variable(row) for row in named if row.get("arg") != "1"],
        )
        redacted = self._redactor().redact_value(detail.model_dump(mode="json"))
        return _bounded(GdbFrameDetail.model_validate(redacted))

    def _current_thread(self: _StackHost, attachment: GdbMiAttachment) -> str:
        records = self.execute_mi_command(attachment, "-thread-list-ids")
        thread = result_payload_dict(records).get("current-thread-id")
        if not isinstance(thread, str) or not thread.isdigit():
            raise CategorizedError(
                "gdb/MI reported no current thread",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "no_current_thread"},
            )
        return thread

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


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _variable(row: dict[str, Any]) -> GdbVariable:
    return GdbVariable(name=row["name"], type=_text(row.get("type")), value=_text(row.get("value")))


def _cut(text: str | None) -> str | None:
    return text[:MAX_VARIABLE_TEXT] if text is not None else None


def _bounded(detail: GdbFrameDetail) -> GdbFrameDetail:
    """Keep the first 64 variables, arguments first, each type and value cut to 256 chars."""
    args = detail.args[:MAX_FRAME_VARIABLES]
    kept = [*args, *detail.locals[: MAX_FRAME_VARIABLES - len(args)]]
    cut = [v.model_copy(update={"type": _cut(v.type), "value": _cut(v.value)}) for v in kept]
    truncated = cut != [*detail.args, *detail.locals]
    return detail.model_copy(
        update={"args": cut[: len(args)], "locals": cut[len(args) :], "truncated": truncated}
    )
