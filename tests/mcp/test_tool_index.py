"""Completeness guard for ``TOOL_KEYWORDS`` in ``kdive.mcp.schema.tool_index``.

Every key in ``TOOL_KEYWORDS`` must be a live registered tool name so the index
never silently accumulates stale entries (mirror of the ``CLASSIFIED_TOOLS`` guard
in ``tests/mcp/core/test_app.py``).
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.mcp.schema.tool_index import TOOL_KEYWORDS
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER


def _verifier() -> JWTVerifier:
    kp = RSAKeyPair.generate()
    return JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)


def _built_app() -> FastMCP:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    return build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())


def _registered_tool_names() -> set[str]:
    app = _built_app()

    async def _run() -> set[str]:
        return {t.name for t in await app.list_tools()}

    return asyncio.run(_run())


def _registered_tool_names_from(app: FastMCP) -> set[str]:
    async def _run() -> set[str]:
        return {t.name for t in await app.list_tools()}

    return asyncio.run(_run())


def test_tool_keywords_keys_are_live_tool_names() -> None:
    """Every key in TOOL_KEYWORDS is a live registered tool name; no stale entries."""
    registered = _registered_tool_names()
    stale = sorted(set(TOOL_KEYWORDS) - registered)
    assert not stale, (
        f"TOOL_KEYWORDS has stale entries (not in live registry): {stale}\n"
        "Remove or rename them so the index stays in sync with the registered tools."
    )


def test_instructions_cover_every_live_namespace() -> None:
    """build_instructions() mentions every live namespace and the gateway tools."""
    app = _built_app()
    text = app.instructions or ""
    live_ns = {name.split(".")[0] for name in _registered_tool_names_from(app)}
    for ns in live_ns:
        assert ns in text, (
            f"Namespace {ns!r} is missing from server instructions.\n"
            "Add it to NAMESPACE_TOC in src/kdive/mcp/schema/tool_index.py."
        )
    assert "tools.search" in text, "instructions must mention tools.search"
    assert "tools.invoke" in text, "instructions must mention tools.invoke"


# The false-when-off claim: the gateway-primary framing that is only accurate when the
# gateway is actually filtering the catalog down to the core set (#1034).
_GATEWAY_PRIMARY_CLAIM = "Only a small set of core tools are listed directly"


def test_instructions_point_at_the_agent_index() -> None:
    """Both instruction variants name the agent-index doc resource as the entry point."""
    from kdive.mcp.schema.tool_index import build_instructions

    for enabled in (False, True):
        assert "resource://kdive/docs/guide/agent-index.md" in build_instructions(enabled)


def test_instructions_both_modes_cover_namespaces_and_gateway_tools() -> None:
    """Every namespace and both gateway tools appear whether the gateway is on or off."""
    from kdive.mcp.schema.tool_index import NAMESPACE_TOC, build_instructions

    for enabled in (False, True):
        text = build_instructions(enabled)
        for ns in NAMESPACE_TOC:
            assert ns in text, f"namespace {ns!r} missing when gateway_enabled={enabled}"
        assert "tools.search" in text
        assert "tools.invoke" in text


def test_instructions_gateway_off_do_not_claim_gateway_primary() -> None:
    """With the gateway off (default), instructions must not claim the gateway is primary.

    Regression for #1034: the gateway is off by default, so every tool is listed
    directly; the old text falsely asserted the opposite.
    """
    from kdive.mcp.schema.tool_index import build_instructions

    text = build_instructions(gateway_enabled=False)
    assert _GATEWAY_PRIMARY_CLAIM not in text, (
        "gateway-off instructions must not claim only core tools are listed directly"
    )
    assert "mcp__kdive__" in text, "gateway-off instructions must name the direct tool surface"


def test_instructions_gateway_on_describe_gateway_first() -> None:
    """With the gateway on, instructions describe the gateway-first discovery pattern."""
    from kdive.mcp.schema.tool_index import build_instructions

    assert _GATEWAY_PRIMARY_CLAIM in build_instructions(gateway_enabled=True)


def test_default_app_instructions_do_not_claim_gateway_primary() -> None:
    """The assembled app defaults to gateway-off instructions (proves the wiring, #1034)."""
    app = _built_app()
    assert _GATEWAY_PRIMARY_CLAIM not in (app.instructions or "")


# The mis-scoped clause from #1248: framing the gateway as "for hosts without lazy tool
# loading" tells a lazy-loading client that materialises only a subset of the ~100 tools
# (and may never bind tools.invoke) that the escape hatch does not apply to it.
_MISSCOPED_CLAUSE = "without lazy tool loading"


def test_instructions_gateway_off_do_not_exclude_lazy_hosts() -> None:
    """Gateway-off instructions must not scope the gateway to non-lazy hosts (#1248).

    A lazy-loading client that materialises only a subset of the catalog may never bind
    ``tools.invoke``; the always-delivered instructions must point it at the gateway as
    the fallback, not tell it the gateway is only for hosts without lazy loading.
    """
    from kdive.mcp.schema.tool_index import build_instructions

    text = build_instructions(gateway_enabled=False)
    assert _MISSCOPED_CLAUSE not in text, (
        "gateway-off instructions must not scope the gateway to hosts "
        "'without lazy tool loading'; lazy hosts that materialise a subset need it too"
    )
    assert "tools.invoke" in text and "tools.search" in text
