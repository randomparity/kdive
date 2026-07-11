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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import DEBUG_DIR
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import DebugSession
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.debug.sessions.context import (
    resolve_debug_session_context,
)
from kdive.mcp.tools.debug.sessions.registry import GdbMiSessionRegistry
from kdive.providers.core.resolver import ProviderBinding, ProviderResolver
from kdive.providers.ports.debug import (
    AttachSeam,
    GdbMiAttachment,
    GdbMiEngine,
)
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.security import audit
from kdive.security.authz.context import RequestContext

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
        self._runtimes: dict[tuple[ResourceKind, str | None], DebugEngineRuntime] = {}
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
            runtime = self._runtimes.get(binding.cache_key)
            if runtime is None:
                runtime = DebugEngineRuntime(
                    engine=debug.engine,
                    attach=debug.attach_seam,
                    transcript_dir=self._transcript_dir,
                )
                self._runtimes[binding.cache_key] = runtime
            return runtime


_RuntimeLookup = Callable[[DebugSession], Awaitable[DebugEngineRuntime | ToolResponse]]


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


async def run_engine_op_with_runtime(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    session_id: str,
    runtime: DebugEngineRuntime,
    op: _EngineOp,
    *,
    audit: _OpAudit | None = None,
) -> ToolResponse:
    """Run a Debug-plane op against an already constructed engine runtime."""

    async def _runtime_for_session(_session: DebugSession) -> DebugEngineRuntime:
        return runtime

    return await _run_engine_op(pool, ctx, session_id, _runtime_for_session, op, audit=audit)


async def run_engine_op_with_resolver(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    session_id: str,
    runtime_resolver: DebugRuntimeResolver,
    op: _EngineOp,
    *,
    audit: _OpAudit | None = None,
) -> ToolResponse:
    """Run a Debug-plane op after resolving the session's provider debug runtime."""

    async def _runtime_for_session(session: DebugSession) -> DebugEngineRuntime | ToolResponse:
        return await runtime_resolver.runtime_for_session(pool, session.id)

    return await _run_engine_op(pool, ctx, session_id, _runtime_for_session, op, audit=audit)


async def _run_engine_op(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    session_id: str,
    runtime_for_session: _RuntimeLookup,
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
        resolved_runtime = await runtime_for_session(session)
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


def _attach_and_run(
    runtime: DebugEngineRuntime, session: DebugSession, op: _EngineOp
) -> ToolResponse:
    return op(runtime.engine, runtime.attach_or_reuse(session))
