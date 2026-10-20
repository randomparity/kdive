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
import functools
import logging
import shutil
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID
from weakref import WeakValueDictionary

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import DEBUG_DIR
from kdive.db.repositories import RUNS, SYSTEMS
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
    GdbMiAttachFailure,
    GdbMiAttachment,
    GdbMiEngine,
)
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.providers.shared.debug_common.gdbmi.core.engine import (
    HALT_BOUND_SEC,
    RESUME_HOLD_BOUND_SEC,
)
from kdive.providers.shared.debug_common.gdbmi.core.execution import inferior_running
from kdive.providers.shared.debug_common.gdbmi.policy.capabilities import (
    DebugCapability,
    supports,
)
from kdive.providers.shared.debug_common.gdbmi.policy.debuginfo import close_attachment
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.serialization import JsonValue

_EngineOp = Callable[[GdbMiEngine, GdbMiAttachment], ToolResponse]

_log = logging.getLogger(__name__)

# Next actions for engine failures an agent recovers from without new input (#3244): a running
# guest is halted or waited on; a stalled link is interrupted again or the session ended.
_RECOVERY_ACTIONS: dict[str, tuple[str, ...]] = {
    "inferior_running": ("debug.interrupt", "debug.continue"),
    "transport_stall": ("debug.interrupt", "debug.end_session"),
    "interrupt_refused": ("debug.end_session",),
}


@dataclass(frozen=True)
class _OpAudit:
    """The attribution to record for one Debug-plane op (ADR-0006/0028)."""

    tool: str
    transition: str
    args: Mapping[str, object]


@dataclass(frozen=True)
class CapabilityRequirement:
    """A debug capability an op needs, and the refusal to return without it (ADR-0712)."""

    capability: DebugCapability
    code: str
    detail: str
    next_actions: tuple[str, ...]
    data: Mapping[str, JsonValue] = field(default_factory=dict)


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
        "debug.advance",
        "debug.load_module_symbols",
    }
)


def _op_audit(tool: str, transition: str | None = None, /, **args: object) -> _OpAudit | None:
    """Return the audit descriptor for an audited ``tool``, or None to skip auditing.

    ``transition`` defaults to the bare op name (``tool`` sans the ``debug.`` prefix); ``args``
    are the op parameters recorded for ``args_digest`` correlation (never raw memory bytes).

    A tool that folds several distinct operations behind one name passes an explicit
    ``transition`` so the audit trail keeps one row per operation rather than collapsing them:
    ``debug.advance`` records ``advance:into`` / ``advance:over`` / ``advance:instruction`` /
    ``advance:out`` (ADR-0463 §3). It is positional-only so it can never collide with an audited
    op parameter in ``args``.
    """
    if tool not in _AUDITED_OPS:
        return None
    return _OpAudit(tool=tool, transition=transition or tool.removeprefix("debug."), args=args)


def with_halt_bounds[F: Callable[..., object]](func: F) -> F:
    """Interpolate the enforced halt and wait bounds into a wrapper docstring (#3244)."""
    if func.__doc__ is None:
        raise AssertionError("a debug wrapper that states halt bounds must have a docstring")
    func.__doc__ = func.__doc__.format(
        halt_s=f"{HALT_BOUND_SEC:g}",
        continue_hold_s=f"{RESUME_HOLD_BOUND_SEC:g}",
    )
    return func


def _gdbmi_maturity() -> dict[str, object]:
    """Return the shared implemented maturity for the gdb-MI tools (ADR-0175)."""
    return _docmeta.maturity_meta("implemented")


def _default_transcript_dir() -> Path:
    # Configurable (KDIVE_DEBUG_DIR) so a deployment points it at the run-artifact tree and
    # tests at a temp dir; the registry default mirrors the other planes' /var/lib/kdive/* roots.
    return Path(config.require(DEBUG_DIR))


async def _finish_task[T](task: asyncio.Future[T]) -> T:
    """Return only once ``task`` has finished; absorb each cancel until then, then re-raise it."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        with suppress(BaseException):
            task.result()  # Retrieve a task error the cancel supersedes, so it is not unlogged.
        raise asyncio.CancelledError
    return task.result()


class ReapFailure(Exception):
    """Local cleanup failed after detach; ``halted`` is False when the halt before it also did."""

    def __init__(self, halted: bool) -> None:
        super().__init__("debug attachment cleanup failed")
        self.halted = halted


class DebugEngineRuntime:
    """Process-scoped holder for the lazy gdb-MI engines + per-session locks (ADR-0034 §4a).

    Owns the in-process :class:`GdbMiSessionRegistry`, a per-session ``asyncio.Lock`` table (the
    get-or-create guarded by a plain :class:`threading.Lock`), and the injected
    :class:`AttachSeam`. One instance is built in app assembly and shared by every
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
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._closing = False
        self._pending_roots: dict[str, Path] = {}
        self._active: set[asyncio.Task[ToolResponse]] = set()
        self._locks_guard = threading.Lock()

    @property
    def engine(self) -> GdbMiEngine:
        return self._engine

    def lock_for(self, session_id: str) -> asyncio.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, asyncio.Lock())

    def reap(self, session_id: str) -> bool:
        """Exit the session's gdb and remove its owned symbol root (no-op if never attached).

        A guest left running by a background continue is halted first, so gdb exits from a
        halted target as on every other detach (#3244). Returns False only when that halt
        failed, leaving the guest's execution unknown; blocking, so call it off the event loop.
        A failed exit or attachment-root removal raises :class:`ReapFailure` and keeps the owner
        registered for a retry; a failed removal of a root-only pending owner raises its OSError.
        """
        root = self._pending_roots.get(session_id)
        if root is not None:
            with suppress(FileNotFoundError):
                shutil.rmtree(root)
            self._pending_roots.pop(session_id)
        attachment = self._registry.get(session_id)
        if attachment is None:
            return True
        halted = True
        if attachment.execution == "running":
            try:
                self._engine.interrupt(attachment)
            except Exception:
                _log.warning("debug session %s: halt before detach failed", session_id)
                halted = False
        try:
            close_attachment(attachment)
        except Exception as exc:
            raise ReapFailure(halted) from exc
        self._registry.reap(session_id)
        return halted

    def attach_or_reuse(self, session: DebugSession) -> GdbMiAttachment:
        session_id = str(session.id)
        if self._closing:
            raise CategorizedError(
                "debug runtime is shutting down", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        existing = self._registry.get(session_id)
        if session_id in self._pending_roots or (existing is not None and existing.cleanup_pending):
            try:
                self.reap(session_id)
            except Exception as exc:
                raise CategorizedError(
                    "debug attachment cleanup is pending",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"reason": "debug_cleanup_pending"},
                ) from exc
            existing = None
        if existing is not None:
            return existing
        endpoint = TransportHandleData.decode(session.transport_handle or "")
        try:
            attachment = self._attach(
                host=endpoint.host,
                port=endpoint.port,
                run_id=str(session.run_id),
                transcript_path=self._transcript_dir / f"{session_id}.jsonl",
            )
        except GdbMiAttachFailure as failure:
            if failure.attachment is None:
                assert failure.staging_dir is not None
                self._pending_roots[session_id] = failure.staging_dir
            else:
                failure.attachment.cleanup_pending = True
                self._registry.register(session_id, failure.attachment)
            raise CategorizedError(
                "debug attachment cleanup is pending",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"reason": "debug_cleanup_pending"},
            ) from failure
        self._registry.register(session_id, attachment)
        return attachment

    async def run(
        self, session: DebugSession, op: _EngineOp, *, while_running: bool = False
    ) -> ToolResponse:
        if self._closing:
            raise CategorizedError(
                "debug runtime is shutting down", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        task = asyncio.create_task(
            asyncio.to_thread(_attach_and_run, self, session, op, while_running)
        )
        self._active.add(task)
        try:
            return await _finish_task(task)
        finally:
            self._active.discard(task)


class DebugRuntimeResolver:
    """Provider-aware cache of per-provider debug engine runtimes."""

    def __init__(self, resolver: ProviderResolver, *, transcript_dir: Path | None = None) -> None:
        self._resolver = resolver
        self._transcript_dir = transcript_dir
        self._closing = False
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
            if self._closing:
                return ToolResponse.failure(
                    object_id or binding.kind.value,
                    ErrorCategory.INFRASTRUCTURE_FAILURE,
                    detail="debug runtime is shutting down",
                )
            runtime = self._runtimes.get(binding.cache_key)
            if runtime is None:
                runtime = DebugEngineRuntime(
                    engine=debug.engine,
                    attach=debug.attach_seam,
                    transcript_dir=self._transcript_dir,
                )
                self._runtimes[binding.cache_key] = runtime
            return runtime

    def owned(self) -> tuple[tuple[DebugEngineRuntime, str], ...]:
        with self._guard:
            runtimes = tuple(self._runtimes.values())
        return tuple(
            (runtime, sid)
            for runtime in runtimes
            for sid in set(runtime._registry.ids()) | runtime._pending_roots.keys()
        )

    async def sweep_once(self, pool: AsyncConnectionPool, *, shutdown: bool = False) -> None:
        for runtime, sid in self.owned():
            try:
                async with runtime.lock_for(sid):
                    attachment = runtime._registry.get(sid)
                    if attachment is None and sid not in runtime._pending_roots:
                        continue
                    if not shutdown and attachment is not None and not attachment.cleanup_pending:
                        async with (
                            asyncio.timeout(5.0),
                            pool.connection() as conn,
                            conn.cursor() as cur,
                        ):
                            await cur.execute(
                                "SELECT state FROM debug_sessions WHERE id = %s", (UUID(sid),)
                            )
                            row = await cur.fetchone()
                        if row is not None and row[0] == "live":
                            continue
                    await run_to_completion(functools.partial(runtime.reap, sid))
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "debug cleanup pending for %s (%s); retry debug.end_session after recovery",
                    sid,
                    type(exc).__name__,
                )

    @asynccontextmanager
    async def lifespan(self, pool: AsyncConnectionPool) -> AsyncIterator[None]:
        stopped = asyncio.Event()

        async def sweep():
            while not stopped.is_set():
                await self.sweep_once(pool)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), timeout=30.0)

        task = asyncio.create_task(sweep())
        try:
            yield
        finally:

            async def shutdown() -> None:
                with self._guard:
                    self._closing = True
                    runtimes = tuple(self._runtimes.values())
                    for runtime in runtimes:
                        runtime._closing = True
                stopped.set()
                await task
                active = {work for runtime in runtimes for work in tuple(runtime._active)}
                if active:
                    await asyncio.wait(active)
                await self.sweep_once(pool, shutdown=True)

            await _finish_task(asyncio.create_task(shutdown()))


_RuntimeLookup = Callable[[DebugSession], Awaitable[DebugEngineRuntime | ToolResponse]]


def _op_failure(session_id: str, exc: CategorizedError) -> ToolResponse:
    """Map an engine ``CategorizedError`` onto a failure envelope (with its ``code`` if any)."""
    category = exc.category
    if category is ErrorCategory.MISSING_DEPENDENCY:
        category = ErrorCategory.DEBUG_ATTACH_FAILURE
    code = exc.details.get("code")
    actions = _RECOVERY_ACTIONS.get(code) if isinstance(code, str) else None
    if exc.details.get("reason") == "debug_cleanup_pending":
        actions = ("debug.end_session", "debug.get_session")
    return ToolResponse.failure_from_error(
        session_id,
        exc,
        category=category,
        suggested_next_actions=list(actions) if actions is not None else None,
    )


async def run_to_completion[T](call: Callable[[], T]) -> T:
    """Run blocking ``call`` off the event loop and return only once it has finished.

    Await it while holding the per-session lock. A cancelled caller would otherwise leave
    ``async with`` while the thread still drives gdb, letting the next op write the same gdb/MI
    stream (#3244). ``asyncio.wait`` never cancels the task, so every cancel is absorbed until
    the thread finishes, then re-raised.
    """
    return await _finish_task(asyncio.ensure_future(asyncio.to_thread(call)))


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
    while_running: bool = False,
) -> ToolResponse:
    """Run a Debug-plane op against an already constructed engine runtime."""

    async def _runtime_for_session(_session: DebugSession) -> DebugEngineRuntime:
        return runtime

    return await _run_engine_op(
        pool, ctx, session_id, _runtime_for_session, op, audit=audit, while_running=while_running
    )


async def run_engine_op_with_resolver(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    session_id: str,
    runtime_resolver: DebugRuntimeResolver,
    op: _EngineOp,
    *,
    audit: _OpAudit | None = None,
    requires: CapabilityRequirement | None = None,
    while_running: bool = False,
) -> ToolResponse:
    """Run a Debug-plane op after resolving the session's provider debug runtime.

    ``while_running`` admits the op while a background continue has the guest running; every
    other op is refused with ``inferior_running`` before it sends a gdb/MI command (#3244).
    """

    async def _runtime_for_session(session: DebugSession) -> DebugEngineRuntime | ToolResponse:
        return await runtime_resolver.runtime_for_session(pool, session.id)

    return await _run_engine_op(
        pool,
        ctx,
        session_id,
        _runtime_for_session,
        op,
        audit=audit,
        requires=requires,
        while_running=while_running,
    )


async def _run_engine_op(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    session_id: str,
    runtime_for_session: _RuntimeLookup,
    op: _EngineOp,
    *,
    audit: _OpAudit | None = None,
    requires: CapabilityRequirement | None = None,
    while_running: bool = False,
) -> ToolResponse:
    """Gate the session, take the per-session lock, attach-or-reuse, and run ``op`` off-loop.

    The blocking engine work (attach + ``op``) is dispatched via ``asyncio.to_thread`` under the
    per-session ``asyncio.Lock`` so a long `continue` never stalls the event loop and only one
    op ever attaches/drives a given engine (ADR-0034 §4a/§4b).

    When ``audit`` is set and the op succeeds, one ``audit_log`` row attributes the op to the
    caller against the session (ADR-0006). The op already ran (an external engine call cannot be
    rolled back), so this is a post-hoc attribution written after the engine lock is released; a
    gate failure or op error returns before it, writing nothing.

    When ``requires`` is set and the session's System lacks that capability, the refusal returns
    before the runtime lookup, so no gdb/MI command reaches the target (ADR-0712).

    The session is re-gated after the lock is taken: an op queued behind ``end_session``'s reap
    gets the not-live refusal instead of attaching a new gdb to a detached session (#3244).
    """
    with bind_context(principal=ctx.principal):
        gated = await _live_session(pool, ctx, session_id)
        if isinstance(gated, ToolResponse):
            return gated
        session = gated
        if requires is not None:
            refusal = await _capability_refusal(pool, session, requires)
            if refusal is not None:
                return refusal
        resolved_runtime = await runtime_for_session(session)
        if isinstance(resolved_runtime, ToolResponse):
            return resolved_runtime
        key = str(session.id)
        async with resolved_runtime.lock_for(key):
            regated = await _live_session(pool, ctx, key)
            if isinstance(regated, ToolResponse):
                return regated
            try:
                result = await resolved_runtime.run(regated, op, while_running=while_running)
            except CategorizedError as exc:
                return _op_failure(session_id, exc)
        if audit is not None and result.error_category is None:
            await _record_op_audit(pool, ctx, session, audit)
        return result


async def _target_platform(
    pool: AsyncConnectionPool, session: DebugSession
) -> tuple[str | None, str | None]:
    """Return the ``(arch, accel)`` of the System the session's Run is bound to."""
    async with pool.connection() as conn:
        run = await RUNS.get(conn, session.run_id)
        system_id = None if run is None else run.system_id
        system = None if system_id is None else await SYSTEMS.get(conn, system_id)
    if system is None:
        return None, None
    arch = system.provisioning_profile.get("arch")
    return (arch if isinstance(arch, str) else None), system.accel


async def _capability_refusal(
    pool: AsyncConnectionPool, session: DebugSession, requires: CapabilityRequirement
) -> ToolResponse | None:
    arch, accel = await _target_platform(pool, session)
    if supports(arch, accel, requires.capability):
        return None
    return ToolResponse.failure(
        str(session.id),
        ErrorCategory.NOT_IMPLEMENTED,
        detail=requires.detail,
        suggested_next_actions=list(requires.next_actions),
        data={"code": requires.code, "arch": arch, "accel": accel, **requires.data},
    )


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
    runtime: DebugEngineRuntime, session: DebugSession, op: _EngineOp, while_running: bool
) -> ToolResponse:
    attachment = runtime.attach_or_reuse(session)
    if attachment.execution == "running" and not while_running:
        raise inferior_running()
    return op(runtime.engine, attachment)
