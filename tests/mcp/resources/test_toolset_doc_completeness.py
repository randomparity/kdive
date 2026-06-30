"""Every served toolset doc must name exactly the live tools in its namespace (#940).

A served ``docs/guide/toolsets/<ns>.md`` doc explains each tool by purpose. This guard ties
the doc to the live registry: a new tool added to a documented namespace, or a tool removed
from one, trips CI until the doc is corrected. The guard checks the tool name is *present*,
not the quality of the surrounding prose (that is a human review concern).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.mcp.resources.registrar import DOC_RESOURCES
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.test_tool_index import _verifier

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLSET_RE = re.compile(r"toolsets/(?P<ns>[a-z_]+)\.md$")
_AGENT_INDEX = _REPO_ROOT / "docs/guide/agent-index.md"
_BACKTICKED_TOOL_RE = re.compile(r"`([a-z_]+\.[a-z_]+)`")


def _live_tool_names() -> set[str]:
    """Return every registered tool name from a built app (no database access)."""
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app: FastMCP = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _run() -> set[str]:
        return {t.name for t in await app.list_tools()}

    return asyncio.run(_run())


def _served_toolset_docs() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for entry in DOC_RESOURCES:
        match = _TOOLSET_RE.search(entry.source)
        if match:
            out.append((match.group("ns"), _REPO_ROOT / entry.source))
    return out


def test_each_served_toolset_doc_names_exactly_its_namespace_tools() -> None:
    live = _live_tool_names()
    docs = _served_toolset_docs()
    for namespace, path in docs:
        body = path.read_text(encoding="utf-8")
        named = set(re.findall(rf"\b{namespace}\.[a-z_]+", body))
        expected = {tool for tool in live if tool.startswith(f"{namespace}.")}
        assert expected, f"{path.name} documents namespace {namespace!r} with no live tools"
        missing = expected - named
        stale = named - expected
        assert not missing, f"{path.name} omits live {namespace} tools: {sorted(missing)}"
        assert not stale, f"{path.name} names non-live {namespace} tools: {sorted(stale)}"


def test_agent_index_references_only_live_tools() -> None:
    """The index names a subset of tools across namespaces; each must be a live tool.

    Unlike the per-namespace docs, the index is not exact-set (it intentionally names only
    the first tool of each stage), so this is a stale-only check: an index that steers an
    agent to a renamed or removed tool trips CI.
    """
    live = _live_tool_names()
    live_namespaces = {name.split(".", 1)[0] for name in live}
    body = _AGENT_INDEX.read_text(encoding="utf-8")
    referenced = {
        token
        for token in _BACKTICKED_TOOL_RE.findall(body)
        if token.split(".", 1)[0] in live_namespaces
    }
    assert referenced, "no tool references found in agent-index.md (regex drift?)"
    stale = referenced - live
    assert not stale, f"agent-index.md references non-live tools: {sorted(stale)}"
