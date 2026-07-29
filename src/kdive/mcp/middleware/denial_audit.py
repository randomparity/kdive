"""Authorization-denial audit middleware (ADR-0062, ADR-0098, ADR-0486)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from math import isfinite
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.middleware.shared import REENTRANT_TOOLS, request_context
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.authz.errors import ProjectMembershipDenied
from kdive.security.authz.rbac import RoleDenied

_log = logging.getLogger(__name__)
_DROP_ARGUMENT = object()

# The two denial classes this boundary owns. `RoleDenied` is audited and names the role it
# needed; `ProjectMembershipDenied` is enveloped role-lessly and never audited. Every other
# `AuthorizationError` — `require_platform_role`, `DestructiveOpDenied` — is audited by its own
# handler and must pass through here untouched (ADR-0062 §5, ADR-0043 §4).
type _Denial = RoleDenied | ProjectMembershipDenied
_DENIAL_TYPES = (RoleDenied, ProjectMembershipDenied)


def _wrapped_denial(exc: ToolError) -> _Denial | None:
    """The denial FastMCP wrapped in ``exc``, or None when it wrapped anything else.

    FastMCP builds the middleware chain strictly *outside* the ``try`` that turns a tool
    exception into ``ToolError`` (``fastmcp.server.server.FastMCP.call_tool``: the
    ``run_middleware=True`` branch re-enters with ``run_middleware=False``, and only that inner
    branch runs the tool and re-raises ``ToolError(...) from e``). So on every real dispatch
    path — direct ``call_tool``, the ``tools.invoke`` gateway, and the SDK ``tools/call``
    handler — a denial reaches this middleware demoted to ``__cause__`` (ADR-0486).

    Only the *immediate* cause is inspected, matching ``gateway.py``'s and
    ``binding_errors.py``'s existing ``ToolError.__cause__`` unwraps. Walking the chain would
    resurrect a denial a handler deliberately converted with ``raise Other from denial``, and a
    second wrapper layer cannot carry a denial anyway: the only way to nest one is for a
    ``ToolError`` to escape an inner chain, and the arms below envelope every denial before it
    can.
    """
    cause = exc.__cause__
    return cause if isinstance(cause, _DENIAL_TYPES) else None


def _denied_result(tool: str, missing: RoleDenied | None) -> ToolResult:
    """The ``authorization_denied`` envelope, in the shape the transport can serialize.

    A bare ``ToolResponse`` returned from a middleware reaches
    ``mcp_operations._call_tool_mcp``'s ``result.to_mcp_result()``, which only ``ToolResult``
    implements — over the wire that is an ``AttributeError``, not a denial envelope. Wrapping
    is what makes this short-circuit survive the transport (ADR-0486).

    ``missing`` names the role the caller lacked, and is **required but nullable** so that the
    role-less arm states its answer rather than omitting it — the shape ADR-0490 §4 asks of a
    forwarding helper, and what keeps a future arm from dropping the role by default. It is set
    for ``RoleDenied`` only: that fires at ``require_role``'s rank-below site, so the caller is
    a member and the role is safe to name (ADR-0490). ``None`` is the non-member
    ``ProjectMembershipDenied``, where no role would have helped and naming one would confirm
    the project exists (ADR-0123).
    """
    envelope = ToolResponse.denied(
        tool, missing_roles=() if missing is None else [missing.required]
    )
    return ToolResult(structured_content=envelope.model_dump(mode="json"))


def _current_agent_session() -> str | None:
    """Read the in-flight request's ``agent_session`` from the verified token."""
    return request_context().agent_session


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
    """Catch member-over-reach ``RoleDenied`` at the dispatch boundary and audit it."""

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
        """Dispatch one tool call and map authorization denials to envelopes.

        A denial arrives in either of two shapes, and both are handled: wrapped in FastMCP's
        ``ToolError`` (every real dispatch path — see :func:`_wrapped_denial`) or raised
        directly (a caller that drives this middleware itself). Anything else, including a
        ``ToolError`` over an ordinary failure, propagates unchanged.
        """
        try:
            return await call_next(context)
        except ToolError as exc:
            denial = _wrapped_denial(exc)
            if denial is None:
                raise
            return await self._denied(context, denial)
        except (RoleDenied, ProjectMembershipDenied) as denial:
            return await self._denied(context, denial)

    async def _denied(self, context: Any, denial: _Denial) -> ToolResult:
        """Audit the denial where ADR-0062 §5 requires it, and envelope it either way.

        Only the member over-reach is audited. The non-member case is deliberately excluded to
        avoid write-amplification on openly-callable reads (ADR-0043 §4, ADR-0098). The audit
        write is best-effort: a failure is logged and swallowed so it can never turn a denial
        into a server error (ADR-0148).
        """
        tool = context.message.name
        if not isinstance(denial, RoleDenied):
            return _denied_result(tool, None)
        args = _audit_args_from_message(context.message)
        try:
            await self._record(tool, denial, args=args)
        except Exception:
            _log.warning("failed to audit RoleDenied for tool %s", tool, exc_info=True)
        # The funnel for the ~40 require_role sites that do not catch locally: without the
        # named role the majority of the surface would deny without saying what it required
        # (ADR-0490, whose coverage this boundary is what makes live).
        return _denied_result(tool, denial)

    async def _record(
        self, tool: str, denial: RoleDenied, *, args: dict[str, object] | None = None
    ) -> None:
        # Ordering-defensive, and unreachable — as a consequence of the boundary above, not as a
        # standing fact (ADR-0485 §2, ADR-0486). This middleware runs in the re-entered inner
        # chain too, and the inner instance *returns* an enveloped denial rather than re-raising,
        # so no denial can reach an outer instance to be audited a second time under the
        # dispatcher's name. Before ADR-0486 the inner instance enveloped nothing and the arm was
        # unreachable for the opposite reason: the denial reached the outer instance as a
        # ToolError and matched no clause there either. Kept because it costs nothing and a
        # future reorder, or a re-raise on this path, would make that hazard real. NOT
        # de-duplication: unlike the usage and telemetry planes, there is no second row to remove.
        if tool in REENTRANT_TOOLS:
            return
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
