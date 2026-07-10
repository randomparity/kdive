"""The Debug-plane gdb-MI tools — `debug.set_breakpoint/.clear/.list`, `.read_memory`,
`.read_registers`, `.resolve_symbol`, `.continue`, `.interrupt`, `.backtrace`, `.read_frame`,
`.disassemble`, `.set_watchpoint/.list_watchpoints/.clear_watchpoint`,
`.list_modules/.load_module_symbols`
(ADR-0034, ADR-0248, ADR-0275, ADR-0276, ADR-0277, ADR-0278).

These extend the `debug.*` session lifecycle tools registered by ``sessions.py``. A `live`
`DebugSession` records an open single-attach gdbstub transport; the first Debug-plane op for
a session lazily spawns a gdb/MI engine over the session's RSP endpoint, cached in a
process-scoped
:class:`DebugEngineRuntime` (registry + per-session ``asyncio.Lock`` table + the
``live_vm``-gated attach seam). Every op is gated (contributor + project + ``live`` state), takes
the per-session lock, attaches-or-reuses, and runs the blocking engine call via
``asyncio.to_thread`` so a long `continue` never stalls the event loop.

Textual transcript/record output is redacted by the engine before persistence/response; raw
`read_memory` bytes are returned **verbatim** under the 4096 cap (rendered as hex in
``data["memory_hex"]``) — the cap is the memory control, redaction is the transcript-text
control, and they are independent (ADR-0034 §3/§6).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.config.core_settings import DEBUG_DIR
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import DebugSession
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.debug.session_context import (
    resolve_debug_session_context,
)
from kdive.mcp.tools.debug.session_registry import GdbMiSessionRegistry
from kdive.providers.core.resolver import ProviderBinding, ProviderResolver
from kdive.providers.ports.debug import (
    AttachSeam,
    GdbMiAttachment,
    GdbMiEngine,
)
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.providers.shared.debug_common.gdbmi import MAX_MEMORY_READ_BYTES, MAX_MODULES
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.serialization import JsonValue

_EngineOp = Callable[[GdbMiEngine, GdbMiAttachment], ToolResponse]


@dataclass(frozen=True)
class _OpAudit:
    """The attribution to record for one Debug-plane op (ADR-0006/0028)."""

    tool: str
    transition: str
    args: Mapping[str, object]


# The Debug-plane ops that write an audit_log row on success: state-mutating engine ops
# (breakpoints/watchpoints/continue/interrupt/symbol-load) and the sensitive raw-memory read.
# Bounded pure reads (list_*, read_registers, resolve_symbol, backtrace, read_frame,
# disassemble) are not audited — the session attach/detach rows plus the per-session transcript
# already attribute them.
_AUDITED_OPS: frozenset[str] = frozenset(
    {
        "debug.read_memory",
        "debug.set_breakpoint",
        "debug.clear_breakpoint",
        "debug.set_watchpoint",
        "debug.clear_watchpoint",
        "debug.continue",
        "debug.interrupt",
        "debug.load_module_symbols",
    }
)


def _op_audit(tool: str, **args: object) -> _OpAudit | None:
    """Return the audit descriptor for an audited ``tool``, or None to skip auditing.

    ``transition`` is the bare op name (``tool`` sans the ``debug.`` prefix); ``args`` are the
    op parameters recorded for ``args_digest`` correlation (never raw memory bytes).
    """
    if tool not in _AUDITED_OPS:
        return None
    return _OpAudit(tool=tool, transition=tool.removeprefix("debug."), args=args)


def _gdbmi_maturity() -> dict[str, object]:
    """The shared ADR-0175 maturity for the gdb-MI `debug.*` tools.

    The seven original ops act over a live gdbstub-backed DebugSession whose full round-trip
    (set_breakpoint -> continue -> read_registers) was proven live on real KVM (M2.8 B6 #680),
    so they are ``implemented``. ``backtrace`` and ``read_frame`` (ADR-0275, PR#929) and
    ``disassemble`` (ADR-0276, PR#932) ride that same transport and were each re-proven live
    against a stopped ``schedule``, so they are in ``_LOCAL_PROVEN_DEBUG_TOOLS`` too.
    ``resolve_symbol`` (ADR-0248) is unit-tested against the scripted controller only — its
    ``-data-evaluate-expression`` form was not separately re-proven live — so it stays out of the
    proven set until a live exercise lands. The three watchpoint ops (ADR-0277) were proven live on
    real KVM against a stopped ``schedule`` (set on ``jiffies`` + an explicit address, list, clear,
    and a ``continue`` that trapped on the watched write in ``tick_do_update_jiffies64``), so they
    join ``_LOCAL_PROVEN_DEBUG_TOOLS``. ``list_modules`` and ``load_module_symbols`` (ADR-0278)
    were proven live against a real kernel with a loaded ``.ko`` (walk found the module at its
    ``mem[0].base``; ``add-symbol-file`` made a previously-unknown module symbol resolvable), so
    they join the proven set too.
    """
    return _docmeta.maturity_meta("implemented")


def _default_transcript_dir() -> Path:
    # Configurable (KDIVE_DEBUG_DIR) so a deployment points it at the run-artifact tree and
    # tests at a temp dir; the registry default mirrors the other planes' /var/lib/kdive/* roots.
    return Path(config.require(DEBUG_DIR))


class DebugEngineRuntime:
    """Process-scoped holder for the lazy gdb-MI engines + per-session locks (ADR-0034 §4a).

    Owns the in-process :class:`GdbMiSessionRegistry`, a per-session ``asyncio.Lock`` table (the
    get-or-create guarded by a plain :class:`threading.Lock`), and the injected
    :class:`AttachSeam`. One instance is built in ``debug.register`` and shared by every
    Debug-plane handler (and by `end_session`'s reap).
    """

    def __init__(
        self, *, engine: GdbMiEngine, attach: AttachSeam, transcript_dir: Path | None = None
    ) -> None:
        self._engine = engine
        self._attach = attach
        self._transcript_dir = (
            transcript_dir if transcript_dir is not None else _default_transcript_dir()
        )
        self._registry = GdbMiSessionRegistry()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()

    @property
    def engine(self) -> GdbMiEngine:
        return self._engine

    def lock_for(self, session_id: str) -> asyncio.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, asyncio.Lock())

    def attach_or_reuse(self, session: DebugSession) -> GdbMiAttachment:
        """Return the live attachment for ``session``.

        A registry miss opens the provider attachment and registers it for later ops on the same
        debug session.
        """
        session_id = str(session.id)
        existing = self._registry.get(session_id)
        if existing is not None:
            return existing
        endpoint = TransportHandleData.decode(session.transport_handle or "")
        attachment = self._attach(
            host=endpoint.host,
            port=endpoint.port,
            run_id=str(session.run_id),
            transcript_path=self._transcript_dir / f"{session_id}.jsonl",
        )
        self._registry.register(session_id, attachment)
        return attachment

    def reap(self, session_id: str) -> None:
        """Exit + drop the live engine for ``session_id`` (no-op if never attached)."""
        attachment = self._registry.reap(session_id)
        if attachment is not None:
            with contextlib.suppress(Exception):
                attachment.controller.exit()
        with self._locks_guard:
            self._locks.pop(session_id, None)


class DebugRuntimeResolver:
    """Provider-aware cache of per-provider debug engine runtimes."""

    def __init__(self, resolver: ProviderResolver, *, transcript_dir: Path | None = None) -> None:
        self._resolver = resolver
        self._transcript_dir = transcript_dir
        self._runtimes: dict[ResourceKind, DebugEngineRuntime] = {}
        self._guard = threading.Lock()

    async def runtime_for_session(
        self, pool: AsyncConnectionPool, session_id: UUID
    ) -> DebugEngineRuntime | ToolResponse:
        async with pool.connection() as conn:
            try:
                binding = await self._resolver.binding_for_session(conn, session_id)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(str(session_id), exc)
        return self.runtime_for_binding(binding, object_id=str(session_id))

    def runtime_for_binding(
        self, binding: ProviderBinding, *, object_id: str | None = None
    ) -> DebugEngineRuntime | ToolResponse:
        debug = binding.runtime.debug
        if debug is None:
            return ToolResponse.failure(
                object_id or binding.kind.value,
                ErrorCategory.DEBUG_ATTACH_FAILURE,
                data={"reason": "provider_debug_unavailable"},
            )
        with self._guard:
            runtime = self._runtimes.get(binding.kind)
            if runtime is None:
                runtime = DebugEngineRuntime(
                    engine=debug.engine,
                    attach=debug.attach_seam,
                    transcript_dir=self._transcript_dir,
                )
                self._runtimes[binding.kind] = runtime
            return runtime


def _op_failure(session_id: str, exc: CategorizedError) -> ToolResponse:
    """Map an engine ``CategorizedError`` onto a failure envelope (with its ``code`` if any)."""
    category = exc.category
    if category is ErrorCategory.MISSING_DEPENDENCY:
        category = ErrorCategory.DEBUG_ATTACH_FAILURE
    return ToolResponse.failure_from_error(session_id, exc, category=category)


async def _live_session(
    pool: AsyncConnectionPool, ctx: RequestContext, session_id: str
) -> DebugSession | ToolResponse:
    """UUID-parse, load, project/role-gate, and require ``live`` state (ADR-0034 §5a codes)."""
    async with pool.connection() as conn:
        resolved = await resolve_debug_session_context(conn, ctx, session_id, require_live=True)
    if isinstance(resolved, ToolResponse):
        return resolved
    return resolved.session


async def run_engine_op(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    session_id: str,
    runtime: DebugEngineRuntime | DebugRuntimeResolver,
    op: _EngineOp,
    *,
    audit: _OpAudit | None = None,
) -> ToolResponse:
    """Gate the session, take the per-session lock, attach-or-reuse, and run ``op`` off-loop.

    The blocking engine work (attach + ``op``) is dispatched via ``asyncio.to_thread`` under the
    per-session ``asyncio.Lock`` so a long `continue` never stalls the event loop and only one
    op ever attaches/drives a given engine (ADR-0034 §4a/§4b).

    When ``audit`` is set and the op succeeds, one ``audit_log`` row attributes the op to the
    caller against the session (ADR-0006). The op already ran (an external engine call cannot be
    rolled back), so this is a post-hoc attribution written after the engine lock is released; a
    gate failure or op error returns before it, writing nothing.
    """
    with bind_context(principal=ctx.principal):
        gated = await _live_session(pool, ctx, session_id)
        if isinstance(gated, ToolResponse):
            return gated
        session = gated
        resolved_runtime = await _runtime_for_op(pool, session, runtime)
        if isinstance(resolved_runtime, ToolResponse):
            return resolved_runtime
        async with resolved_runtime.lock_for(session_id):
            try:
                result = await asyncio.to_thread(_attach_and_run, resolved_runtime, session, op)
            except CategorizedError as exc:
                return _op_failure(session_id, exc)
        if audit is not None and result.error_category is None:
            await _record_op_audit(pool, ctx, session, audit)
        return result


async def _record_op_audit(
    pool: AsyncConnectionPool, ctx: RequestContext, session: DebugSession, op_audit: _OpAudit
) -> None:
    """Append one audit_log row attributing ``op_audit`` to the caller against ``session``."""
    async with pool.connection() as conn, conn.transaction():
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool=op_audit.tool,
                object_kind="debug_sessions",
                object_id=session.id,
                transition=op_audit.transition,
                args={
                    "session_id": str(session.id),
                    "run_id": str(session.run_id),
                    **op_audit.args,
                },
                project=session.project,
            ),
        )


async def _runtime_for_op(
    pool: AsyncConnectionPool,
    session: DebugSession,
    runtime: DebugEngineRuntime | DebugRuntimeResolver,
) -> DebugEngineRuntime | ToolResponse:
    if isinstance(runtime, DebugRuntimeResolver):
        return await runtime.runtime_for_session(pool, session.id)
    return runtime


def _attach_and_run(
    runtime: DebugEngineRuntime, session: DebugSession, op: _EngineOp
) -> ToolResponse:
    return op(runtime.engine, runtime.attach_or_reuse(session))


def _set_breakpoint_op(session_id: str, location: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        ref = engine.set_breakpoint(attachment, location)
        return ToolResponse.success(
            session_id,
            "set",
            suggested_next_actions=["debug.continue", "debug.list_breakpoints"],
            data={"number": ref.number},
        )

    return op


def _clear_breakpoint_op(session_id: str, number: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        engine.clear_breakpoint(attachment, number)
        return ToolResponse.success(
            session_id, "cleared", suggested_next_actions=["debug.list_breakpoints"]
        )

    return op


def _list_breakpoints_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        refs = engine.list_breakpoints(attachment)
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.set_breakpoint", "debug.continue"],
            data={"count": len(refs)},
        )

    return op


def _read_memory_op(session_id: str, address: int, byte_count: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        blob = engine.read_memory(attachment, address=address, byte_count=byte_count)
        return ToolResponse.success(
            session_id,
            "read",
            suggested_next_actions=["debug.read_registers", "debug.continue"],
            data={
                "address": f"0x{address:x}",
                "byte_count": len(blob),
                "memory_hex": blob.hex(),
            },
        )

    return op


def _read_registers_op(session_id: str, registers: list[str]) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        values = engine.read_registers(attachment, registers)
        rendered = {str(k): str(v) for k, v in values.items()}
        return ToolResponse.success(
            session_id,
            "read",
            suggested_next_actions=["debug.read_memory", "debug.continue"],
            data=rendered,
        )

    return op


def _resolve_symbol_op(session_id: str, name: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        address = engine.resolve_symbol(attachment, name)
        return ToolResponse.success(
            session_id,
            "resolved",
            suggested_next_actions=["debug.read_memory", "debug.read_registers"],
            data={"symbol": name, "address": f"0x{address:x}"},
        )

    return op


def _continue_op(session_id: str, timeout_sec: float) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.continue_(attachment, timeout_sec=timeout_sec)
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=[
                "debug.read_registers",
                "debug.read_memory",
                "debug.list_breakpoints",
            ],
            data=_stop_data(stop.reason, stop.timed_out),
        )

    return op


def _interrupt_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.interrupt(attachment)
        reason = stop.reason if stop is not None else None
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=["debug.read_registers", "debug.continue"],
            data=_stop_data(reason, False),
        )

    return op


def _backtrace_op(session_id: str, max_frames: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.backtrace(attachment, max_frames=max_frames)
        frames: list[JsonValue] = [
            frame.model_dump(mode="json", exclude_none=True) for frame in result.frames
        ]
        return ToolResponse.success(
            session_id,
            "walked",
            suggested_next_actions=["debug.read_frame", "debug.read_registers"],
            data={"frame_count": len(frames), "truncated": result.truncated, "frames": frames},
        )

    return op


def _read_frame_op(session_id: str, level: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        frame = engine.read_frame(attachment, level=level)
        return ToolResponse.success(
            session_id,
            "read",
            suggested_next_actions=["debug.read_registers", "debug.read_memory"],
            data={"level": level, "frame": frame.model_dump(mode="json", exclude_none=True)},
        )

    return op


def _disassemble_op(
    session_id: str, symbol: str | None, address: int | None, instruction_count: int
) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.disassemble(
            attachment, symbol=symbol, address=address, instruction_count=instruction_count
        )
        instructions: list[JsonValue] = [
            insn.model_dump(mode="json", exclude_none=True) for insn in result.instructions
        ]
        return ToolResponse.success(
            session_id,
            "disassembled",
            suggested_next_actions=["debug.read_memory", "debug.read_registers"],
            data={
                "instruction_count": len(instructions),
                "truncated": result.truncated,
                "instructions": instructions,
            },
        )

    return op


def _set_watchpoint_op(
    session_id: str, symbol: str | None, address: int | None, byte_count: int
) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        ref = engine.set_watchpoint(
            attachment, symbol=symbol, address=address, byte_count=byte_count
        )
        data: dict[str, JsonValue] = {"number": ref.number, "byte_count": byte_count}
        if ref.expr is not None:
            data["expr"] = ref.expr
        return ToolResponse.success(
            session_id,
            "watching",
            suggested_next_actions=["debug.continue", "debug.list_watchpoints"],
            data=data,
        )

    return op


def _list_watchpoints_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        refs = engine.list_watchpoints(attachment)
        watchpoints: list[JsonValue] = [
            ref.model_dump(mode="json", exclude_none=True) for ref in refs
        ]
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.set_watchpoint", "debug.continue"],
            data={"count": len(watchpoints), "watchpoints": watchpoints},
        )

    return op


def _clear_watchpoint_op(session_id: str, number: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        engine.clear_watchpoint(attachment, number)
        return ToolResponse.success(
            session_id, "cleared", suggested_next_actions=["debug.list_watchpoints"]
        )

    return op


def _list_modules_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.list_modules(attachment, max_modules=MAX_MODULES)
        modules: list[JsonValue] = [
            module.model_dump(mode="json", exclude_none=True) for module in result.modules
        ]
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.load_module_symbols", "debug.backtrace"],
            data={
                "count": len(modules),
                "truncated": result.truncated,
                "decode_errors": result.decode_errors,
                "modules": modules,
            },
        )

    return op


def _load_module_symbols_op(session_id: str, module: str, expected_base: int | None) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.load_module_symbols(attachment, module=module, expected_base=expected_base)
        data: dict[str, JsonValue] = {
            "module": result.name,
            "base_address": result.base_address,
            "symbols_loaded": result.symbols_loaded,
        }
        if result.identity_verified is not None:
            data["identity_verified"] = result.identity_verified
        return ToolResponse.success(
            session_id,
            "loaded",
            suggested_next_actions=[
                "debug.backtrace",
                "debug.disassemble",
                "debug.list_modules",
            ],
            data=data,
        )

    return op


def _stop_data(reason: str | None, timed_out: bool) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {"timed_out": timed_out}
    if reason is not None:
        data["reason"] = reason
    return data


def _register_debug_ops(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    """Register the sixteen gdb-MI `debug.*` tools on ``app``, sharing ``runtime`` (ADR-0034)."""
    _register_debug_set_breakpoint(app, pool, runtime)
    _register_debug_clear_breakpoint(app, pool, runtime)
    _register_debug_list_breakpoints(app, pool, runtime)
    _register_debug_read_memory(app, pool, runtime)
    _register_debug_read_registers(app, pool, runtime)
    _register_debug_resolve_symbol(app, pool, runtime)
    _register_debug_continue(app, pool, runtime)
    _register_debug_interrupt(app, pool, runtime)
    _register_debug_backtrace(app, pool, runtime)
    _register_debug_read_frame(app, pool, runtime)
    _register_debug_disassemble(app, pool, runtime)
    _register_debug_set_watchpoint(app, pool, runtime)
    _register_debug_list_watchpoints(app, pool, runtime)
    _register_debug_clear_watchpoint(app, pool, runtime)
    _register_debug_list_modules(app, pool, runtime)
    _register_debug_load_module_symbols(app, pool, runtime)


def _register_debug_set_breakpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.set_breakpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_set_breakpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession to set a breakpoint on.")
        ],
        location: Annotated[str, Field(description="Bare C function or symbol name to break at.")],
    ) -> ToolResponse:
        """Set a breakpoint on a live DebugSession via gdb-MI. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _set_breakpoint_op(session_id, location),
            audit=_op_audit("debug.set_breakpoint", location=location),
        )


def _register_debug_clear_breakpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.clear_breakpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_clear_breakpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose breakpoint to clear.")
        ],
        number: Annotated[
            str,
            Field(description="Breakpoint number to clear (from debug.list_breakpoints)."),
        ],
    ) -> ToolResponse:
        """Clear a breakpoint by number on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _clear_breakpoint_op(session_id, number),
            audit=_op_audit("debug.clear_breakpoint", number=number),
        )


def _register_debug_list_breakpoints(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.list_breakpoints",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_list_breakpoints(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose breakpoints to list.")
        ],
    ) -> ToolResponse:
        """List all breakpoints on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool, current_context(), session_id, runtime, _list_breakpoints_op(session_id)
        )


def _register_debug_read_memory(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.read_memory",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_read_memory(
        session_id: Annotated[str, Field(description="The live DebugSession to read memory from.")],
        address: Annotated[int, Field(description="Start address (integer) to read from.")],
        byte_count: Annotated[
            int, Field(description=f"Number of bytes to read (capped at {MAX_MEMORY_READ_BYTES}).")
        ],
    ) -> ToolResponse:
        """Read raw memory bytes from a live DebugSession (bounded by byte_count). Contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _read_memory_op(session_id, address, byte_count),
            audit=_op_audit("debug.read_memory", address=f"0x{address:x}", byte_count=byte_count),
        )


def _register_debug_read_registers(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.read_registers",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_read_registers(
        session_id: Annotated[
            str, Field(description="The live DebugSession to read registers from.")
        ],
        registers: Annotated[
            list[str],
            Field(description='Register names to read (e.g. ["rip", "rsp"]).'),
        ],
    ) -> ToolResponse:
        """Read named registers from a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _read_registers_op(session_id, registers),
        )


def _register_debug_resolve_symbol(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.resolve_symbol",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_resolve_symbol(
        session_id: Annotated[
            str, Field(description="The live DebugSession to resolve the symbol on.")
        ],
        name: Annotated[
            str,
            Field(
                description="Bare C global or function symbol name to resolve to its address "
                "(e.g. 'd_hash_shift'). Read its value with debug.read_memory. This resolves an "
                "address only; to read a struct field or array member by name "
                "(some_struct->field[3].member), use the drgn path introspect.script instead."
            ),
        ],
    ) -> ToolResponse:
        """Resolve a kernel symbol to its address on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _resolve_symbol_op(session_id, name),
        )


def _register_debug_continue(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.continue",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_continue(
        session_id: Annotated[
            str, Field(description="The live DebugSession to continue execution on.")
        ],
        timeout_sec: Annotated[
            float,
            Field(
                description="Seconds to wait for a stop event; 0.0 uses the provider "
                "interactive wait cap."
            ),
        ] = 0.0,
    ) -> ToolResponse:
        """Resume a live DebugSession and wait for a stop event. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _continue_op(session_id, timeout_sec),
            audit=_op_audit("debug.continue", timeout_sec=timeout_sec),
        )


def _register_debug_interrupt(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.interrupt",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_interrupt(
        session_id: Annotated[str, Field(description="The live DebugSession to interrupt.")],
    ) -> ToolResponse:
        """Send an interrupt to halt a running live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _interrupt_op(session_id),
            audit=_op_audit("debug.interrupt"),
        )


def _register_debug_backtrace(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.backtrace",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_backtrace(
        session_id: Annotated[
            str, Field(description="The live DebugSession to walk the stopped stack on.")
        ],
        max_frames: Annotated[
            int,
            Field(
                description="Maximum frames to return (1-64); the backtrace is truncated past it."
            ),
        ] = 64,
    ) -> ToolResponse:
        """Walk the stopped kernel's stack on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _backtrace_op(session_id, max_frames),
        )


def _register_debug_read_frame(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.read_frame",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_read_frame(
        session_id: Annotated[
            str, Field(description="The live DebugSession to inspect a stack frame on.")
        ],
        level: Annotated[
            int,
            Field(description="Stack frame index to inspect (0 is the innermost frame)."),
        ],
    ) -> ToolResponse:
        """Inspect one selected stack frame on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _read_frame_op(session_id, level),
        )


def _register_debug_disassemble(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.disassemble",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_disassemble(
        session_id: Annotated[str, Field(description="The live DebugSession to disassemble on.")],
        symbol: Annotated[
            str | None,
            Field(
                description="Bare C function/symbol name to disassemble around (or use address)."
            ),
        ] = None,
        address: Annotated[
            int | None,
            Field(description="Start address (integer) to disassemble from (or use symbol)."),
        ] = None,
        instruction_count: Annotated[
            int,
            Field(description="Instructions to return (1-256); the window is truncated past it."),
        ] = 64,
    ) -> ToolResponse:
        """Disassemble a bounded window around a symbol/address on a live DebugSession.

        Requires contributor.
        """
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _disassemble_op(session_id, symbol, address, instruction_count),
        )


def _register_debug_set_watchpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.set_watchpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_set_watchpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession to set a watchpoint on.")
        ],
        symbol: Annotated[
            str | None,
            Field(description="Bare C symbol to watch for writes (or use address)."),
        ] = None,
        address: Annotated[
            int | None,
            Field(description="Start address (integer) to watch for writes (or use symbol)."),
        ] = None,
        byte_count: Annotated[
            int,
            Field(description="Bytes to watch; one of 1, 2, 4, or 8 (one hardware watchpoint)."),
        ] = 8,
    ) -> ToolResponse:
        """Set a hardware write watchpoint on a symbol/address for a live DebugSession.

        Watchpoints are hardware (debug-register) watchpoints: the stub may accept one yet never
        trap, surfacing as a debug.continue timeout rather than an error. Requires contributor.
        """
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _set_watchpoint_op(session_id, symbol, address, byte_count),
            audit=_op_audit(
                "debug.set_watchpoint",
                symbol=symbol,
                address=None if address is None else f"0x{address:x}",
                byte_count=byte_count,
            ),
        )


def _register_debug_list_watchpoints(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.list_watchpoints",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_list_watchpoints(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose watchpoints to list.")
        ],
    ) -> ToolResponse:
        """List all watchpoints on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool, current_context(), session_id, runtime, _list_watchpoints_op(session_id)
        )


def _register_debug_clear_watchpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.clear_watchpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_clear_watchpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose watchpoint to clear.")
        ],
        number: Annotated[
            str, Field(description="Watchpoint number to clear (from debug.list_watchpoints).")
        ],
    ) -> ToolResponse:
        """Clear a watchpoint by number on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _clear_watchpoint_op(session_id, number),
            audit=_op_audit("debug.clear_watchpoint", number=number),
        )


def _register_debug_list_modules(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.list_modules",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_list_modules(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose loaded modules to list.")
        ],
    ) -> ToolResponse:
        """List loaded kernel modules (name, base address, whether symbols are loaded).

        Requires contributor.
        """
        return await run_engine_op(
            pool, current_context(), session_id, runtime, _list_modules_op(session_id)
        )


def _register_debug_load_module_symbols(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.load_module_symbols",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_load_module_symbols(
        session_id: Annotated[
            str, Field(description="The live DebugSession to load module symbols on.")
        ],
        module: Annotated[
            str,
            Field(description="Loaded module name to load symbols for (from debug.list_modules)."),
        ],
        expected_base: Annotated[
            int | None,
            Field(
                description="The base address seen in debug.list_modules; if it no longer matches "
                "the live module, the load is refused as stale rather than loading wrong symbols."
            ),
        ] = None,
    ) -> ToolResponse:
        """Load one loaded module's symbols at its current base on a live DebugSession.

        Requires contributor.
        """
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _load_module_symbols_op(session_id, module, expected_base),
            audit=_op_audit(
                "debug.load_module_symbols",
                module=module,
                expected_base=None if expected_base is None else f"0x{expected_base:x}",
            ),
        )
