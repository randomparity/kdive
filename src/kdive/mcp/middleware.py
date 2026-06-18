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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.exposure import visible_tool_names
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import SHAPE_XOR_ERROR_TYPE
from kdive.mcp.tools._platform_auth import actor_for
from kdive.security import audit
from kdive.security.authz.errors import AuthError, ProjectMembershipDenied
from kdive.security.authz.rbac import AuthorizationError, RoleDenied
from kdive.security.usage import UsageEvent, record_usage

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, Meter
    from opentelemetry.trace import Tracer
    from psycopg_pool import AsyncConnectionPool

_log = logging.getLogger(__name__)

#: Histogram bucket bounds (seconds) for per-tool request duration (the "D" in RED).
_DURATION_BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_DROP_ARGUMENT = object()


class _ToolOutcome(StrEnum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"


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


def _loc_under(param: str) -> Callable[[ValidationError], bool]:
    """Predicate: every error location is under ``param`` (a binding failure for that param).

    FastMCP raises a binding ``ValidationError`` whose ``loc`` starts with the param name for a
    malformed typed param. Requiring *all* entries to start there distinguishes the binding failure
    from an unrelated ``ValidationError`` the tool body might surface, so only the former is
    re-enveloped.
    """

    def _predicate(exc: ValidationError) -> bool:
        errors = exc.errors()
        return bool(errors) and all(
            bool(err.get("loc")) and err["loc"][0] == param for err in errors
        )

    return _predicate


def _is_shape_xor_error(exc: ValidationError) -> bool:
    """Whether every error entry is the shape-XOR-custom validator error (#473, ADR-0132).

    The XOR rule is a model-level constraint JSON Schema cannot express, raised as a typed
    ``shape_xor_custom`` error so it is distinguishable from a field-level error on the same
    payload (a typo'd extra field, a bad ``resource.mode`` discriminator, a non-int ``vcpus``),
    which must keep FastMCP's per-field detail and is therefore re-raised, not converted.
    """
    errors = exc.errors()
    return bool(errors) and all(err.get("type") == SHAPE_XOR_ERROR_TYPE for err in errors)


def _profile_envelope(object_id: str, exc: ValidationError) -> ToolResponse:
    """Envelope a malformed typed-profile binding error (ADR-0124, reuses ADR-0123)."""
    error = CategorizedError(
        "invalid provisioning profile",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "errors": exc.errors(include_url=False, include_input=False, include_context=False),
        },
    )
    return ToolResponse.failure_from_error(object_id, error)


def _build_profile_envelope(object_id: str, exc: ValidationError) -> ToolResponse:
    """Envelope a malformed ``build_profile`` binding error (#482, mirrors ``_profile_envelope``).

    Uses the same ``"invalid build profile"`` message the in-body ``BuildProfile.parse`` failure
    surfaces (``profiles/build.py``), so the binding-path envelope's ``detail`` is byte-identical to
    the body path's for an equivalent malformed profile.
    """
    error = CategorizedError(
        "invalid build profile",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "errors": exc.errors(include_url=False, include_input=False, include_context=False),
        },
    )
    return ToolResponse.failure_from_error(object_id, error)


def _shape_xor_envelope(object_id: str, exc: ValidationError) -> ToolResponse:
    """Envelope a shape-XOR-custom binding error with a precise ``detail`` (#473, ADR-0132)."""
    both = any(err.get("ctx", {}).get("both") for err in exc.errors())
    detail = (
        "supplied both a shape and a custom size; supply exactly one sizing source "
        "(a shape, or the full {vcpus, memory_gb, disk_gb} triple)"
        if both
        else (
            "supplied neither a shape nor a full {vcpus, memory_gb, disk_gb} triple; "
            "supply exactly one sizing source"
        )
    )
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, detail=detail)


@dataclass(frozen=True, slots=True)
class _BindingConversion:
    """How to convert one tool's binding ``ValidationError`` into a returned envelope."""

    id_arg: str
    matches: Callable[[ValidationError], bool]
    build: Callable[[str, ValidationError], ToolResponse]


# Tools whose typed params FastMCP validates at argument binding (before the tool body). Each maps
# the binding error to the call argument that names the call's object — so the re-enveloped error
# carries the same object_id the body path would have — plus the predicate that recognises *its*
# binding failure and the envelope builder. A tool not listed here, or a ValidationError its
# predicate rejects, propagates unchanged so role/membership denials still route through
# ``DenialAuditMiddleware``.
_BINDING_CONVERSIONS: dict[str, _BindingConversion] = {
    "systems.define": _BindingConversion("allocation_id", _loc_under("profile"), _profile_envelope),
    "systems.provision": _BindingConversion(
        "allocation_id", _loc_under("profile"), _profile_envelope
    ),
    "systems.reprovision": _BindingConversion(
        "system_id", _loc_under("profile"), _profile_envelope
    ),
    "runs.create": _BindingConversion(
        "system_id", _loc_under("build_profile"), _build_profile_envelope
    ),
    "allocations.request": _BindingConversion("project", _is_shape_xor_error, _shape_xor_envelope),
}


class BindingErrorMiddleware(Middleware):
    """Convert a binding-time ``ValidationError`` into the uniform envelope (ADR-0124, ADR-0132).

    FastMCP validates a tool's typed params at argument binding — before the tool body and before
    any in-body catch that builds the envelope — so a malformed typed param (a bad provisioning
    ``profile``, or an ``allocations.request`` payload that violates the shape-XOR-custom rule)
    raises a ``pydantic.ValidationError`` the caller would otherwise see as a raw FastMCP
    ``ToolError``. This seam re-envelopes it as the standard ``configuration_error`` response.

    Registered **innermost** of the three middlewares (after ``TelemetryMiddleware`` and
    ``DenialAuditMiddleware``), so it converts the binding error into a *returned* envelope inside
    the telemetry span — counted as a normal completion, matching a body-rejected bad input. It acts
    only for the tools in :data:`_BINDING_CONVERSIONS`, and only for the binding error its
    per-tool predicate recognises (a field-level error on the same payload is re-raised so FastMCP's
    per-field detail is preserved); every other tool and every non-``ValidationError`` exception
    propagates unchanged.
    """

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one call; re-envelope a recognised binding ``ValidationError``."""
        conversion = _BINDING_CONVERSIONS.get(context.message.name)
        if conversion is None:
            return await call_next(context)
        try:
            return await call_next(context)
        except ValidationError as exc:
            if not conversion.matches(exc):
                # Not this tool's binding failure (e.g. a field-level error on the same payload,
                # or a ValidationError the body surfaced) — propagate, never mislabel it.
                raise
            object_id = _binding_object_id(context, conversion.id_arg)
            envelope = conversion.build(object_id, exc)
            # The middleware short-circuits the tool body, so it must return the same ``ToolResult``
            # FastMCP builds from a tool's ``ToolResponse`` return — a bare ``ToolResponse`` has no
            # ``to_mcp_result`` and would raise at serialization. ``structured_content`` is the flat
            # envelope dict (ADR-0113), matching the swept output schema.
            return ToolResult(structured_content=envelope.model_dump(mode="json"))


def _binding_object_id(context: Any, id_arg: str) -> str:
    """The call's object id from ``id_arg``, falling back to the tool name."""
    arguments = getattr(context.message, "arguments", None)
    if isinstance(arguments, dict):
        value = arguments.get(id_arg)
        if isinstance(value, str):
            return value
    return str(context.message.name)


class ToolExposureMiddleware(Middleware):
    """Filter `list_tools` to the tools the connection's grants could invoke (ADR-0148, #506).

    Reads the connection's verified-token :class:`RequestContext` and drops any tool the
    caller could not invoke under any of its grants (the union of project roles + platform
    roles; any-of for dual-gated tools), shrinking the catalog the model must select from.

    Advisory and **fail-open**: list filtering is an accuracy aid, not a security control —
    execution-time RBAC remains the boundary. On a missing/invalid context or any internal
    error it returns the unfiltered catalog and logs, so tool discovery never breaks. (The
    token resolves in ``on_list_tools`` over the real HTTP transport; the in-memory
    transport carries none, so the end-to-end proof is the live-tier wire test.)
    """

    async def on_list_tools(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Sequence[Tool]:
        """Return only the advertised tools the in-flight connection may invoke."""
        tools: Sequence[Tool] = await call_next(context)
        try:
            ctx = current_context()
            visible = visible_tool_names(ctx, (tool.name for tool in tools))
        except AuthError:
            # Expected when list_tools runs without a verified token (doc generation, an
            # unauthenticated discovery probe). Advisory filter: advertise everything.
            _log.debug("no verified token in on_list_tools; advertising the full catalog")
            return tools
        except Exception:
            _log.warning("tool-exposure filter failed; advertising the full catalog", exc_info=True)
            return tools
        return [tool for tool in tools if tool.name in visible]


def _result_error_category(result: Any) -> str | None:
    """The envelope's ``error_category`` from a ``ToolResult`` or bare ``ToolResponse``.

    The normal dispatch path returns a ``ToolResult`` whose ``structured_content`` is the
    flat envelope dict (ADR-0113); ``DenialAuditMiddleware`` short-circuits with a bare
    ``ToolResponse``. Returns ``None`` for a success envelope or an unrecognised shape.
    """
    if isinstance(result, ToolResponse):
        return result.error_category
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        value = structured.get("error_category")
        return value if isinstance(value, str) else None
    return None


def _call_project(context: Any) -> str | None:
    """The call's ``project`` argument, if present as a non-empty string."""
    arguments = getattr(context.message, "arguments", None)
    if isinstance(arguments, dict):
        value = arguments.get("project")
        if isinstance(value, str) and value:
            return value
    return None


class UsageTrackingMiddleware(Middleware):
    """Record one best-effort ``tool_invocation`` row per call (ADR-0148, #506).

    Operational analytics for future workflow-scoped exposure decisions, not an audit
    trail. **Best-effort**: a recording failure (or a saturated pool past
    ``acquire_timeout``) is logged and swallowed — it never fails or delays the call (the
    :class:`DenialAuditMiddleware` precedent). Recording happens after ``call_next``
    returns, when the tool body has released its own pool connection, so the recorder's
    connection does not double-hold against the same call.

    Registered just inside :class:`TelemetryMiddleware` (which stays outermost), so it
    observes the final outcome after :class:`DenialAuditMiddleware` converts a
    role/membership denial to an ``authorization_denied`` envelope. A propagated
    :class:`~kdive.security.authz.rbac.AuthorizationError` (its
    :class:`~kdive.security.authz.gate.DestructiveOpDenied` subclass and the base
    non-member denial bubble past ``DenialAuditMiddleware`` rather than becoming an
    envelope) is classified ``denied`` too, so the denial signal is complete.

    Args:
        pool: The shared async connection pool the row is written through.
        acquire_timeout: Bounded wait (seconds) for a pool connection; on timeout the row
            is dropped (logged) rather than delaying response delivery under saturation.
    """

    def __init__(self, pool: AsyncConnectionPool, *, acquire_timeout: float = 1.0) -> None:
        self._pool = pool
        self._acquire_timeout = acquire_timeout

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one call, then record its outcome best-effort."""
        try:
            result = await call_next(context)
        except AuthorizationError:
            await self._record(context, _ToolOutcome.DENIED)
            raise
        except Exception:
            await self._record(context, _ToolOutcome.ERROR)
            raise
        await self._record(context, self._classify(result))
        return result

    @staticmethod
    def _classify(result: Any) -> _ToolOutcome:
        category = _result_error_category(result)
        if category is None:
            return _ToolOutcome.OK
        if category == ErrorCategory.AUTHORIZATION_DENIED.value:
            return _ToolOutcome.DENIED
        return _ToolOutcome.ERROR

    async def _record(self, context: Any, outcome: _ToolOutcome) -> None:
        tool = getattr(context.message, "name", "?")
        try:
            ctx = current_context()
            event = UsageEvent(
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=_call_project(context),
                tool=tool,
                outcome=outcome.value,
                actor=actor_for(ctx),
                client_id=ctx.client_id,
            )
            async with (
                self._pool.connection(timeout=self._acquire_timeout) as conn,
                conn.transaction(),
            ):
                await record_usage(conn, event)
        except Exception:
            _log.warning("usage recording failed for tool %s", tool, exc_info=True)


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
                self._finish(span, tool, _ToolOutcome.ERROR, started)
                self._errors.add(1, {"tool": tool, "outcome": _ToolOutcome.ERROR.value})
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            outcome = (
                _ToolOutcome.ERROR
                if _result_error_category(result) is not None
                else _ToolOutcome.OK
            )
            self._finish(span, tool, outcome, started)
            if outcome is _ToolOutcome.ERROR:
                self._errors.add(1, {"tool": tool, "outcome": outcome.value})
                span.set_status(Status(StatusCode.ERROR))
            return result

    def _finish(self, span: Any, tool: str, outcome: _ToolOutcome, started: float) -> None:
        labels = {"tool": tool, "outcome": outcome.value}
        span.set_attribute("outcome", outcome.value)
        self._requests.add(1, labels)
        self._duration.record(time.perf_counter() - started, labels)
