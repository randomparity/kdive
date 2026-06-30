"""The gdb-MI tier: a persistent ``gdb --interpreter=mi3`` engine over the gdbstub (ADR-0034).

The supported command surface is intentionally narrow: breakpoints (set/clear/list),
``read_registers``, ``read_memory`` with a 4096-byte cap, ``resolve_symbol`` (a gated
symbol→address lookup), ``continue_``, and ``interrupt``. ``resolve_symbol`` evaluates exactly
one form, ``&<identifier>`` (address-of-a-name, ADR-0248) — a narrowing, not a reversal, of the
"no expression evaluation" rule: general expression evaluation, module loading, stack walking,
and watchpoints remain outside this engine's contract.

All **textual** MI transcript/record output passes through the :class:`Redactor` before it is
persisted to the per-session transcript or returned in a response. The exception is
``read_memory``: the raw guest bytes are returned **verbatim** under the 4096 cap — the
redactor masks text/structure, and masking opaque binary memory would corrupt the requested
dump (ADR-0034 decision 3).

The ``GdbController`` subprocess seam is injectable: the real :class:`PygdbmiController` drives
a ``gdb`` child via pygdbmi (``live_vm``-only); tests inject a scripted fake. The live
:class:`GdbMiAttachment` objects are held in an in-process registry keyed on ``session_id`` —
server-process-scoped and non-durable (v1 ADR-0021): a restart strands the
attachment and the next op gets ``no_live_session``.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import (
    GdbBacktrace,
    GdbBreakpointRef,
    GdbController,
    GdbFrame,
    GdbMiAttachment,
    GdbStopRecord,
)
from kdive.providers.shared.debug_common import execution as mi_execution
from kdive.providers.shared.debug_common import mi_controller
from kdive.providers.shared.debug_common.execution import (
    MAX_INTERACTIVE_WAIT_SEC,
    ExecutionControl,
)
from kdive.providers.shared.debug_common.hostpolicy import HostPolicy, require_loopback
from kdive.providers.shared.debug_common.mi_controller import PygdbmiController
from kdive.providers.shared.debug_common.mi_protocol import (
    MiRecord,
    breakpoint_rows,
    evaluate_value,
    memory_segments,
    mi_int,
    parse_mi_records,
    payload_dict,
    register_values_by_number,
    result_payload_dict,
    stack_frames,
)
from kdive.providers.shared.debug_common.mi_protocol import (
    register_names as parsed_register_names,
)
from kdive.providers.shared.debug_common.transcript import (
    append_transcript as write_transcript,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

__all__ = [
    "GdbMiEngine",
    "MAX_INTERACTIVE_WAIT_SEC",
    "MAX_MEMORY_READ_BYTES",
    "MiRecord",
    "PygdbmiController",
    "parse_mi_records",
]

MAX_MEMORY_READ_BYTES = 4096
# Caps the backtrace *response* (not the gdb command): a free-running kernel stack is bounded,
# but the response stays bounded and a deeper stack is flagged via GdbBacktrace.truncated.
MAX_BACKTRACE_FRAMES = 64
_INTERRUPT_STOP_TIMEOUT_SEC = mi_execution.INTERRUPT_STOP_TIMEOUT_SEC
_STOP_POLL_SLICE_SEC = mi_execution.STOP_POLL_SLICE_SEC
_timeout_error = mi_controller.timeout_error

# Per-command MI write timeout. 10s bounds a healthy localhost RSP connect/read. The resume
# path uses ASYNC continue (mi-async on), so `-exec-continue` returns `^running` immediately
# rather than blocking until a stop a free-running kernel never produces.
_MI_COMMAND_TIMEOUT_SEC = 10.0
# gdb's RSP read timeout (`set remotetimeout`): generous-but-finite so a slow/silent stub
# yields a clean gdb-reported disconnect rather than an opaque hang.
RSP_REMOTE_TIMEOUT_SEC = 30
# Bounded retry for the RSP connect (`-target-select remote`): the connect is idempotent until
# `^connected`, so retrying any connect error a fixed small number of times is sound.
_CONNECT_RETRY_COUNT = 3
_CONNECT_RETRY_BACKOFF_SEC = 0.5

# A bare C identifier. The name-shape gate keeps a breakpoint location an address-of-a-name,
# never an arbitrary expression — so `-break-insert` is non-injectable.
_SYMBOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The first hex token in a `-data-evaluate-expression &name` value. gdb renders a pointer as
# `<optional type cast> 0xADDR <optional symbol>`, so a leftmost search yields the address even
# past a `(int *)` cast; the address always precedes the `<symbol>` annotation.
_SYMBOL_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
# A register name (passed to -data-list-register-names lookup).
_REGISTER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# A breakpoint location: a bare C identifier (function/symbol).
_BREAK_LOCATION_RE = _SYMBOL_NAME_RE
# A gdb breakpoint id is a bare integer.
_BREAK_ID_RE = re.compile(r"^[0-9]+$")
# gdb stop reasons meaning the inferior is gone (not a debuggable HALT).
_TERMINAL_STOP_REASONS = frozenset({"exited", "exited-normally", "exited-signalled"})
# gdb's ^error message for a stack command issued against a running target carries this token
# ("...while the target is running.", "Selected thread is running."); used to reclassify the
# generic command failure to the precise `inferior_running` code.
_RUNNING_RE = re.compile(r"running", re.IGNORECASE)
# gdb's ^error message when there is no unwindable stack ("No stack.") or the requested level is
# beyond stack depth ("No frame at level N."). Real gdb reports the out-of-range/empty case this
# way rather than an empty `^done,stack=[]`, so the missing-frame codes must catch both shapes.
_NO_STACK_RE = re.compile(r"no (stack|frame)", re.IGNORECASE)


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


class GdbMiEngine:
    """Persistent ``gdb --interpreter=mi3`` engine for the Debug-plane ops (ADR-0034, ADR-0248)."""

    def __init__(
        self,
        *,
        controller_factory: Callable[[list[str]], GdbController] | None = None,
        gdb_path_finder: Callable[[str], str | None] = shutil.which,
        redactor: Redactor | None = None,
        redactor_factory: Callable[[], Redactor] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        host_policy: HostPolicy = require_loopback,
    ) -> None:
        self._controller_factory = controller_factory or (
            lambda command: PygdbmiController(command)
        )
        self._gdb_path_finder = gdb_path_finder
        self._redactor_factory = _redactor_factory(redactor, redactor_factory)
        self._sleep = sleep
        self._host_policy = host_policy
        self._execution = ExecutionControl(self, command_timeout_sec=_MI_COMMAND_TIMEOUT_SEC)

    def _redactor(self) -> Redactor:
        return self._redactor_factory()

    # --- attach (live_vm) -----------------------------------------------------------------

    def attach(  # pragma: no cover - live_vm
        self, *, host: str, port: int, vmlinux_path: Path, transcript_path: Path
    ) -> GdbMiAttachment:
        """Spawn gdb, load symbols, and connect RSP. Live-only; tests inject a fake attachment."""
        self._host_policy(host)
        gdb_path = self._gdb_path_finder("gdb")
        if gdb_path is None:
            raise CategorizedError(
                "missing required gdb tool",
                category=ErrorCategory.MISSING_DEPENDENCY,
                details={"missing_tools": ["gdb"]},
            )
        resolved_vmlinux = vmlinux_path.expanduser().resolve()
        if not resolved_vmlinux.is_file():
            raise _config_error(
                "vmlinux symbol file does not exist",
                code="bad_vmlinux_path",
                details={"vmlinux_path": str(vmlinux_path)},
            )
        controller = self._controller_factory([gdb_path, "--nx", "--quiet", "--interpreter=mi3"])
        attachment = GdbMiAttachment(
            controller=controller, rsp_host=host, rsp_port=port, transcript_path=transcript_path
        )
        try:
            self.execute_mi_command(attachment, "-gdb-set confirm off")
            self.execute_mi_command(attachment, "-gdb-set pagination off")
            self.execute_mi_command(attachment, "-gdb-set mi-async on")
            self.execute_mi_command(
                attachment, f"-file-exec-and-symbols {self._mi_path(resolved_vmlinux)}"
            )
            self.execute_mi_command(attachment, f"-gdb-set remotetimeout {RSP_REMOTE_TIMEOUT_SEC}")
            self._connect_with_retry(attachment, host, port)
        except CategorizedError as exc:
            with contextlib.suppress(Exception):
                controller.exit()
            raise self._as_attach_failure(exc) from exc
        return attachment

    def _connect_with_retry(
        self, attachment: GdbMiAttachment, host: str, port: int
    ) -> None:  # pragma: no cover - live_vm
        command = f"-target-select remote {host}:{port}"
        last_exc: CategorizedError | None = None
        for attempt in range(_CONNECT_RETRY_COUNT):
            try:
                self.execute_mi_command(attachment, command)
                return
            except CategorizedError as exc:
                last_exc = self._as_attach_failure(exc)
                if attempt + 1 < _CONNECT_RETRY_COUNT:
                    self._sleep(_CONNECT_RETRY_BACKOFF_SEC)
        raise (
            last_exc
            if last_exc is not None
            else CategorizedError(
                "gdb/MI RSP connect failed", category=ErrorCategory.DEBUG_ATTACH_FAILURE
            )
        )

    def _as_attach_failure(
        self, exc: CategorizedError
    ) -> CategorizedError:  # pragma: no cover - live_vm
        if (
            exc.category is ErrorCategory.DEBUG_ATTACH_FAILURE
            and exc.details.get("code") != "transport_stall"
        ):
            return exc
        details = {key: value for key, value in exc.details.items() if key != "code"}
        return CategorizedError(
            str(exc), category=ErrorCategory.DEBUG_ATTACH_FAILURE, details=details
        )

    # --- breakpoints ----------------------------------------------------------------------

    def set_breakpoint(self, attachment: GdbMiAttachment, location: str) -> GdbBreakpointRef:
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

    def clear_breakpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        if not _BREAK_ID_RE.match(number):
            raise _config_error(
                f"breakpoint id must be numeric, got {number!r}",
                code="bad_breakpoint_id",
                details={"number": number},
            )
        self.execute_mi_command(attachment, f"-break-delete {number}")

    def list_breakpoints(self, attachment: GdbMiAttachment) -> list[GdbBreakpointRef]:
        return [
            self._breakpoint_ref_from(entry)
            for entry in breakpoint_rows(self.execute_mi_command(attachment, "-break-list"))
        ]

    def _breakpoint_ref(self, records: list[MiRecord], *, key: str) -> GdbBreakpointRef:
        payload = result_payload_dict(records)
        entry = payload.get(key)
        if not isinstance(entry, dict):
            raise CategorizedError(
                f"gdb/MI {key} response had no breakpoint record",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"command_key": key},
            )
        return self._breakpoint_ref_from(entry)

    def _breakpoint_ref_from(self, entry: dict[str, Any]) -> GdbBreakpointRef:
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

    # --- registers / memory ---------------------------------------------------------------

    def read_registers(
        self, attachment: GdbMiAttachment, register_names: list[str]
    ) -> dict[str, object]:
        if not isinstance(register_names, list) or not register_names:
            raise _config_error("registers must be a non-empty list", code="bad_register")
        requested: list[str] = []
        for name in register_names:
            if not isinstance(name, str) or not _REGISTER_RE.match(name):
                raise _config_error(f"invalid register name {name!r}", code="bad_register")
            requested.append(name)
        # gdb keys register VALUES by ordinal number; map names->ordinals via
        # -data-list-register-names, then return only the requested names.
        ordered_names = parsed_register_names(
            self.execute_mi_command(attachment, "-data-list-register-names")
        )
        by_number = register_values_by_number(
            self.execute_mi_command(attachment, "-data-list-register-values x")
        )
        registers: dict[str, object] = {}
        for name in requested:
            if name in ordered_names:
                ordinal = str(ordered_names.index(name))
                if ordinal in by_number:
                    registers[name] = by_number[ordinal]
        missing = [name for name in requested if name not in registers]
        if missing:
            raise CategorizedError(
                "gdb/MI omitted requested register data",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "missing_registers", "requested": requested, "missing": missing},
            )
        redacted = self._redactor().redact_value(registers)
        return redacted if isinstance(redacted, dict) else {}

    def read_memory(self, attachment: GdbMiAttachment, *, address: int, byte_count: int) -> bytes:
        """Read ``byte_count`` bytes from ``address``, returned **verbatim** (not redacted).

        Enforces the ported 4096-byte cap and a 64-bit address range. The gdb/MI
        ``-data-read-memory-bytes`` ``memory=[{contents:...}]`` segments are hex-decoded and
        concatenated; the raw bytes are returned unmasked (ADR-0034 decision 3). The transcript
        line for the command is still redacted (it is text).
        """
        if not isinstance(address, int) or not isinstance(byte_count, int):
            raise _config_error("address and byte_count must be integers", code="bad_read_range")
        if address < 0 or address > 0xFFFFFFFFFFFFFFFF:
            raise _config_error(
                "address out of range", code="bad_read_range", details={"address": address}
            )
        if byte_count < 1 or byte_count > MAX_MEMORY_READ_BYTES:
            raise _config_error(
                f"byte_count must be between 1 and {MAX_MEMORY_READ_BYTES}",
                code="bad_read_range",
                details={"byte_count": byte_count},
            )
        records = self.execute_mi_command(
            attachment, f"-data-read-memory-bytes 0x{address:x} {byte_count}"
        )
        segments = memory_segments(records)
        try:
            blob = b"".join(bytes.fromhex(str(seg.get("contents", ""))) for seg in segments)
        except ValueError as exc:
            # A non-hex / odd-length `contents` is a malformed stub reply, not a verbatim dump;
            # surface it as an attach-level failure rather than letting ValueError escape uncaught.
            raise CategorizedError(
                "gdb/MI returned non-hex memory contents",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "bad_memory_contents"},
            ) from exc
        if len(blob) != byte_count:
            raise CategorizedError(
                "gdb/MI returned fewer memory bytes than requested",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={
                    "code": "short_memory_read",
                    "address": address,
                    "requested": byte_count,
                    "actual": len(blob),
                },
            )
        return blob

    # --- symbol resolution ----------------------------------------------------------------

    def resolve_symbol(self, attachment: GdbMiAttachment, name: str) -> int:
        """Resolve a bare C symbol ``name`` to its address via ``-data-evaluate-expression``.

        The Run's DWARF ``vmlinux`` is already loaded at attach, so the symbol table is present.
        ``name`` is gated to a bare C identifier (``_SYMBOL_NAME_RE``) so the only expression
        ever evaluated is ``&<identifier>`` — address-of-a-name, never an arbitrary expression
        (non-injectable, the same property the breakpoint-location gate relies on). ``&name``
        resolves both data globals (e.g. ``d_hash_shift``) and functions, unlike
        ``-break-insert`` (a code location only). This is a symtab lookup, so it is valid whether
        or not the inferior is stopped.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_symbol_name`` for a non-identifier
                name (raised before any MI command); ``DEBUG_ATTACH_FAILURE`` for a gdb error
                (an unknown or addressless symbol — e.g. an enum/macro constant — yields a gdb
                ``^error`` mapped by ``execute_mi_command``) or a present-but-unparseable address
                value (``bad_symbol_value``, with the value redacted).
        """
        if not _SYMBOL_NAME_RE.match(name):
            raise _config_error(
                f"symbol name must be a bare C identifier, got {name!r}",
                code="bad_symbol_name",
                details={"name": name},
            )
        value = evaluate_value(
            self.execute_mi_command(attachment, f"-data-evaluate-expression &{name}")
        )
        match = _SYMBOL_ADDR_RE.search(value) if isinstance(value, str) else None
        if match is None:
            raise CategorizedError(
                "gdb/MI returned no parseable symbol address",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={
                    "code": "bad_symbol_value",
                    "name": name,
                    "value": self._redactor().redact_value(value),
                },
            )
        return int(match.group(0), 16)

    # --- stack walking (ADR-0275) ---------------------------------------------------------

    def backtrace(
        self, attachment: GdbMiAttachment, *, max_frames: int = MAX_BACKTRACE_FRAMES
    ) -> GdbBacktrace:
        """Walk the stopped inferior's stack, bounded to ``max_frames`` (ADR-0275).

        Issues ``-stack-list-frames`` unbounded (gdb returns the whole stack) and bounds the
        *response*: ``truncated`` is measured against the full depth before slicing to
        ``max_frames``. A running target's gdb ``^error`` is reclassified to ``inferior_running``;
        missing frame data — whether gdb answers ``^error,"No stack."`` or an empty
        ``^done,stack=[]`` — is ``no_frames``.
        """
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

    def read_frame(self, attachment: GdbMiAttachment, *, level: int) -> GdbFrame:
        """Inspect one selected stack frame by ``level`` (ADR-0275).

        ``level`` is gated only to a non-negative int; an out-of-range level is **not** a config
        error — gdb answers it as ``no_frame_at_level`` (whether via ``^error,"No frame at level
        N."`` or an empty ``^done``), so ``read_frame`` can reach a frame past the ``backtrace``
        response cap on a deep kernel stack.
        """
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
        self, attachment: GdbMiAttachment, command: str, *, missing: CategorizedError
    ) -> list[MiRecord]:
        """Run a stack MI command, mapping gdb ``^error`` to the precise debug codes.

        A running-target error becomes ``inferior_running``; gdb's "No stack."/"No frame at level
        N." (the real out-of-range/empty answer) becomes ``missing`` (the caller's ``no_frames`` /
        ``no_frame_at_level``); any other gdb error passes through unchanged.
        """
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

    def _redact_frame(self, frame: GdbFrame) -> GdbFrame:
        return GdbFrame.model_validate(self._redactor().redact_value(frame.model_dump(mode="json")))

    # --- interactive execution ------------------------------------------------------------

    def continue_(self, attachment: GdbMiAttachment, *, timeout_sec: float) -> GdbStopRecord:
        """Resume, wait for the stop, and interrupt back on timeout."""
        return self._execution.resume(attachment, "-exec-continue", timeout_sec=timeout_sec)

    def interrupt(self, attachment: GdbMiAttachment) -> GdbStopRecord | None:
        """Idempotent 'ensure HALTED': -exec-interrupt then wait the short fixed bound."""
        return self._execution.interrupt(attachment)

    def wait_for_stop(
        self, attachment: GdbMiAttachment, *, timeout_sec: float
    ) -> GdbStopRecord | None:
        return self._execution.wait_for_stop(attachment, timeout_sec=timeout_sec)

    # --- record helpers (public to _ExecutionControl) -------------------------------------

    def stop_record_from(self, record: MiRecord) -> GdbStopRecord:
        payload = payload_dict(record.payload)
        reason = payload.get("reason")
        if isinstance(reason, str) and reason in _TERMINAL_STOP_REASONS:
            raise CategorizedError(
                f"gdb/MI inferior exited ({reason}); the debug session is dead",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "session_exited", "reason": reason},
            )
        frame_payload = payload.get("frame")
        frame_payload = payload_dict(frame_payload)
        frame = self._frame_from(frame_payload) if frame_payload else None
        thread = payload.get("stopped-threads")
        return GdbStopRecord(
            reason=reason if isinstance(reason, str) else None,
            bkptno=payload.get("bkptno") if isinstance(payload.get("bkptno"), str) else None,
            stopped_thread=thread if isinstance(thread, str) else None,
            frame=frame,
        )

    def _frame_from(self, payload: dict[str, Any]) -> GdbFrame:
        return GdbFrame(
            level=mi_int(payload.get("level")),
            func=payload.get("func") if isinstance(payload.get("func"), str) else None,
            addr=payload.get("addr") if isinstance(payload.get("addr"), str) else None,
            file=payload.get("file") if isinstance(payload.get("file"), str) else None,
            line=mi_int(payload.get("line")),
        )

    def redact_stop(self, stop: GdbStopRecord) -> GdbStopRecord:
        return GdbStopRecord.model_validate(
            self._redactor().redact_value(stop.model_dump(mode="json"))
        )

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]:
        """Write one MI command, accumulate + transcribe its records, raise on ``^error``."""
        records = self.records_from(
            attachment.controller.write(command, timeout_sec=_MI_COMMAND_TIMEOUT_SEC)
        )
        attachment.records.extend(records)
        self.append_transcript(attachment.transcript_path, command, records)
        result = MiRecord.first_result(records)
        if result is not None and result.message == "error":
            raise CategorizedError(
                f"gdb/MI command failed: {command}",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={
                    "command": command,
                    "payload": self._redactor().redact_value(result.payload),
                },
            )
        return records

    def records_from(self, raw: list[dict[str, object]]) -> list[MiRecord]:
        return [MiRecord.from_raw(item) for item in raw]

    def _mi_path(self, path: Path) -> str:  # pragma: no cover - live_vm
        text = str(path)
        if any(char in text for char in "\t\r\n"):
            raise _config_error(
                "vmlinux path must not contain control whitespace", code="bad_vmlinux_path"
            )
        return text.replace("\\", "\\\\").replace(" ", "\\ ")

    def append_transcript(
        self, transcript_path: Path, command: str, records: list[MiRecord]
    ) -> None:
        write_transcript(
            transcript_path=transcript_path,
            command=command,
            records=records,
            redactor=self._redactor(),
        )


def _redactor_factory(
    redactor: Redactor | None, redactor_factory: Callable[[], Redactor] | None
) -> Callable[[], Redactor]:
    if redactor_factory is not None:
        return redactor_factory
    if redactor is not None:
        return lambda: redactor
    registry = SecretRegistry()
    return lambda: Redactor(registry=registry)
