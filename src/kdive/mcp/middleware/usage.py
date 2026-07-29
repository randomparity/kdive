"""Per-call usage recording and outcome classification."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp.server.middleware import Middleware
from opentelemetry import metrics
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.shared import (
    REENTRANT_TOOLS,
    UNMETERED_TOOLS,
    ToolOutcome,
    request_context,
    result_error_category,
)
from kdive.mcp.platform_auth import actor_for
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.usage import UsageEvent, digest_args, record_usage

_log = logging.getLogger(__name__)

# The only plane that skips both sets (ADR-0485): a row is de-duplicated away for a
# re-entrant dispatcher, and declined outright for an unmetered tool because it is a per-call
# Postgres write on the highest-frequency agent call.
_UNRECORDED_TOOLS: frozenset[str] = REENTRANT_TOOLS | UNMETERED_TOOLS

# ADR-0148 keeps the swallow — best-effort recording must never fail a tool call — but a
# swallowed write is a *lost row*, and a WARNING is invisible to anything scraping /metrics.
# Counting it makes the loss rate a signal an operator can alert on (ADR-0449). Named and
# scoped like the exposure middleware's fail-open counters, its nearest precedent.
#
# Attributed only by `reason`, which is server-derived and two-valued. `_record`'s except
# clause covers the whole body — context lookup, digest, acquire, and the INSERT — so an
# unattributed count could not tell "the pool could not hand out a connection in time", the
# loss mode ADR-0449 defers a pool-sizing decision to, from a SQL or schema failure that
# argues for something else entirely.
#
# Deliberately *not* labelled by tool name, matching the exposure middleware's fail-open
# counters: that name is the raw string off the client's `tools/call` (FastMCP resolves the
# tool *inside* `call_next`, so an unknown one reaches the except branch with arbitrary
# content) and the SDK enforces no cardinality limit, so one authenticated client could grow
# the metric store without bound precisely while recording is already failing. The tool name
# is in the WARNING beside the increment, and in `tool_invocation.tool` for calls that land.
_RECORDING_FAILURES = metrics.get_meter("kdive.mcp").create_counter(
    "kdive_mcp_usage_recording_failures",
    description="tool_invocation rows dropped by best-effort usage recording (ADR-0148)",
)


def _failure_reason(exc: BaseException) -> str:
    """Classify a swallowed recording failure into a bounded, server-derived label."""
    return "pool_timeout" if isinstance(exc, PoolTimeout) else "other"


def _call_arguments(context: Any) -> Mapping[str, object] | None:
    """The call's argument mapping, if the transport carried one."""
    arguments = getattr(context.message, "arguments", None)
    return arguments if isinstance(arguments, Mapping) else None


def _project_in(mapping: Mapping[Any, Any]) -> str | None:
    """One mapping's ``project`` entry, if it is a non-empty string."""
    value = mapping.get("project")
    return value if isinstance(value, str) and value else None


def _call_project(context: Any) -> str | None:
    """The call's ``project`` argument, at the top level or one level into a payload.

    Read and list tools that take a typed request model carry the project one level down —
    under ``request`` for most of them, under ``target`` for ``accounting.usage`` — so a
    top-level-only lookup left every such row unattributed, including the denied ones
    ADR-0148 keeps the row for (#1644). Descending into each ``Mapping``-valued argument
    covers them all without a wrapper-key list that would drift as tools are added.

    One level only: deeper, a ``project`` field on some nested sub-model would be attributed
    as the call's project. The top level still wins, so a tool that names both is resolved by
    its own argument rather than by its payload's.

    Total over the argument mappings the transport delivers — plain ``dict`` objects decoded
    from the call's JSON — for which anything that is not a mapping carrying a non-empty
    string ``project`` yields ``None`` rather than raising. That matters because it runs
    inside ADR-0148's best-effort recorder, where a raise costs the whole row. It is not
    total over an arbitrary ``Mapping``: a ``get``/``values`` that raises would propagate,
    and nothing on this path can produce one.
    """
    arguments = _call_arguments(context)
    if arguments is None:
        return None
    top_level = _project_in(arguments)
    if top_level is not None:
        return top_level
    for value in arguments.values():
        if isinstance(value, Mapping):
            nested = _project_in(value)
            if nested is not None:
                return nested
    return None


class UsageTrackingMiddleware(Middleware):
    """Record one best-effort ``tool_invocation`` row per call.

    Each row carries an ``args_digest`` (ADR-0304): a stable SHA-256 over the call's
    *redacted* arguments, computed through the app-owned ``SecretRegistry`` so the digest
    can never diverge from the log/telemetry redaction contract. The ``Redactor`` is
    cached and rebuilt only when the registry version changes.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        secret_registry: SecretRegistry,
        acquire_timeout: float = 1.0,
    ) -> None:
        self._pool = pool
        self._acquire_timeout = acquire_timeout
        self._registry = secret_registry
        self._cached_version = -1
        self._redactor = Redactor(registry=secret_registry)

    def _current_redactor(self) -> Redactor:
        version = self._registry.version()
        if version != self._cached_version:
            self._redactor = Redactor(registry=self._registry)
            self._cached_version = version
        return self._redactor

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one call, then record its outcome best-effort.

        A denial is classified from the **returned envelope**, never from an exception: FastMCP
        builds the middleware chain outside the branch that wraps a tool exception in
        ``ToolError``, so no ``AuthorizationError`` can arrive here unwrapped, and every denial
        class is enveloped before it reaches this middleware — by its own handler or by
        ``DenialAuditMiddleware`` (ADR-0486, ADR-0493). What still reaches ``except Exception``
        is a genuine fault, which ``error`` is the right outcome for.
        """
        if getattr(context.message, "name", "?") in _UNRECORDED_TOOLS:
            return await call_next(context)
        try:
            result = await call_next(context)
        except Exception:
            await self._record(context, ToolOutcome.ERROR)
            raise
        await self._record(context, self._classify(result))
        return result

    @staticmethod
    def _classify(result: Any) -> ToolOutcome:
        category = result_error_category(result)
        if category is None:
            return ToolOutcome.OK
        if category == ErrorCategory.AUTHORIZATION_DENIED:
            return ToolOutcome.DENIED
        return ToolOutcome.ERROR

    async def _record(self, context: Any, outcome: ToolOutcome) -> None:
        tool = getattr(context.message, "name", "?")
        try:
            ctx = request_context()
            event = UsageEvent(
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=_call_project(context),
                tool=tool,
                outcome=outcome.value,
                actor=actor_for(ctx),
                client_id=ctx.client_id,
                args_digest=digest_args(self._current_redactor(), _call_arguments(context)),
            )
            async with (
                self._pool.connection(timeout=self._acquire_timeout) as conn,
                conn.transaction(),
            ):
                await record_usage(conn, event)
        except Exception as exc:
            # Log first, then count, and never let the counting be what fails the call:
            # ADR-0148's swallow is unconditional, so the instrument observing it must not
            # outrank the failure it observes.
            _log.warning("usage recording failed for tool %s", tool, exc_info=True)
            with contextlib.suppress(Exception):
                _RECORDING_FAILURES.add(1, {"reason": _failure_reason(exc)})
