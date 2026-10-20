"""Interactive execution helpers for the local-libvirt gdb/MI provider."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import (
    GdbHalt,
    GdbMiAttachment,
    GdbStopRecord,
    HaltCause,
)
from kdive.providers.shared.debug_common.gdbmi.commands.watchpoints import (
    watchpoint_insert_failure,
)
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import MiRecord

MAX_INTERACTIVE_WAIT_SEC = 60
STOP_POLL_SLICE_SEC = 0.5
INTERRUPT_STOP_TIMEOUT_SEC = 10.0
_CONTINUE = "-exec-continue"


class GdbMiEngineHandle(Protocol):
    """Engine surface used by execution control without importing the engine class."""

    def records_from(self, raw: list[dict[str, object]]) -> list[MiRecord]: ...

    def append_transcript(
        self, transcript_path: Path, command: str, records: list[MiRecord]
    ) -> None: ...

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def stop_record_from(self, record: MiRecord) -> GdbStopRecord: ...

    def redact_stop(self, stop: GdbStopRecord) -> GdbStopRecord: ...


def inferior_running(command: str | None = None) -> CategorizedError:
    """The refusal for a command that needs a halted guest while a run is in progress."""
    details: dict[str, object] = {"code": "inferior_running"}
    if command is not None:
        details["command"] = command
    return CategorizedError(
        "the guest is running; halt it with debug.interrupt or wait for its stop with "
        "debug.continue",
        category=ErrorCategory.DEBUG_ATTACH_FAILURE,
        details=details,
    )


def _transport_stall(message: str, verb: str) -> CategorizedError:
    return CategorizedError(
        message,
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"code": "transport_stall", "verb": verb},
    )


def _halt_cause(stop: GdbStopRecord) -> HaltCause:
    """Attribute a stop that ended a run: only a SIGINT signal stop is the interrupt's own."""
    if stop.reason == "signal-received" and stop.signal_name == "SIGINT":
        return "interrupt"
    return "stop_event"


class ExecutionControl:
    """Resume/wait/interrupt machinery for the interactive ops.

    Owns ``attachment.execution`` (#3244): a resume sets ``running`` before it is written, every
    observed ``*stopped`` sets ``halted``, and a resume gdb refuses restores ``halted``. Any
    other failure after a resume or interrupt was written leaves ``running``.
    """

    def __init__(self, engine: GdbMiEngineHandle, *, command_timeout_sec: float) -> None:
        self._engine = engine
        self._command_timeout_sec = command_timeout_sec

    def _stop_from_records(
        self, attachment: GdbMiAttachment, records: list[MiRecord]
    ) -> GdbStopRecord | None:
        """The parsed stop for the first ``*stopped`` record, or None if none is present."""
        stop = next((record for record in records if record.message == "stopped"), None)
        if stop is None:
            return None
        attachment.execution = "halted"
        return self._engine.stop_record_from(stop)

    def wait_for_stop(
        self, attachment: GdbMiAttachment, *, timeout_sec: float
    ) -> GdbStopRecord | None:
        slices = max(1, int(timeout_sec / STOP_POLL_SLICE_SEC) + 1)
        for _ in range(slices):
            records = self._engine.records_from(
                attachment.controller.read(timeout_sec=STOP_POLL_SLICE_SEC)
            )
            attachment.records.extend(records)
            if records:
                self._engine.append_transcript(attachment.transcript_path, "<read>", records)
            stop = self._stop_from_records(attachment, records)
            if stop is not None:
                return stop
        return None

    def _interrupt_stop(self, attachment: GdbMiAttachment) -> GdbStopRecord | None:
        raw = attachment.controller.write("-exec-interrupt", timeout_sec=self._command_timeout_sec)
        records = self._engine.records_from(raw)
        attachment.records.extend(records)
        self._engine.append_transcript(attachment.transcript_path, "-exec-interrupt", records)
        stop = self._stop_from_records(attachment, records)
        if stop is None:
            result = MiRecord.first_result(records)
            if result is not None and result.message == "error":
                # gdb refused (its target is gone, say): no stop will follow, so do not wait
                # out the stop bound and call it a stalled link. The state stays `running`.
                raise CategorizedError(
                    "gdb/MI refused the interrupt; end the session",
                    category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                    details={"code": "interrupt_refused", "command": "-exec-interrupt"},
                )
            stop = self.wait_for_stop(attachment, timeout_sec=INTERRUPT_STOP_TIMEOUT_SEC)
        return self._engine.redact_stop(stop) if stop is not None else None

    def interrupt(self, attachment: GdbMiAttachment) -> GdbHalt:
        """Halt a running guest and attribute the halt; a halted guest gets no command."""
        if attachment.execution == "attach_halted":
            attachment.execution = "halted"
            return GdbHalt(halted_by="attach")
        if attachment.execution == "halted":
            return GdbHalt(halted_by="already_halted")
        stop = self._interrupt_stop(attachment)
        if stop is None:
            raise _transport_stall(
                "gdb/MI RSP went silent: interrupt issued but no *stopped arrived; link stalled",
                "-exec-interrupt",
            )
        return GdbHalt(halted_by=_halt_cause(stop), stop=stop)

    def _start(self, attachment: GdbMiAttachment, verb: str) -> GdbStopRecord | None:
        """Write a resume verb; return a stop its own reply carried (ADR-0216, #711)."""
        attachment.execution = "running"
        try:
            resumed = self._engine.execute_mi_command(attachment, verb)
        except CategorizedError as exc:
            if exc.category is ErrorCategory.DEBUG_ATTACH_FAILURE:
                # gdb answered ^error, so the target never resumed.
                attachment.execution = "halted"
            insert_failure = watchpoint_insert_failure(exc, verb)
            if insert_failure is None:
                raise
            raise insert_failure from exc
        return self._stop_from_records(attachment, resumed)

    def resume_background(self, attachment: GdbMiAttachment) -> GdbStopRecord | None:
        """Resume without waiting; a guest already running gets no command."""
        if attachment.execution == "running":
            return None
        stop = self._start(attachment, _CONTINUE)
        return self._engine.redact_stop(stop) if stop is not None else None

    def resume(
        self, attachment: GdbMiAttachment, verb: str, *, timeout_sec: float
    ) -> GdbStopRecord:
        if timeout_sec < 0 or not math.isfinite(timeout_sec):
            raise CategorizedError(
                "gdb/MI resume timeout must be a finite non-negative number",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"code": "bad_resume_timeout", "timeout_sec": timeout_sec},
            )
        requested = math.ceil(timeout_sec) if timeout_sec else MAX_INTERACTIVE_WAIT_SEC
        bounded = max(1, min(requested, MAX_INTERACTIVE_WAIT_SEC))
        # The continue command's own reader can capture an early ``*stopped`` (a hot-path
        # breakpoint fires within milliseconds) alongside ``^running``. Scan those records
        # first; only poll the stream afresh when they hold no stop (ADR-0216, #711). A guest
        # already running from a background continue is waited on, not resumed again.
        if attachment.execution != "running":
            stop = self._start(attachment, verb)
        elif verb == _CONTINUE:
            stop = None
        else:
            raise inferior_running(verb)
        if stop is None:
            stop = self.wait_for_stop(attachment, timeout_sec=bounded)
        if stop is not None:
            return self._engine.redact_stop(stop)
        interrupted = self._interrupt_stop(attachment)
        if interrupted is None:
            raise _transport_stall(
                "gdb/MI RSP went silent: interrupt issued but no *stopped arrived; link stalled",
                verb,
            )
        return self._engine.redact_stop(interrupted.model_copy(update={"timed_out": True}))
