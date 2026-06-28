"""Completeness guard for ``TOOL_KEYWORDS`` in ``kdive.mcp.tool_index``.

Every key in ``TOOL_KEYWORDS`` must be a live registered tool name so the index
never silently accumulates stale entries (mirror of the ``CLASSIFIED_TOOLS`` guard
in ``tests/mcp/core/test_app.py``).
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.mcp.tool_index import TOOL_KEYWORDS
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
            "Add it to NAMESPACE_TOC in src/kdive/mcp/tool_index.py."
        )
    assert "tools.search" in text, "instructions must mention tools.search"
    assert "tools.invoke" in text, "instructions must mention tools.invoke"
