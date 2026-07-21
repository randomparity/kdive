"""Unit + mutation tests for the agent-smoke walker (#1370, ADR-0411).

Unmarked (they run in the default suite / PR gate), so the walker's stall detection is itself
guarded. A synthetic :class:`FakeSurface` lets each stall class be injected precisely: start
from a healthy surface that walks green, break exactly one thing, and assert exactly that
stall fires. The gated ``agent_smoke`` test drives the *real* surface; this guards the logic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

from tests.smoke.agent_smoke.walker import (
    AGENT_INDEX_URI,
    GATEWAY_TOOLS,
    NAMED_PROMPTS,
    WIND_DOWN_TOOLS,
    Surface,
    walk,
)

# A minimal served index mirroring agent-index.md's structure: a numbered 'typical session'
# whose stages name tools, links to a toolset guide (end-of-sentence punctuation included on
# purpose, to exercise stripping), and a wind-down naming teardown → release → close.
_HEALTHY_INDEX = f"""# Driving a kdive investigation

## Reaching tools

Reach anything through `tools.search` and `tools.invoke`.

## The typical session

1. **Orient** — start with `session.whoami`, then `investigations.open`.
2. **Acquire** — `allocations.request`, then `allocations.wait`.
3. **Provision** — `images.describe`, then `systems.provision`.
4. **Wind down** — `{WIND_DOWN_TOOLS[0]}`, then `{WIND_DOWN_TOOLS[1]}`, then
   `{WIND_DOWN_TOOLS[2]}`.

## Guides

See resource://kdive/docs/guide/toolsets/runs.md.
"""

_HEALTHY_TOOLS = {
    "session.whoami",
    "investigations.open",
    "allocations.request",
    "allocations.wait",
    "images.describe",
    "systems.provision",
    *GATEWAY_TOOLS,
    *WIND_DOWN_TOOLS,
}

_GUIDE_URI = "resource://kdive/docs/guide/toolsets/runs.md"


@dataclass
class FakeSurface:
    """A synthetic :class:`Surface`: plain data the walker reads as if it were served."""

    tools: set[str] = field(default_factory=set)
    resources: dict[str, str] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)

    async def tool_names(self) -> frozenset[str]:
        return frozenset(self.tools)

    async def resource_uris(self) -> frozenset[str]:
        return frozenset(self.resources)

    async def read(self, uri: str) -> str:
        return self.resources.get(uri, "")

    async def prompt_names(self) -> frozenset[str]:
        return frozenset(self.prompts)

    async def render(self, name: str) -> str:
        return self.prompts.get(name, "")


def _healthy() -> FakeSurface:
    return FakeSurface(
        tools=set(_HEALTHY_TOOLS),
        resources={
            AGENT_INDEX_URI: _HEALTHY_INDEX,
            _GUIDE_URI: "# Runs\n\nBody.",
        },
        prompts={name: f"{name} body" for name in NAMED_PROMPTS},
    )


def _walk(surface: Surface):
    return asyncio.run(walk(surface))


def _stages(result) -> set[str]:
    return {stall.stage for stall in result.stalls}


def test_healthy_surface_walks_green() -> None:
    result = _walk(_healthy())
    assert result.ok, [(stall.stage, stall.reason) for stall in result.stalls]
    assert {"orient", "wind-down", "gateway", "links", "prompts"} <= set(result.visited)


def test_missing_entry_doc_stalls_at_orient_and_stops() -> None:
    surface = _healthy()
    del surface.resources[AGENT_INDEX_URI]
    result = _walk(surface)
    assert _stages(result) == {"orient"}
    # Cannot walk without the map: the terminal stages are never visited.
    assert result.visited == ("orient",)


def test_empty_entry_doc_stalls_at_orient() -> None:
    surface = _healthy()
    surface.resources[AGENT_INDEX_URI] = "   \n"
    result = _walk(surface)
    assert _stages(result) == {"orient"}


def test_stage_naming_no_live_tool_stalls_at_that_stage() -> None:
    surface = _healthy()
    surface.tools.discard("images.describe")
    surface.tools.discard("systems.provision")
    result = _walk(surface)
    assert "stage-3" in _stages(result)


def test_missing_gateway_tool_stalls_at_gateway() -> None:
    surface = _healthy()
    surface.tools.discard("tools.invoke")
    result = _walk(surface)
    assert "gateway" in _stages(result)
    assert any("tools.invoke" in stall.reason for stall in result.stalls)


def test_missing_wind_down_tool_stalls_at_wind_down() -> None:
    surface = _healthy()
    surface.tools.discard(WIND_DOWN_TOOLS[-1])
    result = _walk(surface)
    assert "wind-down" in _stages(result)


def test_unserved_link_stalls_at_links() -> None:
    surface = _healthy()
    del surface.resources[_GUIDE_URI]
    result = _walk(surface)
    assert "links" in _stages(result)
    assert any(_GUIDE_URI in stall.reason for stall in result.stalls)


def test_empty_linked_guide_stalls_at_links() -> None:
    surface = _healthy()
    surface.resources[_GUIDE_URI] = "  "
    result = _walk(surface)
    assert "links" in _stages(result)


def test_trailing_sentence_punctuation_is_stripped_from_links() -> None:
    # The healthy doc writes "…runs.md." with a sentence period; the served resource key has
    # no period. A green walk proves the walker stripped it rather than 404-ing on "…runs.md.".
    assert _walk(_healthy()).ok


def test_missing_prompt_stalls_at_prompts() -> None:
    surface = _healthy()
    del surface.prompts[NAMED_PROMPTS[0]]
    result = _walk(surface)
    assert "prompts" in _stages(result)
    assert any(NAMED_PROMPTS[0] in stall.reason for stall in result.stalls)


def test_empty_rendered_prompt_stalls_at_prompts() -> None:
    surface = _healthy()
    surface.prompts[NAMED_PROMPTS[1]] = ""
    result = _walk(surface)
    assert "prompts" in _stages(result)


def test_missing_typical_session_section_stalls() -> None:
    surface = _healthy()
    surface.resources[AGENT_INDEX_URI] = "# Title\n\nNo numbered session here.\n"
    result = _walk(surface)
    assert "typical-session" in _stages(result)


def test_stalls_are_independent_and_accumulate() -> None:
    surface = replace(_healthy())
    surface.tools.discard("tools.search")
    del surface.prompts[NAMED_PROMPTS[2]]
    result = _walk(surface)
    assert {"gateway", "prompts"} <= _stages(result)
