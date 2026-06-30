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
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastmcp.server.middleware import Middleware

from kdive.mcp.middleware.shared import request_context
from kdive.mcp.resources.registrar import audience_by_uri
from kdive.security.authz.errors import AuthError
from kdive.security.authz.rbac import AuthorizationError

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
        """Reject a read of an ``audience="operator"`` resource by a non-platform caller."""
        uri = str(context.message.uri)
        if audience_by_uri().get(uri, "all") == "operator" and not _is_elevated():
            raise AuthorizationError(f"{uri} requires a platform role")
        return await call_next(context)
