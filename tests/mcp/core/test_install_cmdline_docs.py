"""The runs.install cmdline Field + runs.boot docstring document cmdline iteration (ADR-0299).

The agent reads only the ``@app.tool`` wrapper docstring + ``Field`` text (CLAUDE.md: the wrapper
docstring is the agent-facing contract). These guards pin that ``runs.install`` exposes a
``cmdline`` override, enumerates the always-present/never-modifiable platform tokens, and that
``runs.boot`` no longer claims the cmdline is fixed at build time.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools import Tool
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _build_app() -> Any:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    keypair = make_keypair()
    verifier = JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)
    return build_app(pool, verifier=verifier, secret_registry=SecretRegistry())


_TOOLS = cast(list[Tool], asyncio.run(_build_app().list_tools()))


def _tool(name: str) -> Tool:
    for tool in _TOOLS:
        if tool.name == name:
            return tool
    raise AssertionError(f"tool {name} is not registered")


def _field_description(name: str, field: str) -> str:
    props = _tool(name).parameters.get("properties", {})
    assert field in props, f"{name} must expose the {field} parameter"
    return props[field].get("description", "")


def test_runs_install_exposes_cmdline_override() -> None:
    desc = _field_description("runs.install", "cmdline")
    assert "no rebuild" in desc.lower()
    assert "replace" in desc.lower()  # replaces the build-time extra


def test_runs_install_cmdline_enumerates_platform_tokens() -> None:
    # The agent must know exactly which args are always present and cannot be overridden.
    desc = _field_description("runs.install", "cmdline")
    for token in ("console=ttyS0", "root=/dev/vda", "crashkernel=256M", "nokaslr"):
        assert token in desc, f"cmdline Field must name the platform token {token}"


def test_runs_boot_doc_points_cmdline_iteration_at_install() -> None:
    desc = _tool("runs.boot").description or ""
    assert "fixed at build time" not in desc
    assert "runs.install" in desc
