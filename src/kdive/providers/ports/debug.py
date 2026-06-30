"""Debug provider contracts and gdb/MI records."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from kdive.providers.ports._common import ProviderModel


class GdbFrame(ProviderModel):
    """One stack frame from a gdb/MI stop record."""

    level: int | None = None
    func: str | None = None
    addr: str | None = None
    file: str | None = None
    line: int | None = None


class GdbStopRecord(ProviderModel):
    """A parsed gdb/MI stop record."""

    reason: str | None = None
    bkptno: str | None = None
    stopped_thread: str | None = None
    frame: GdbFrame | None = None
    timed_out: bool = False


class GdbBacktrace(ProviderModel):
    """A bounded, parsed gdb/MI stack backtrace."""

    frames: list[GdbFrame]
    truncated: bool = False


class GdbInstruction(ProviderModel):
    """One disassembled instruction from a gdb/MI ``-data-disassemble`` result."""

    address: str | None = None
    inst: str | None = None
    func_name: str | None = None
    offset: int | None = None


class GdbDisassembly(ProviderModel):
    """A bounded, parsed gdb/MI disassembly window."""

    instructions: list[GdbInstruction]
    truncated: bool = False


class GdbBreakpointRef(ProviderModel):
    """One gdb/MI breakpoint reference."""

    number: str
    type: str | None = None
    addr: str | None = None
    func: str | None = None
    what: str | None = None
    enabled: bool | None = None


class GdbWatchpointRef(ProviderModel):
    """One gdb/MI watchpoint reference."""

    number: str
    type: str | None = None
    expr: str | None = None
    addr: str | None = None
    enabled: bool | None = None


class GdbModule(ProviderModel):
    """One loaded kernel module from a live gdbstub session (ADR-0278)."""

    name: str | None = None
    base_address: str | None = None
    symbols_loaded: bool | None = None
    identity_verified: bool | None = None


class GdbModuleList(ProviderModel):
    """A bounded, parsed list of loaded kernel modules (ADR-0278)."""

    modules: list[GdbModule]
    truncated: bool = False
    decode_errors: int = 0


class GdbController(Protocol):
    """Controller operations a gdb/MI attachment exposes."""

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        """Write one gdb/MI command and return records emitted before the prompt.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` when the prompt does not arrive
                before ``timeout_sec``. Callers that interpret error records should surface
                gdb/MI command failures as ``DEBUG_ATTACH_FAILURE``.
        """
        ...

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        """Read pending gdb/MI records without sending a command.

        Unlike :meth:`write` and :meth:`get_gdb_response`, this is the non-raising sibling:
        a read timeout is non-fatal and returns an empty list, so polling callers can
        distinguish "no records yet" from a command timeout without catching an error.
        """
        ...

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        """Read records until the prompt or timeout.

        Args:
            timeout_sec: Maximum wait for a prompt.
            raise_error_on_timeout: If true, timeout raises; if false, timeout returns
                an empty list.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` when ``raise_error_on_timeout``
                is true and no prompt arrives before ``timeout_sec``.
        """
        ...

    def exit(self) -> None:
        """Terminate the underlying gdb/MI controller.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` if the controller cannot be
                terminated cleanly.
        """
        ...


@dataclass
class GdbMiAttachment:
    """A live gdb/MI attachment plus endpoint and transcript metadata."""

    controller: GdbController
    rsp_host: str
    rsp_port: int
    transcript_path: Path
    records: list[object] = field(default_factory=list)
    run_id: str = ""
    loaded_modules: set[str] = field(default_factory=set)


class GdbMiEngine(Protocol):
    """Debug operation engine over a live gdb/MI attachment."""

    def set_breakpoint(self, attachment: GdbMiAttachment, location: str) -> GdbBreakpointRef:
        """Set a breakpoint through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid locations,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures, or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def clear_breakpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        """Clear a breakpoint through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid breakpoint numbers,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures, or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def list_breakpoints(self, attachment: GdbMiAttachment) -> list[GdbBreakpointRef]:
        """List breakpoints through gdb/MI.

        Raises:
            CategorizedError: ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def read_memory(self, attachment: GdbMiAttachment, *, address: int, byte_count: int) -> bytes:
        """Read guest memory through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid address/count values,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI read failures, or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def read_registers(
        self, attachment: GdbMiAttachment, register_names: list[str]
    ) -> dict[str, object]:
        """Read selected registers through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for empty or invalid register names,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI read failures, or ``INFRASTRUCTURE_FAILURE``
                for command timeouts.
        """
        ...

    def resolve_symbol(self, attachment: GdbMiAttachment, name: str) -> int:
        """Resolve a bare C symbol name to its address through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a non-identifier name,
                ``DEBUG_ATTACH_FAILURE`` for a gdb error or an unparseable address value, or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def continue_(self, attachment: GdbMiAttachment, *, timeout_sec: float) -> GdbStopRecord:
        """Resume execution and return a stop record.

        If the requested wait times out, the provider interrupts execution and returns the
        resulting stop with ``timed_out=True``. If no stop arrives after the interrupt, the
        provider raises instead of returning ``None``.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid timeout values,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures, or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def interrupt(self, attachment: GdbMiAttachment) -> GdbStopRecord | None:
        """Interrupt execution and return the stop record when one is reported.

        Raises:
            CategorizedError: ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def backtrace(self, attachment: GdbMiAttachment, *, max_frames: int) -> GdbBacktrace:
        """Walk the stopped inferior's stack through gdb/MI, bounded to ``max_frames``.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_frame_count`` for an out-of-range
                ``max_frames`` (raised before any MI command); ``DEBUG_ATTACH_FAILURE`` /
                ``inferior_running`` when the target is running, ``no_frames`` when gdb returns
                no usable frame data, or for other gdb/MI command failures;
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def read_frame(self, attachment: GdbMiAttachment, *, level: int) -> GdbFrame:
        """Inspect one selected stack frame by ``level`` through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_frame_level`` for a negative or
                non-integer ``level`` (raised before any MI command); ``DEBUG_ATTACH_FAILURE`` /
                ``inferior_running`` when the target is running, ``no_frame_at_level`` when no
                frame exists at ``level``, or for other gdb/MI command failures;
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def disassemble(
        self,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None,
        address: int | None,
        instruction_count: int,
    ) -> GdbDisassembly:
        """Disassemble a bounded forward instruction window around a symbol or address.

        Exactly one of ``symbol`` / ``address`` must be given. Bounds the response to
        ``instruction_count`` (slicing an oversized range), with ``truncated`` when more
        instructions follow.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_instruction_count`` for an
                out-of-range count, ``bad_target`` when not exactly one of symbol/address is
                given, ``bad_address`` for an out-of-range address, ``bad_symbol_name`` (via
                ``resolve_symbol``) for a non-identifier name (all raised before the disassemble
                command); ``DEBUG_ATTACH_FAILURE`` / ``no_instructions`` when gdb returns no
                usable instruction data or the address is unreadable; ``INFRASTRUCTURE_FAILURE``
                for command timeouts.
        """
        ...

    def set_watchpoint(
        self,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None,
        address: int | None,
        byte_count: int,
    ) -> GdbWatchpointRef:
        """Set a hardware **write** watchpoint on a bare symbol or explicit address.

        Exactly one of ``symbol`` / ``address`` must be given; ``byte_count`` must be one of
        ``{1, 2, 4, 8}``. The watch expression is constructed from the resolved numeric address,
        never a caller expression.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_byte_count`` for an unsupported size,
                ``bad_target`` when not exactly one of symbol/address is given, ``bad_address`` for
                an out-of-range address, ``bad_symbol_name`` (via ``resolve_symbol``) for a
                non-identifier name (all before any MI command); ``DEBUG_ATTACH_FAILURE`` /
                ``inferior_running`` when the target is running, ``watchpoint_unsupported`` when the
                target refuses the watchpoint at set time, ``no_watchpoint_record`` for a malformed
                ``-break-watch`` result, or for other gdb/MI command failures;
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def list_watchpoints(self, attachment: GdbMiAttachment) -> list[GdbWatchpointRef]:
        """List watchpoints (only watchpoints, not breakpoints) through gdb/MI.

        Raises:
            CategorizedError: ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def clear_watchpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        """Clear a watchpoint by number through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_watchpoint_id`` for a non-numeric id,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures, or ``INFRASTRUCTURE_FAILURE``
                for command timeouts.
        """
        ...

    def list_modules(self, attachment: GdbMiAttachment, *, max_modules: int) -> GdbModuleList:
        """List loaded kernel modules by walking the ``modules`` list (ADR-0278).

        Walks the kernel module list via internally-constructed expressions (never caller text),
        bounded to ``max_modules`` (``truncated`` when more follow). A single undecodable row is
        skipped and counted in ``decode_errors``; ``symbols_loaded`` reflects what this session
        loaded.

        Raises:
            CategorizedError: ``DEBUG_ATTACH_FAILURE`` / ``inferior_running`` when the target is
                running, ``module_decode_failed`` when the list head or base-address field cannot
                be read; ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...


class AttachSeam(Protocol):
    """Lazy attach seam returning a live gdb/MI attachment."""

    def __call__(
        self, *, host: str, port: int, run_id: str, transcript_path: Path
    ) -> GdbMiAttachment: ...
