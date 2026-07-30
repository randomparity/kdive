"""Per-connection doc-resource exposure middleware (#940).

Role-gates the doc-resource surface so an ``audience="operator"`` doc is neither listed
nor readable by a caller that holds no platform role. The audience of each doc has a single
source (``audience_by_uri``). The predicate keys on the platform-role axis
(``ctx.platform_roles`` non-empty), not the project-scoped ``Role.OPERATOR``, because the
operator docs describe platform tools (``ops.*``, accounting admin, audit). A strict
``platform_operator`` check is avoided because ``platform_admin`` does not imply
``platform_operator``, so it would hide the operator workflow from a platform admin.

Both paths are fail-closed for the gated subset: an auth error hides operator docs from the
listing and rejects an operator-doc read. Tools remain gated at invocation regardless, so
this is signpost-hygiene layered on top of the tool authorization boundary.

The read arm answers ``NotFoundError`` rather than an ``authorization_denied`` envelope
(ADR-0499). This plane has no envelope to return — ``ReadResourceResult`` carries only
``contents`` — and the two arms must agree: a read that admitted the doc exists would be an
existence oracle for what the listing withheld. FastMCP's own component-auth filter reaches
the same answer, returning ``None`` from ``_get_resource`` on an authorization failure
"consistent with list filtering". No denial is audited here; see ADR-0499 §3.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastmcp.exceptions import NotFoundError
from fastmcp.server.middleware import Middleware

from kdive.mcp.middleware.shared import request_context
from kdive.mcp.resources.registrar import audience_by_uri
from kdive.security.authz.errors import AuthError

_log = logging.getLogger(__name__)


def _caller_has_platform_role(ctx: Any) -> bool:
    """Return True when the caller holds any platform role."""
    return bool(getattr(ctx, "platform_roles", frozenset()))


def _is_elevated() -> bool:
    """Return whether the in-flight caller holds a platform role.

    Fail-closed: an auth error (no verified token) or any unexpected failure resolves to
    not-elevated, so an operator doc is never exposed on a degraded auth path.
    """
    try:
        return _caller_has_platform_role(request_context())
    except AuthError:
        return False
    except Exception:
        _log.warning(
            "doc-exposure role check failed; treating caller as non-elevated", exc_info=True
        )
        return False


class DocExposureMiddleware(Middleware):
    """Filter the doc-resource list and read by the caller's platform role."""

    async def on_list_resources(
        self, context: Any, call_next: Callable[[Any], Any]
    ) -> Sequence[Any]:
        """Drop ``audience="operator"`` resources for callers holding no platform role."""
        resources = await call_next(context)
        if _is_elevated():
            return resources
        audience = audience_by_uri()
        return [r for r in resources if audience.get(str(r.uri), "all") != "operator"]

    async def on_read_resource(self, context: Any, call_next: Callable[[Any], Any]) -> Any:
        """Answer a gated ``audience="operator"`` read as if the resource did not exist.

        The message is FastMCP's own ``Unknown resource: {uri!r}`` verbatim, and that is
        load-bearing rather than cosmetic: ``_read_resource_mcp`` interpolates the exception
        into the wire message (``f"Resource not found: {e}"``), so any text naming the gate
        would republish on ``resources/read`` exactly what :meth:`on_list_resources`
        withheld. Matching it byte for byte makes a gated doc and a never-registered URI
        indistinguishable to the caller (ADR-0499).
        """
        uri = str(context.message.uri)
        if audience_by_uri().get(uri, "all") == "operator" and not _is_elevated():
            raise NotFoundError(f"Unknown resource: {uri!r}")
        return await call_next(context)
