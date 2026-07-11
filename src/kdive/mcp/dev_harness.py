"""MCP development harness helpers for live-stack and app-introspection tools (ADR-0044).

Reusable pieces for run-local scripts and integration tests:

* :func:`mint_token` obtains a bearer token from the mock-oauth2-server by driving its
  interactive-login authorization-code flow and posting a literal ``claims`` JSON object;
  the returned access token carries the nested-object ``roles`` claim and the
  ``platform_roles`` array claim (proven to flow into the access token, ADR-0044 Context).
* :class:`LiveStackClient` wraps :class:`fastmcp.Client`, parsing each tool result's
  structured output back into the project :class:`~kdive.mcp.responses.ToolResponse`.
* :func:`make_keypair` and :func:`mint` create local JWTs for in-memory app introspection.

The authorization-code flow (:class:`OidcIssuer`, :func:`_build_claims`,
:func:`_authorization_code`, :func:`_exchange_code`) lives in :mod:`kdive.cli.login`; this
harness re-exports those symbols so script and integration drivers share one flow.

This module imports no pytest symbols so it stays importable as a plain library.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Self

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import RSAKeyPair

import kdive.config as config
from kdive.cli.login import (
    _DEFAULT_AUDIENCE,
    _DEFAULT_CLIENT_ID,
    OidcIssuer,
    _authorization_code,
    _build_claims,
    _exchange_code,
)
from kdive.config.core_settings import OIDC_AUDIENCE, OIDC_ISSUER
from kdive.mcp.responses import ToolResponse

ISSUER = "https://idp.test.kdive"
AUDIENCE = _DEFAULT_AUDIENCE
OIDC_CLIENT_ID_ENV = "KDIVE_OIDC_CLIENT_ID"

__all__ = [
    "AUDIENCE",
    "ISSUER",
    "LiveStackClient",
    "LiveStackToolError",
    "OidcIssuer",
    "_authorization_code",
    "_build_claims",
    "_exchange_code",
    "make_keypair",
    "mint",
    "mint_token",
    "oidc_issuer_from_env",
]


class LiveStackToolError(RuntimeError):
    """A tool call returned an error result over the wire (e.g. a raised authz denial).

    fastmcp surfaces a handler that *raises* (rather than returning a :class:`ToolResponse`)
    as a tool-error ``CallToolResult`` (``is_error`` true, no ``structured_content``). The
    driver asserts the RBAC raised-path on this typed error rather than on an
    ``error_category`` (ADR-0045 §2).
    """

    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        self.message = message
        super().__init__(f"tool {tool!r} returned an error: {message}")


def _tool_error_text(result: object) -> str:
    """Best-effort human-readable text from a tool-error ``CallToolResult``."""
    content = getattr(result, "content", None)
    if content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            return text
    return "tool error"


def oidc_issuer_from_env() -> OidcIssuer:
    """Resolve the mock-OIDC issuer from the config snapshot.

    This keeps the test-side reader aligned with the CLI's ``kdive.config`` path while still
    honoring the test-only ``KDIVE_OIDC_CLIENT_ID`` override catalogued as external env.
    """
    base_url = config.get(OIDC_ISSUER)
    if not base_url:
        raise RuntimeError("KDIVE_OIDC_ISSUER is not set; cannot reach the mock-OIDC issuer")
    return OidcIssuer(
        base_url=base_url,
        audience=config.get(OIDC_AUDIENCE) or _DEFAULT_AUDIENCE,
        client_id=config.env_snapshot().get(OIDC_CLIENT_ID_ENV, _DEFAULT_CLIENT_ID),
    )


def mint_token(
    issuer: OidcIssuer,
    *,
    subject: str,
    projects: Sequence[str],
    roles: Mapping[str, str],
    platform_roles: Sequence[str] | None = None,
    agent_session: str | None = None,
    client_id: str | None = None,
) -> str:
    """Mint an access token from the mock-OIDC issuer carrying the kdive claims.

    Drives the issuer's interactive-login authorization-code flow: POST the login form
    with a literal ``claims`` JSON (nested-object ``roles`` + optional ``platform_roles``
    array), capture the ``code`` from the redirect, exchange it for the access token. The
    token validates through the server's real ``JWTVerifier`` (ADR-0044). ``client_id`` sets
    the OIDC ``azp`` claim so the boundary test can mint an ``operator-cli`` token.
    """
    claims = _build_claims(
        subject=subject,
        audience=issuer.audience,
        projects=projects,
        roles=roles,
        platform_roles=platform_roles,
        agent_session=agent_session,
        client_id=client_id,
    )
    code = _authorization_code(issuer, claims)
    return _exchange_code(issuer, code)


def make_keypair() -> RSAKeyPair:
    return RSAKeyPair.generate()


def mint(
    keypair: RSAKeyPair,
    *,
    subject: str = "user-1",
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    agent_session: str | None = "sess-1",
    projects: list[str] | None = None,
    roles: dict[str, str] | None = None,
    client_id: str | None = None,
    expires_in_seconds: int = 3600,
) -> str:
    """Mint a signed JWT carrying the kdive custom claims.

    ``roles`` is the per-project role map (``{"proj-a": "admin"}``) the
    ``roles_from_claims`` parser reads; omit it for a membership-only token. ``client_id``
    sets the OIDC ``azp`` claim (the operator-CLI client id) the actor map resolves; omit
    it for an agent token.
    """
    extra: dict[str, object] = {}
    if agent_session is not None:
        extra["agent_session"] = agent_session
    if projects is not None:
        extra["projects"] = projects
    if roles is not None:
        extra["roles"] = roles
    if client_id is not None:
        extra["azp"] = client_id
    return keypair.create_token(
        subject=subject,
        issuer=issuer,
        audience=audience,
        additional_claims=extra,
        expires_in_seconds=expires_in_seconds,
    )


class LiveStackClient:
    """A thin wrapper over :class:`fastmcp.Client` returning parsed envelopes (ADR-0044).

    ``call_tool`` parses ``CallToolResult.structured_content`` — a clean ``dict`` — back into
    the project :class:`ToolResponse`: a scalar tool's payload is the object dict, a
    ``list[ToolResponse]`` tool is wrapped as ``{"result": [...]}``. The constructor accepts an
    already-built client (the in-memory tier injects one over a probe app); :meth:`over_http`
    builds the streamable-HTTP + bearer client for the live tier.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @classmethod
    def over_http(cls, base_url: str, token: str) -> Self:
        """Build a streamable-HTTP client carrying ``token`` as the bearer."""
        transport = StreamableHttpTransport(
            url=base_url, headers={"Authorization": f"Bearer {token}"}
        )
        return cls(Client(transport))

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)

    async def list_tools(self) -> list[str]:
        """Return the registered tool names."""
        tools = await self._client.list_tools()
        return [tool.name for tool in tools]

    async def call_tool(self, name: str, **args: object) -> ToolResponse | list[ToolResponse]:
        """Call ``name`` and parse the structured output into ``ToolResponse``.

        Reads ``CallToolResult.structured_content`` — a clean ``dict`` (fastmcp 3.4.0). A
        ``list[ToolResponse]`` tool is wrapped by FastMCP as ``{"result": [<dict>, ...]}``,
        so a payload that is exactly a single ``result`` key holding a list parses to a list
        of envelopes; any other object is one envelope. ``CallToolResult.data`` is not used:
        it is a FastMCP-generated plain class (``Root``), not a pydantic model, so it has no
        ``model_dump``.

        A tool-error result (``is_error`` true — a handler that *raised*, e.g. an authz denial
        that surfaces as a raise rather than a ``ToolResponse``) raises
        :class:`LiveStackToolError` before the structured-content parse (ADR-0045 §2).

        ``raise_on_error=False`` is required: fastmcp's ``Client.call_tool`` otherwise raises
        its own ``fastmcp.exceptions.ToolError`` on an error result, defeating the typed
        ``LiveStackToolError`` wrapping the driver asserts on. Passing it returns the
        ``CallToolResult`` so the ``is_error`` branch below can re-raise the typed error.
        """
        result = await self._client.call_tool(name, args, raise_on_error=False)
        if getattr(result, "is_error", False):
            raise LiveStackToolError(name, _tool_error_text(result))
        payload = result.structured_content
        if payload is None:
            raise RuntimeError(f"tool {name!r} returned no structured content")
        inner = payload.get("result")
        if list(payload) == ["result"] and isinstance(inner, list):
            return [ToolResponse.model_validate(item) for item in inner]
        return ToolResponse.model_validate(payload)
