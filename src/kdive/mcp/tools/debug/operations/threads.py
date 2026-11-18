"""CPU (gdbstub thread) listing and selection debug-op registration (ADR-0819)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.debug.operations.runtime import (
    DebugRuntimeResolver,
    _EngineOp,
    _gdbmi_maturity,
    _op_audit,
    run_engine_op_with_resolver,
)
from kdive.providers.ports.debug import GdbMiAttachment, GdbMiEngine
from kdive.providers.shared.debug_common.gdbmi.commands.threads import MAX_THREADS
from kdive.serialization import JsonValue


def register(app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver) -> None:
    _register_debug_list_threads(app, pool, runtime)
    _register_debug_select_thread(app, pool, runtime)


def _with_thread_bound[F: Callable[..., object]](func: F) -> F:
    """Interpolate the enforced thread cap into the tool docstring (ADR-0819)."""
    if func.__doc__ is None:
        raise AssertionError("debug.list_threads states its thread cap in its docstring")
    func.__doc__ = func.__doc__.format(max_threads=MAX_THREADS)
    return func


def _list_threads_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.list_threads(attachment)
        threads: list[JsonValue] = [
            thread.model_dump(mode="json", by_alias=True, exclude_none=True)
            for thread in result.threads
        ]
        data: dict[str, JsonValue] = {
            "thread_count": len(threads),
            "truncated": result.truncated,
            "threads": threads,
        }
        if result.current_thread_id is not None:
            data["current_thread_id"] = result.current_thread_id
        if result.selected_thread_id is not None:
            data["selected_thread_id"] = result.selected_thread_id
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.select_thread", "debug.read_registers"],
            data=data,
        )

    return op


def _select_thread_op(session_id: str, thread_id: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        thread = engine.select_thread(attachment, thread_id=thread_id)
        return ToolResponse.success(
            session_id,
            "selected",
            suggested_next_actions=["debug.read_registers", "debug.backtrace"],
            data={
                "thread_id": thread.id,
                "thread": thread.model_dump(mode="json", by_alias=True, exclude_none=True),
            },
        )

    return op


def _register_debug_list_threads(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.list_threads",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    @_with_thread_bound
    async def debug_list_threads(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose gdbstub CPUs to list.")
        ],
    ) -> ToolResponse:
        """List the guest CPUs (gdbstub threads) on a live DebugSession. Requires contributor.

        The target must be stopped first (halt it with debug.interrupt or hit a breakpoint).

        data.threads holds up to {max_threads} rows, each an id (a positive integer string), gdb's
        target_id, details (QEMU puts the CPU number here: thread 2 is "CPU#1"), state, and the
        func and addr of the thread's innermost frame; data.truncated says whether rows were cut.
        data.current_thread_id is the thread gdb itself has current, which a stop moves.
        data.selected_thread_id appears once debug.select_thread has chosen one; reads then use it.
        """
        return await run_engine_op_with_resolver(
            pool, current_context(), session_id, runtime, _list_threads_op(session_id)
        )


def _register_debug_select_thread(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.select_thread",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_select_thread(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose CPU selection to set.")
        ],
        thread_id: Annotated[
            int,
            Field(
                description=(
                    "Thread id to select: a positive integer from debug.list_threads data.threads."
                )
            ),
        ],
    ) -> ToolResponse:
        """Select the guest CPU that later register and stack reads use. Requires contributor.

        The target must be stopped first (halt it with debug.interrupt or hit a breakpoint).
        Until the session ends, debug.read_registers, debug.backtrace and debug.read_frame read
        the selected thread, even after a stop makes another thread current in gdb. The selection
        belongs to the session, so every contributor on it shares it, and it cannot be cleared
        except by starting a new session. data.thread_id and data.thread report the selection.

        A thread_id that is not a positive integer fails with configuration_error (data.code
        bad_thread_id). An id gdb does not list fails with debug_attach_failure (data.code
        unknown_thread), as does a later read once the selected thread has gone away; call
        debug.list_threads and select again.
        """
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _select_thread_op(session_id, thread_id),
            audit=_op_audit("debug.select_thread", thread_id=thread_id),
        )
