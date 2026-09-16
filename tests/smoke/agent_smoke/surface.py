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
from kdive.mcp.tools.gateway import _NAME_LEN_MAX

#: The project the walked agent holds grants on.
WALK_PROJECT = "proj-a"

#: The walked agent's token subject.
WALK_SUBJECT = "agent-smoke-walker"

#: The walk's grants. ``admin`` because the served index advertises admin-scoped tools
#: (``systems.teardown``) in its wind-down stage: a lesser role would hide them behind RBAC and
#: the walk could not tell "the surface does not serve this" — the stall class this tier exists
#: to detect — from "this caller may not see it". Execution-time RBAC is tested elsewhere.
#:
#: Project grants only: no platform role is held, so platform-scoped tools are deliberately
#: outside the walked surface and a golden-path stage that came to name one would stall. That
#: is a real signal for this tier — today's index names none.
WALK_ROLE = "admin"


def agent_claims() -> dict[str, Any]:
    """The verified-token claims of the agent this tier walks as.

    Carries no ``azp``, so ``resolve_exposure_profile`` resolves the gateway profile rather
    than the operator CLI's direct profile — the identity whose ``tools/list`` the gateway
    clips to ``CORE_TOOLS``.
    """
    return _build_claims(
        subject=WALK_SUBJECT,
        audience=AUDIENCE,
        projects=[WALK_PROJECT],
        roles={WALK_PROJECT: WALK_ROLE},
        platform_roles=None,
        agent_session="agent-smoke",
    )


@contextmanager
def _as_agent() -> Iterator[None]:
    """Run the block with the walk's agent token in the request context.

    ``client_id`` here is the SDK's own field and nothing downstream reads it: profile
    resolution takes the caller's ``azp``/``client_id`` from the *claims*
    (``security/authz/context.py``), which :func:`agent_claims` deliberately omits.
    """
    token = AccessToken(
        token="agent-smoke",  # pragma: allowlist secret
        client_id=WALK_SUBJECT,
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
        self._advertised: frozenset[str] | None = None

    async def tool_names(self) -> frozenset[str]:
        """The advertised catalog, fetched once: a walk asks for it on nearly every token.

        Each ``list_tools`` runs the whole middleware chain — RBAC classification, profile
        resolution, provider-schema projection over the registry — and the served catalog does
        not change under a walk, so re-deriving it per token buys nothing.
        """
        if self._advertised is None:
            with _as_agent():
                self._advertised = frozenset(t.name for t in await self._app.list_tools())
        return self._advertised

    async def reachable(self, name: str) -> bool:
        """Whether an agent can reach ``name`` — advertised, or found through the gateway.

        The clip narrows what is *advertised*, not what is *callable*: everything else stays
        reachable via ``tools.search`` + ``tools.invoke`` (ADR-0268). Discovery uses the
        search's exact ``names`` mode, which is documented for a name the caller was handed by
        a guide — precisely this caller. It skips ranking, applies the same RBAC filter, and
        reports a name no visible tool carries in ``data.unknown_names`` rather than by
        raising, so a backticked token that is not a tool name (a ref, a response field, a
        param) resolves to False without depending on how a fuzzy query happens to rank.
        """
        if name in await self.tool_names():
            return True
        if not 0 < len(name) <= _NAME_LEN_MAX:
            return False  # outside the field's own bounds, so no tool carries it
        with _as_agent():
            result = await self._app.call_tool("tools.search", {"names": [name]})
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
