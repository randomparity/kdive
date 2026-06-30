"""Fault-inject gdb/MI debug ports."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from kdive.providers.ports.debug import (
    GdbBacktrace,
    GdbBreakpointRef,
    GdbDisassembly,
    GdbFrame,
    GdbInstruction,
    GdbMiAttachment,
    GdbStopRecord,
    GdbWatchpointRef,
)


class _SyntheticGdbController:
    """A no-op gdb/MI controller for the synthetic attachment."""

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        return []

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        return []

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        return []

    def exit(self) -> None:
        return None


def fault_inject_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:
    return GdbMiAttachment(
        controller=_SyntheticGdbController(),
        rsp_host=host,
        rsp_port=port,
        transcript_path=transcript_path,
    )


class FaultInjectDebugEngine:
    """GdbMiEngine port: track breakpoints in-memory and return plausible records."""

    def __init__(self) -> None:
        self._breakpoints: dict[Path, dict[str, GdbBreakpointRef]] = {}
        self._watchpoints: dict[Path, dict[str, GdbWatchpointRef]] = {}
        self._next = 1
        self._lock = Lock()

    def set_breakpoint(self, attachment: GdbMiAttachment, location: str) -> GdbBreakpointRef:
        with self._lock:
            number = str(self._next)
            self._next += 1
            ref = GdbBreakpointRef(number=number, type="breakpoint", func=location, enabled=True)
            self._breakpoints.setdefault(attachment.transcript_path, {})[number] = ref
            return ref

    def clear_breakpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        with self._lock:
            bucket = self._breakpoints.get(attachment.transcript_path)
            if bucket is None:
                return
            bucket.pop(number, None)
            if not bucket:
                self._breakpoints.pop(attachment.transcript_path, None)

    def list_breakpoints(self, attachment: GdbMiAttachment) -> list[GdbBreakpointRef]:
        with self._lock:
            return list(self._breakpoints.get(attachment.transcript_path, {}).values())

    def read_memory(self, attachment: GdbMiAttachment, *, address: int, byte_count: int) -> bytes:
        return bytes(byte_count)

    def read_registers(
        self, attachment: GdbMiAttachment, register_names: list[str]
    ) -> dict[str, object]:
        return {name: 0 for name in register_names}

    def resolve_symbol(self, attachment: GdbMiAttachment, name: str) -> int:
        del attachment, name
        return 0xFFFFFFFF81000000

    def continue_(self, attachment: GdbMiAttachment, *, timeout_sec: float) -> GdbStopRecord:
        return GdbStopRecord(reason="breakpoint-hit", stopped_thread="1")

    def interrupt(self, attachment: GdbMiAttachment) -> GdbStopRecord | None:
        return GdbStopRecord(reason="signal-received", stopped_thread="1")

    def backtrace(self, attachment: GdbMiAttachment, *, max_frames: int = 64) -> GdbBacktrace:
        del attachment, max_frames
        return GdbBacktrace(
            frames=[
                GdbFrame(
                    level=0,
                    func="panic",
                    addr="0xffffffff81000000",
                    file="kernel/panic.c",
                    line=1,
                ),
                GdbFrame(level=1, func="do_exit", addr="0xffffffff81001000"),
            ],
            truncated=False,
        )

    def read_frame(self, attachment: GdbMiAttachment, *, level: int) -> GdbFrame:
        del attachment
        return GdbFrame(
            level=level, func="panic", addr="0xffffffff81000000", file="kernel/panic.c", line=1
        )

    def set_watchpoint(
        self,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None = None,
        address: int | None = None,
        byte_count: int = 8,
    ) -> GdbWatchpointRef:
        del symbol
        with self._lock:
            number = str(self._next)
            self._next += 1
            target = address if address is not None else 0xFFFFFFFF81000000
            ref = GdbWatchpointRef(
                number=number,
                type="hw watchpoint",
                expr=f"*(char(*)[{byte_count}])0x{target:x}",
                enabled=True,
            )
            self._watchpoints.setdefault(attachment.transcript_path, {})[number] = ref
            return ref

    def list_watchpoints(self, attachment: GdbMiAttachment) -> list[GdbWatchpointRef]:
        with self._lock:
            return list(self._watchpoints.get(attachment.transcript_path, {}).values())

    def clear_watchpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        with self._lock:
            bucket = self._watchpoints.get(attachment.transcript_path)
            if bucket is None:
                return
            bucket.pop(number, None)
            if not bucket:
                self._watchpoints.pop(attachment.transcript_path, None)

    def disassemble(
        self,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None = None,
        address: int | None = None,
        instruction_count: int = 64,
    ) -> GdbDisassembly:
        del attachment, symbol, address, instruction_count
        return GdbDisassembly(
            instructions=[
                GdbInstruction(
                    address="0xffffffff81000000", inst="push %rbp", func_name="panic", offset=0
                ),
                GdbInstruction(
                    address="0xffffffff81000001",
                    inst="mov %rsp,%rbp",
                    func_name="panic",
                    offset=1,
                ),
            ],
            truncated=False,
        )
