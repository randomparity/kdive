"""Unit tests for the agent-smoke surface adapter's reachability resolution (#2523).

Unmarked, so they run in the default suite and the PR gate. The gated ``agent_smoke`` test
drives :class:`AppSurface` against the real built app, but that tier is deliberately outside
the gate (ADR-0411) — so the branching and envelope parsing :meth:`AppSurface.reachable` gained
when the tier was re-aimed at the gateway contract would otherwise have no guarded home.

A stub stands in for the app: these assert what the adapter does with what ``list_tools`` and
``tools.search`` return, not what the server returns. ``test_walker.py`` stubs the adapter away
in turn, and the gated tier joins the two against the real surface.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast

from fastmcp import FastMCP

from tests.smoke.agent_smoke.surface import AppSurface


@dataclass
class _StubTool:
    name: str


@dataclass
class _StubResult:
    structured_content: dict[str, Any]


@dataclass
class _StubApp:
    """The two app methods :meth:`AppSurface.reachable` drives, plus a call log.

    ``registered`` is what a search resolves against; ``envelope`` overrides the whole search
    response, for the shapes a registry cannot express.
    """

    advertised: list[str] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    envelope: dict[str, Any] | None = None
    searches: list[dict[str, Any]] = field(default_factory=list)
    list_calls: int = 0

    async def list_tools(self) -> list[_StubTool]:
        self.list_calls += 1
        return [_StubTool(name) for name in self.advertised]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _StubResult:
        assert name == "tools.search"
        self.searches.append(arguments)
        if self.envelope is not None:
            return _StubResult(self.envelope)
        wanted = set(arguments["names"])
        matches = [{"name": n} for n in self.registered if n in wanted]
        return _StubResult({"data": {"matches": matches, "unknown_names": sorted(wanted)}})


def _surface(app: _StubApp) -> AppSurface:
    return AppSurface(cast(FastMCP, app))  # structural stand-in for the served app


def _reachable(app: _StubApp, name: str) -> bool:
    return asyncio.run(_surface(app).reachable(name))


def test_advertised_tool_is_reachable_without_a_search() -> None:
    """The short-circuit: a name in tools/list needs no gateway lookup."""
    app = _StubApp(advertised=["tools.search", "runs.create"])

    assert _reachable(app, "runs.create")
    assert app.searches == []


def test_unadvertised_but_registered_tool_is_reachable_through_the_gateway() -> None:
    """The clip narrows what is advertised, not what is callable (ADR-0268)."""
    app = _StubApp(advertised=["tools.search"], registered=["systems.teardown"])

    assert _reachable(app, "systems.teardown")
    assert app.searches == [{"names": ["systems.teardown"]}]


def test_unknown_name_is_not_reachable() -> None:
    app = _StubApp(advertised=["tools.search"], registered=["systems.teardown"])

    assert not _reachable(app, "refs.latest_console")


def test_a_match_under_another_name_does_not_make_the_token_reachable() -> None:
    """Only ``name`` itself counts — a neighbouring hit is not the tool the doc named.

    ``names`` mode is exact, but the adapter keeps its own equality check so a mode change, or
    the case-insensitive matching the field documents, cannot quietly turn a backticked
    response field into a "reachable tool".
    """
    app = _StubApp(
        advertised=["tools.search"],
        envelope={"data": {"matches": [{"name": "allocations.release"}]}},
    )

    assert not _reachable(app, "systems.teardown")


def test_token_outside_the_name_bound_is_not_reachable_and_issues_no_search() -> None:
    """``tools.search`` rejects a name over 128 chars, and no tool carries one either."""
    app = _StubApp(advertised=["tools.search"])

    assert not _reachable(app, "x" * 129)
    assert not _reachable(app, "")
    assert app.searches == []


def test_the_advertised_catalog_is_fetched_once_per_surface() -> None:
    """Each list_tools runs the whole middleware chain; a walk asks on nearly every token."""
    app = _StubApp(advertised=["tools.search"], registered=["systems.teardown"])
    surface = _surface(app)

    async def walk_like() -> None:
        for name in ("tools.search", "systems.teardown", "refs.latest_console"):
            await surface.reachable(name)
        await surface.tool_names()

    asyncio.run(walk_like())

    assert app.list_calls == 1


def test_an_empty_envelope_leaves_the_token_unreachable() -> None:
    """A response shape the adapter cannot read is a dead end, not a reachable tool."""
    app = _StubApp(advertised=["tools.search"], envelope={})

    assert not _reachable(app, "systems.teardown")
