"""MCP dispatch-boundary middleware: denial audit (ADR-0062 §5) + telemetry (ADR-0090 §5).

`require_role`'s **member-over-reach** site raises :class:`~kdive.security.authz.rbac.RoleDenied`
(the dedicated discriminator, not the base :class:`~kdive.security.authz.rbac.AuthorizationError`
the non-member site keeps). :class:`DenialAuditMiddleware` is the single tool-dispatch
boundary that catches **`RoleDenied` specifically**, writes one guard-exempt `audit_log`
denial row (object NULL, reserved bare ``transition='denied'``, ``project`` from the
exception), and returns the uniform authorization-denied envelope. Catching the
``AuthorizationError`` base instead would double-write
``require_platform_role`` denials and :class:`~kdive.security.authz.gate.DestructiveOpDenied`
(both already handled elsewhere); the non-member denial is also deliberately excluded to
avoid write-amplification (ADR-0043 §4 / ADR-0062 §5).

The same boundary also catches :class:`~kdive.security.authz.errors.ProjectMembershipDenied`
(raised by ``require_project`` when the caller **names** a project they are not a member of) and
returns the **same** authorization-denied envelope **without** auditing — unifying the named-scope
membership denial onto exit 3 (ADR-0098), superseding ADR-0020 §4's "raise" for that surface. It
is the non-member case, so it inherits the same no-write-amplification exclusion; only the
*subclass* is caught, so a bare :class:`~kdive.security.authz.errors.AuthError` authentication
failure still propagates unchanged.

:class:`TelemetryMiddleware` is the per-request instrumentation seam (ADR-0090 §5): a span
per MCP tool call plus per-tool RED metrics (request rate, error count, duration
histogram). Labels are restricted to the allowlist (``tool``/``outcome``) so no
tenant/principal identifier becomes a free-cardinality label; secret values that reach a
span attribute or exception event are scrubbed by the redacting span exporter on export.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from math import isfinite
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.authz.errors import ProjectMembershipDenied
from kdive.security.authz.rbac import RoleDenied

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, Meter
    from opentelemetry.trace import Tracer
    from psycopg_pool import AsyncConnectionPool

_log = logging.getLogger(__name__)

#: Histogram bucket bounds (seconds) for per-tool request duration (the "D" in RED).
_DURATION_BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_DROP_ARGUMENT = object()


def _current_agent_session() -> str | None:
    """Read the in-flight request's ``agent_session`` from the verified token."""
    return current_context().agent_session


def _json_argument(value: object) -> object:
    """Return a JSON-native copy of ``value``, or ``_DROP_ARGUMENT`` if it is not safe."""
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else _DROP_ARGUMENT
    if isinstance(value, list):
        values: list[object] = []
        for item in value:
            sanitized = _json_argument(item)
            if sanitized is _DROP_ARGUMENT:
                return _DROP_ARGUMENT
            values.append(sanitized)
        return values
    if isinstance(value, dict):
        values: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return _DROP_ARGUMENT
            sanitized = _json_argument(item)
            if sanitized is _DROP_ARGUMENT:
                return _DROP_ARGUMENT
            values[key] = sanitized
        return values
    return _DROP_ARGUMENT


def _audit_args_from_message(message: Any) -> dict[str, object]:
    """Extract the JSON-native MCP call arguments for denial-audit digesting."""
    raw = getattr(message, "arguments", None)
    if not isinstance(raw, dict):
        return {}
    args: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        sanitized = _json_argument(value)
        if sanitized is not _DROP_ARGUMENT:
            args[key] = sanitized
    return args


class DenialAuditMiddleware(Middleware):
    """Catch member-over-reach `RoleDenied` at the dispatch boundary and audit it.

    Args:
        pool: The shared async connection pool the denial row is written through (its own
            connection — the denial path runs after the tool's transaction has unwound).
        agent_session: A callable returning the in-flight ``agent_session`` (injected so
            the recording logic is unit-testable without a live request scope); defaults
            to reading it from the verified token via :func:`current_context`.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        agent_session: Callable[[], str | None] = _current_agent_session,
    ) -> None:
        self._pool = pool
        self._agent_session = agent_session

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one tool call; map (and, for member-over-reach, audit) a denial.

        Two denial types are caught and enveloped as ``authorization_denied`` (ADR-0098):

        * :class:`RoleDenied` (member-over-reach) — enveloped **and** audited (one denial row).
        * :class:`~kdive.security.authz.errors.ProjectMembershipDenied` (the caller named a
          project they are not a member of) — enveloped but **not** audited. The non-member
          case is deliberately excluded to avoid write-amplification on openly-callable reads
          (ADR-0043 §4); only the *subclass* is caught, so a bare :class:`AuthError`
          authentication failure still propagates.

        Every other exception (the base
        :class:`~kdive.security.authz.rbac.AuthorizationError` non-member denial,
        :class:`~kdive.security.authz.gate.DestructiveOpDenied`, and unrelated errors) propagates
        unaudited.
        """
        try:
            return await call_next(context)
        except RoleDenied as denial:
            tool = context.message.name
            args = _audit_args_from_message(context.message)
            try:
                await self._record(tool, denial, args=args)
            except Exception:
                _log.warning("failed to audit RoleDenied for tool %s", tool, exc_info=True)
            return ToolResponse.failure(tool, ErrorCategory.AUTHORIZATION_DENIED)
        except ProjectMembershipDenied:
            # Non-member naming a project: envelope only, never audit (no write-amplification).
            return ToolResponse.failure(context.message.name, ErrorCategory.AUTHORIZATION_DENIED)

    async def _record(
        self, tool: str, denial: RoleDenied, *, args: dict[str, object] | None = None
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await audit.record_denial(
                conn,
                event=audit.DenialEvent(
                    principal=denial.principal,
                    agent_session=self._agent_session(),
                    project=denial.project,
                    tool=tool,
                    args={} if args is None else args,
                    reason=str(denial),
                ),
            )


# The typed-profile tools (#451): FastMCP validates their ``ProvisioningProfile`` param at binding,
# so a malformed profile raises before the tool body. Each maps to the call argument that names the
# call's object, so the re-enveloped error carries the same object_id the body path would have.
_PROFILE_TOOL_ID_ARG: dict[str, str] = {
    "systems.define": "allocation_id",
    "systems.provision": "allocation_id",
    "systems.reprovision": "system_id",
}


class ProfileBindingMiddleware(Middleware):
    """Convert a binding-time profile ``ValidationError`` into the project envelope (ADR-0124).

    The typed ``profile`` param is ``ProvisioningProfile`` (``extra="forbid"``), so FastMCP
    validates and rejects a malformed profile at argument binding — before the tool body and before
    the ``_runtime_resolution`` catch that builds the envelope. Without this seam the caller would
    get a raw FastMCP ``ToolError`` instead of the uniform ``configuration_error`` envelope.

    Registered **innermost** of the three middlewares (after ``TelemetryMiddleware`` and
    ``DenialAuditMiddleware``), so it converts the binding error into a *returned* envelope inside
    the telemetry span — counted as a normal completion, matching a body-rejected bad profile. It
    acts only for the typed-profile tools (:data:`_PROFILE_TOOL_ID_ARG`); every other tool and every
    non-``ValidationError`` exception propagates unchanged, so role/membership denials still route
    through ``DenialAuditMiddleware``.
    """

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one call; re-envelope a typed-profile binding ``ValidationError``."""
        tool = context.message.name
        id_arg = _PROFILE_TOOL_ID_ARG.get(tool)
        if id_arg is None:
            return await call_next(context)
        try:
            return await call_next(context)
        except ValidationError as exc:
            envelope = self._envelope(tool, id_arg, context, exc)
            # The middleware short-circuits the tool body, so it must return the same ``ToolResult``
            # FastMCP builds from a tool's ``ToolResponse`` return — a bare ``ToolResponse`` has no
            # ``to_mcp_result`` and would raise at serialization. ``structured_content`` is the flat
            # envelope dict (ADR-0113), matching the swept output schema.
            return ToolResult(structured_content=envelope.model_dump(mode="json"))

    def _envelope(self, tool: str, id_arg: str, context: Any, exc: ValidationError) -> ToolResponse:
        """Build the ``configuration_error`` envelope for a binding error (reuses ADR-0123)."""
        arguments = getattr(context.message, "arguments", None)
        object_id = tool
        if isinstance(arguments, dict):
            value = arguments.get(id_arg)
            if isinstance(value, str):
                object_id = value
        error = CategorizedError(
            "invalid provisioning profile",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "errors": exc.errors(include_url=False, include_input=False, include_context=False),
            },
        )
        return ToolResponse.failure_from_error(object_id, error)


class TelemetryMiddleware(Middleware):
    """Emit a span + per-tool RED metrics for every MCP tool call (ADR-0090 §5).

    One span per call (``kind=SERVER``) carries only allowlisted labels — ``tool`` and
    ``outcome`` — never a tenant/principal identifier (ADR-0090 §4). On failure the
    exception is recorded as a span event (scrubbed of secrets by the redacting span
    exporter on export) and the original exception re-raised. RED metrics: a request
    counter, an error counter, and a duration histogram, all labelled by ``tool`` and
    ``outcome`` only.

    Args:
        tracer: The tracer (from the facade's :class:`TracerProvider`) spans are opened on.
        meter: The meter (from the facade's :class:`MeterProvider`) instruments are made on.
    """

    def __init__(self, *, tracer: Tracer, meter: Meter) -> None:
        self._tracer = tracer
        self._requests: Counter = meter.create_counter(
            "kdive.mcp.requests", unit="1", description="MCP tool calls dispatched."
        )
        self._errors: Counter = meter.create_counter(
            "kdive.mcp.request.errors", unit="1", description="MCP tool calls that failed."
        )
        self._duration: Histogram = meter.create_histogram(
            "kdive.mcp.request.duration",
            unit="s",
            description="MCP tool-call wall-clock duration.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Time and trace one tool call; record RED metrics; re-raise on failure."""
        tool = context.message.name
        started = time.perf_counter()
        with self._tracer.start_as_current_span(
            f"mcp.tool/{tool}", kind=SpanKind.SERVER, attributes={"tool": tool}
        ) as span:
            try:
                result = await call_next(context)
            except Exception as exc:
                self._finish(span, tool, "error", started)
                self._errors.add(1, {"tool": tool, "outcome": "error"})
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            self._finish(span, tool, "ok", started)
            return result

    def _finish(self, span: Any, tool: str, outcome: str, started: float) -> None:
        labels = {"tool": tool, "outcome": outcome}
        span.set_attribute("outcome", outcome)
        self._requests.add(1, labels)
        self._duration.record(time.perf_counter() - started, labels)
