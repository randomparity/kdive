"""Adapt a built FastMCP app to the walker's normalized :class:`Surface` (#1370, ADR-0411).

The walker speaks in normalized strings (tool names, resource URIs and bodies, prompt names
and rendered text); this adapter maps those onto the served MCP surface a real agent reaches:
``list_tools`` / ``list_resources`` / ``read_resource`` / ``list_prompts`` / ``render_prompt``.

Every call runs under a verified agent-profile token (:func:`agent_claims`), because that is
what makes the served catalog the one an agent is actually given. Without a token in the
context ``ToolExposureMiddleware`` cannot resolve a profile, fails open, and advertises the
whole registry — so the tier walked the pre-``62fb4fa4d`` flat catalog instead of the
``CORE_TOOLS`` clip the gateway serves (ADR-0268; #2521 survey entry A1, fixed by #2523).

A read or render that *raises* is itself a stall — the agent asked the surface for something
it could not deliver — so the adapter converts a failure into an empty string, which the
walker records as a dead end. This is deliberate surfacing of the failure as a walk result,
not silent swallowing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastmcp import FastMCP
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
from mcp.server.auth.provider import AccessToken

from kdive.mcp.dev_harness import AUDIENCE, _build_claims

#: The project the walked agent holds grants on.
WALK_PROJECT = "proj-a"

#: The walk's grants. ``admin`` because the served index advertises admin-scoped tools
#: (``systems.teardown``) in its wind-down stage: a lesser role would hide them behind RBAC and
#: the walk could not tell "the surface does not serve this" — the stall class this tier exists
#: to detect — from "this caller may not see it". Execution-time RBAC is tested elsewhere.
WALK_ROLE = "admin"

#: ``tools.search``'s own per-call ceiling (``le=50`` on its ``limit`` field).
_SEARCH_LIMIT = 50


def agent_claims() -> dict[str, Any]:
    """The verified-token claims of the agent this tier walks as.

    Carries no ``azp``, so ``resolve_exposure_profile`` resolves the gateway profile rather
    than the operator CLI's direct profile — the identity whose ``tools/list`` the gateway
    clips to ``CORE_TOOLS``.
    """
    return _build_claims(
        subject="agent-smoke-walker",
        audience=AUDIENCE,
        projects=[WALK_PROJECT],
        roles={WALK_PROJECT: WALK_ROLE},
        platform_roles=None,
        agent_session="agent-smoke",
    )


@contextmanager
def _as_agent() -> Iterator[None]:
    """Run the block with the walk's agent token in the request context."""
    token = AccessToken(
        token="agent-smoke",  # pragma: allowlist secret
        client_id="agent-smoke-client",
        scopes=[],
        claims=agent_claims(),
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


class AppSurface:
    """The walker's :class:`~tests.smoke.agent_smoke.walker.Surface` over a built app."""

    def __init__(self, app: FastMCP) -> None:
        self._app = app

    async def tool_names(self) -> frozenset[str]:
        with _as_agent():
            return frozenset(tool.name for tool in await self._app.list_tools())

    async def reachable(self, name: str) -> bool:
        """Whether an agent can reach ``name`` — advertised, or found through the gateway.

        The clip narrows what is *advertised*, not what is *callable*: everything else stays
        reachable via ``tools.search`` + ``tools.invoke`` (ADR-0268). Discovery requires the
        search to return ``name`` itself, never merely a related hit, so a backticked token
        that is not a tool name (a ref, a response field, a param) still resolves to False
        even when the fuzzy search ranks neighbours for it.
        """
        if name in await self.tool_names():
            return True
        with _as_agent():
            try:
                result = await self._app.call_tool(
                    "tools.search", {"query": name, "limit": _SEARCH_LIMIT}
                )
            except Exception:  # a raised search is a dead end the walk must record
                return False
        data = (getattr(result, "structured_content", None) or {}).get("data") or {}
        return any(match.get("name") == name for match in data.get("matches") or ())

    async def resource_uris(self) -> frozenset[str]:
        with _as_agent():
            return frozenset(str(resource.uri) for resource in await self._app.list_resources())

    async def read(self, uri: str) -> str:
        try:
            with _as_agent():
                result = await self._app.read_resource(uri)
        except Exception:  # a raised read is a dead end the walk must record
            return ""
        content = result.contents[0].content
        return content if isinstance(content, str) else ""

    async def prompt_names(self) -> frozenset[str]:
        with _as_agent():
            return frozenset(prompt.name for prompt in await self._app.list_prompts())

    async def render(self, name: str) -> str:
        try:
            with _as_agent():
                result = await self._app.render_prompt(name, {})
        except Exception:  # a raised render is a dead end the walk must record
            return ""
        text = getattr(result.messages[0].content, "text", "")
        return text if isinstance(text, str) else ""
