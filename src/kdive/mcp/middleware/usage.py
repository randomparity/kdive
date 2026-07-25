"""Per-call usage recording and outcome classification."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp.server.middleware import Middleware
from opentelemetry import metrics
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.shared import (
    META_TOOLS,
    ToolOutcome,
    request_context,
    result_error_category,
)
from kdive.mcp.platform_auth import actor_for
from kdive.security.authz.rbac import AuthorizationError
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.usage import UsageEvent, digest_args, record_usage

_log = logging.getLogger(__name__)

# ADR-0148 keeps the swallow — best-effort recording must never fail a tool call — but a
# swallowed write is a *lost row*, and a WARNING is invisible to anything scraping /metrics.
# Counting it makes the loss rate a signal an operator can alert on (ADR-0449). Named and
# scoped like the exposure middleware's fail-open counters, its nearest precedent.
#
# No attributes, also matching that precedent. The obvious label would be the tool name, but
# it is the raw name off the client's `tools/call`: FastMCP resolves the tool *inside*
# `call_next`, so an unknown one reaches the `except` branch below with arbitrary content,
# and the SDK enforces no cardinality limit. Labelling would let one authenticated client
# grow the metric store without bound precisely while recording is already failing. The tool
# name is in the WARNING beside this, and in `tool_invocation.tool` for the calls that land.
_RECORDING_FAILURES = metrics.get_meter("kdive.mcp").create_counter(
    "kdive_mcp_usage_recording_failures",
    description="tool_invocation rows dropped by best-effort usage recording (ADR-0148)",
)


def _call_arguments(context: Any) -> Mapping[str, object] | None:
    """The call's argument mapping, if the transport carried one."""
    arguments = getattr(context.message, "arguments", None)
    return arguments if isinstance(arguments, Mapping) else None


def _call_project(context: Any) -> str | None:
    """The call's ``project`` argument, if present as a non-empty string."""
    arguments = _call_arguments(context)
    if arguments is not None:
        value = arguments.get("project")
        if isinstance(value, str) and value:
            return value
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
        """Dispatch one call, then record its outcome best-effort."""
        if getattr(context.message, "name", "?") in META_TOOLS:
            return await call_next(context)
        try:
            result = await call_next(context)
        except AuthorizationError:
            await self._record(context, ToolOutcome.DENIED)
            raise
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
        except Exception:
            # Log first, then count, and never let the counting be what fails the call:
            # ADR-0148's swallow is unconditional, so the instrument observing it must not
            # outrank the failure it observes.
            _log.warning("usage recording failed for tool %s", tool, exc_info=True)
            with contextlib.suppress(Exception):
                _RECORDING_FAILURES.add(1)
