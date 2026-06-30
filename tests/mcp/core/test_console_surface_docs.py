"""The runs.get / artifacts.list wrapper docstrings document the console surface (#935).

The agent reads only the ``@app.tool`` wrapper docstring + ``Field`` text (CLAUDE.md: the wrapper
docstring is the agent-facing contract). These guards pin that the console refs, the Run-scoped
manifest, the System-scoped nature of the list, and the part-key naming are discoverable at call
time — the discoverability dimension of #935.
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


def _description(name: str) -> str:
    for tool in _TOOLS:
        if tool.name == name:
            return tool.description or ""
    raise AssertionError(f"tool {name} is not registered")


def test_runs_get_docstring_names_console_surface() -> None:
    desc = _description("runs.get")
    assert "console_artifacts" in desc, "runs.get must name the Run-scoped console manifest"
    assert "console_access" in desc
    assert "refs.console" in desc or 'refs["console"]' in desc or "`console`" in desc


def test_artifacts_list_docstring_documents_system_scope_and_naming() -> None:
    desc = _description("artifacts.list")
    assert "System-scoped" in desc, "artifacts.list must disclose it mixes all Runs/sessions"
    assert "console-part-" in desc, "artifacts.list must document the rotating-part key naming"
    assert "console-<run" in desc, "artifacts.list must document the per-Run boot-snapshot naming"
    assert "runs.get" in desc, "artifacts.list must point at runs.get for Run correlation"
