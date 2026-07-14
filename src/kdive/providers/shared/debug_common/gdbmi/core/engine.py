"""The gdb-MI tier: a persistent ``gdb --interpreter=mi3`` engine over the gdbstub (ADR-0034).

The supported command surface is intentionally narrow: breakpoints (set/clear/list),
``read_registers``, ``read_memory`` with a 4096-byte cap, ``resolve_symbol`` (a gated
symbol→address lookup), ``continue_``, and ``interrupt``. ``resolve_symbol`` evaluates exactly
one form, ``&<identifier>`` (address-of-a-name, ADR-0248) — a narrowing, not a reversal, of the
"no expression evaluation" rule. Stack walking (ADR-0275), disassembly (ADR-0276), write
watchpoints (ADR-0277), and module-symbol loading (ADR-0278) are also in-contract; general
expression evaluation remains outside this engine's contract.

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
import platform
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import (
    GdbController,
    GdbMiAttachment,
    GdbStopRecord,
)
from kdive.providers.shared.debug_common.gdbmi.commands.breakpoints import GdbMiBreakpointCommands
from kdive.providers.shared.debug_common.gdbmi.commands.disassembly import GdbMiDisassemblyCommands
from kdive.providers.shared.debug_common.gdbmi.commands.modules import GdbMiModuleCommands
from kdive.providers.shared.debug_common.gdbmi.commands.registers import GdbMiRegisterCommands
from kdive.providers.shared.debug_common.gdbmi.commands.stack import GdbMiStackCommands
from kdive.providers.shared.debug_common.gdbmi.commands.symbols import GdbMiSymbolCommands
from kdive.providers.shared.debug_common.gdbmi.commands.watchpoints import GdbMiWatchpointCommands
from kdive.providers.shared.debug_common.gdbmi.core import execution as mi_execution
from kdive.providers.shared.debug_common.gdbmi.core import mi_controller
from kdive.providers.shared.debug_common.gdbmi.core.execution import (
    MAX_INTERACTIVE_WAIT_SEC,
    ExecutionControl,
)
from kdive.providers.shared.debug_common.gdbmi.core.mi_controller import PygdbmiController
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import (
    MiRecord,
    memory_segments,
    parse_mi_records,
    payload_dict,
)
from kdive.providers.shared.debug_common.gdbmi.core.transcript import (
    append_transcript as write_transcript,
)
from kdive.providers.shared.debug_common.gdbmi.policy.arch import (
    arch_from_elf,
    gdb_target_arch_name,
    select_gdb_binary,
)
from kdive.providers.shared.debug_common.gdbmi.policy.debuginfo import (
    ModuleDebuginfo,
    ModuleDebuginfoResolverSeam,
)
from kdive.providers.shared.debug_common.gdbmi.policy.hostpolicy import HostPolicy, require_loopback
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

# gdb stop reasons meaning the inferior is gone (not a debuggable HALT).
_TERMINAL_STOP_REASONS = frozenset({"exited", "exited-normally", "exited-signalled"})


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


class GdbMiEngine(
    GdbMiBreakpointCommands,
    GdbMiRegisterCommands,
    GdbMiSymbolCommands,
    GdbMiStackCommands,
    GdbMiDisassemblyCommands,
    GdbMiWatchpointCommands,
    GdbMiModuleCommands,
):
    """Persistent ``gdb --interpreter=mi3`` engine for the Debug-plane ops (ADR-0034, ADR-0248)."""

    def __init__(
        self,
        *,
        controller_factory: Callable[[list[str]], GdbController] | None = None,
        gdb_path_finder: Callable[[str], str | None] = shutil.which,
        host_arch_finder: Callable[[], str] = platform.machine,
        redactor: Redactor | None = None,
        redactor_factory: Callable[[], Redactor] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        host_policy: HostPolicy = require_loopback,
        module_debuginfo_resolver: ModuleDebuginfoResolverSeam | None = None,
    ) -> None:
        self._controller_factory = controller_factory or (
            lambda command: PygdbmiController(command)
        )
        self._gdb_path_finder = gdb_path_finder
        self._host_arch_finder = host_arch_finder
        self._redactor_factory = _redactor_factory(redactor, redactor_factory)
        self._sleep = sleep
        self._host_policy = host_policy
        self._module_resolver = module_debuginfo_resolver or _missing_module_resolver
        self._execution = ExecutionControl(self, command_timeout_sec=_MI_COMMAND_TIMEOUT_SEC)

    def _redactor(self) -> Redactor:
        return self._redactor_factory()

    @staticmethod
    def _missing_gdb_error(*, is_cross_arch: bool, guest_arch: str | None) -> CategorizedError:
        """Build the ``MISSING_DEPENDENCY`` error for an unresolvable gdb binary.

        A cross-arch attach names the multiarch prerequisite so the fix is actionable; a native
        attach keeps the original "missing required gdb" contract.
        """
        if is_cross_arch:
            return CategorizedError(
                f"missing a multiarch-capable gdb for a {guest_arch} guest on a non-{guest_arch} "
                "host; install gdb-multiarch (Debian/Ubuntu) or a multiarch gdb build",
                category=ErrorCategory.MISSING_DEPENDENCY,
                details={"missing_tools": ["gdb-multiarch", "gdb"], "guest_arch": guest_arch},
            )
        return CategorizedError(
            "missing required gdb tool",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"missing_tools": ["gdb"]},
        )

    # --- attach (live_vm) -----------------------------------------------------------------

    def attach(  # pragma: no cover - live_vm
        self, *, host: str, port: int, vmlinux_path: Path, transcript_path: Path, run_id: str = ""
    ) -> GdbMiAttachment:
        """Spawn gdb, load symbols, and connect RSP. Live-only; tests inject a fake attachment.

        ``run_id`` is carried on the attachment so ``load_module_symbols`` can resolve the Run's
        module ``.ko`` via the injected resolver (ADR-0278).
        """
        self._host_policy(host)
        resolved_vmlinux = vmlinux_path.expanduser().resolve()
        if not resolved_vmlinux.is_file():
            raise _config_error(
                "vmlinux symbol file does not exist",
                code="bad_vmlinux_path",
                details={"vmlinux_path": str(vmlinux_path)},
            )
        guest_arch = arch_from_elf(resolved_vmlinux)
        host_arch = self._host_arch_finder()
        is_cross_arch = guest_arch is not None and guest_arch != host_arch
        gdb_path = select_gdb_binary(host_arch, guest_arch, self._gdb_path_finder)
        if gdb_path is None:
            raise self._missing_gdb_error(is_cross_arch=is_cross_arch, guest_arch=guest_arch)
        controller = self._controller_factory([gdb_path, "--nx", "--quiet", "--interpreter=mi3"])
        attachment = GdbMiAttachment(
            controller=controller,
            rsp_host=host,
            rsp_port=port,
            transcript_path=transcript_path,
            run_id=run_id,
        )
        try:
            self.execute_mi_command(attachment, "-gdb-set confirm off")
            self.execute_mi_command(attachment, "-gdb-set pagination off")
            self.execute_mi_command(attachment, "-gdb-set mi-async on")
            self.execute_mi_command(
                attachment, f"-file-exec-and-symbols {self._mi_path(resolved_vmlinux)}"
            )
            if is_cross_arch:
                gdb_arch = gdb_target_arch_name(guest_arch)  # type: ignore[arg-type]
                if gdb_arch is not None:
                    self.execute_mi_command(attachment, f"-gdb-set architecture {gdb_arch}")
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

    # --- memory ---------------------------------------------------------------------------

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


def _missing_module_resolver(run_id: str, module: str) -> ModuleDebuginfo:
    """Default ``module_debuginfo_resolver``: none wired (mirrors the attach-seam default)."""
    raise CategorizedError(
        "no module-debuginfo resolver is configured for this engine",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"missing_tools": ["module_debuginfo_resolver"], "module": module},
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
