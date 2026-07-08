"""Opt-in compact response envelope middleware (ADR-0314, #1035).

When KDIVE_COMPACT_RESPONSES is on, rebuild each tool result's envelope with the null/empty
defaulted fields omitted (recursively within ``items``), cutting per-call tokens. Registered
outermost in ``build_app`` so it observes the final result — a ``ToolResult`` (the normal tool
path and ``BindingErrorMiddleware``'s synthesized failures) or a bare ``ToolResponse``
(``DenialAuditMiddleware``'s ``authorization_denied`` short-circuit); both are compacted.
Default off: the result passes through unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from pydantic import ValidationError

from kdive.mcp.responses import ToolResponse
from kdive.mcp.verbosity import compact_responses_enabled

# The exact set of top-level envelope keys. A dict carrying any other key is not a ToolResponse
# dump and is passed through untouched — pydantic's default extra="ignore" would otherwise let a
# superset validate and silently drop its extra keys.
_ENVELOPE_FIELDS = frozenset(ToolResponse.model_fields)


class CompactResponseMiddleware(Middleware):
    """Omit null/empty defaulted envelope fields when KDIVE_COMPACT_RESPONSES is on (ADR-0314)."""

    async def on_call_tool(self, context: Any, call_next: Callable[[Any], Any]) -> Any:
        """Compact the tool result's envelope when the flag is on; otherwise pass it through."""
        result = await call_next(context)
        if not compact_responses_enabled():
            return result
        return _compact_result(result)


def _compact_result(result: Any) -> Any:
    """Return `result` with its envelope compacted, or unchanged when it is not an envelope.

    Handles both shapes a middleware chain can produce: a bare ``ToolResponse`` (a short-circuit
    such as ``DenialAuditMiddleware``) is dumped directly; a ``ToolResult`` whose
    ``structured_content`` is a dict of envelope keys is re-validated as a ``ToolResponse``.
    ``ToolResponse`` is ``extra="forbid"``, so a superset dict at any depth (top level *or* an
    ``items`` row) raises ``ValidationError`` and the whole result passes through untouched — the
    top-level subset check is a cheap fast-path for the common non-envelope case.
    ``model_dump(exclude_defaults=True)`` recurses into ``items`` and keeps every non-default
    failure field; rebuilding a ``ToolResult`` from only the compact ``structured_content``
    regenerates a matching ``content`` text block, so both wire copies shrink, and ``is_error`` /
    ``meta`` are carried through. Anything else (a non-envelope dict, a ``ValidationError``,
    non-dict content) is returned untouched — fail safe, never corrupt a response.
    """
    if isinstance(result, ToolResponse):
        return ToolResult(structured_content=result.model_dump(mode="json", exclude_defaults=True))
    if not isinstance(result, ToolResult):
        return result
    sc = result.structured_content
    if not isinstance(sc, dict) or not set(sc) <= _ENVELOPE_FIELDS:
        return result
    try:
        envelope = ToolResponse.model_validate(sc)
    except ValidationError:
        return result
    compact = envelope.model_dump(mode="json", exclude_defaults=True)
    return ToolResult(structured_content=compact, meta=result.meta, is_error=result.is_error)
