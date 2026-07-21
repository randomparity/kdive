"""Deterministic doc-driven agent-smoke walker over the served surface (#1370, ADR-0411).

An agent orients by reading the served ``agent-index.md`` and then drives the golden path
using **only** what the surface serves — the tools ``list_tools`` advertises, the guides its
``resource://`` links point at, and the lifecycle prompts it names. This module walks that
path deterministically (no LLM) and records every **stall**: a step whose next action is a
dead end because the surface does not actually serve what the doc advertises.

The walk is *surface-driven*. It parses the served index at runtime and follows what the doc
itself says, so it holds no hand-copied tool table — that is #1366's job. #1366 (ADR-0407,
``test_next_actions_graph.py``) is the **PR-gate static graph guard** over a reviewed
node/edge table; this is the **gated non-PR served-surface walk** that opens each linked
resource and renders each prompt. The failure classes are complementary: #1366 catches "the
doc regressed against reviewed intent"; this catches "the doc names surface the server does
not actually serve."

The walker talks to a small :class:`Surface` protocol (normalized string views of the MCP
surface), so the real app and a synthetic fixture drive the same code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

AGENT_INDEX_URI = "resource://kdive/docs/guide/agent-index.md"

# The gateway the index promises is "always available" — a lazy-loading host that materializes
# only some tools reaches everything else through it, so its absence strands such a client.
GATEWAY_TOOLS: tuple[str, ...] = ("tools.search", "tools.invoke")

# The lifecycle prompts the index footer tells prompt-listing clients they can use.
NAMED_PROMPTS: tuple[str, ...] = ("start_investigation", "build_boot_debug", "triage_panic")

# The wind-down the index's final stage names: an agent that cannot reach these leaks the
# capacity it acquired, which is itself a stall.
WIND_DOWN_TOOLS: tuple[str, ...] = (
    "systems.teardown",
    "allocations.release",
    "investigations.close",
)

_SESSION_HEADING = "## The typical session"
_STEP = re.compile(r"^(\d+)\.\s(.+?)(?=^\d+\.\s|\Z)", re.MULTILINE | re.DOTALL)
_BACKTICKED = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"resource://kdive/\S+")


class Surface(Protocol):
    """A normalized, string-only view of the served MCP surface an agent reaches."""

    async def tool_names(self) -> frozenset[str]: ...
    async def resource_uris(self) -> frozenset[str]: ...
    async def read(self, uri: str) -> str: ...
    async def prompt_names(self) -> frozenset[str]: ...
    async def render(self, name: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Stall:
    """One dead end the agent hit: which stage, and why the next action was unreachable."""

    stage: str
    reason: str


@dataclass(frozen=True, slots=True)
class WalkResult:
    """The outcome of one golden-path walk: the stages visited and every stall recorded."""

    visited: tuple[str, ...]
    stalls: tuple[Stall, ...]

    @property
    def ok(self) -> bool:
        """True when the golden path walked end-to-end with no dead end."""
        return not self.stalls


def _session_section(doc: str) -> str:
    """Return the numbered 'typical session' body (up to the next heading), or '' if absent."""
    if _SESSION_HEADING not in doc:
        return ""
    rest = doc[doc.index(_SESSION_HEADING) + len(_SESSION_HEADING) :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: next_heading.start()] if next_heading else rest


def _stage_tools(section: str, tools: frozenset[str]) -> list[tuple[int, list[str]]]:
    """Map each numbered stage to the live tools it names, in first-mention order.

    Backticked tokens that are not live tool names — refs (``refs.latest_console``), response
    fields (``data.supports_snapshots``), provider paths, params — are dropped, so a stage's
    surviving list is exactly the actions the served surface can honor.
    """
    stages: list[tuple[int, list[str]]] = []
    for number, body in _STEP.findall(section):
        named = [t for t in dict.fromkeys(_BACKTICKED.findall(body)) if t in tools]
        stages.append((int(number), named))
    return stages


def _advertised_links(doc: str) -> list[str]:
    """Return each distinct ``resource://kdive/...`` link the doc advertises, punctuation-stripped.

    A link at the end of a sentence, inside a Markdown target, or wrapped in backticks/angle
    brackets carries trailing delimiters that are not part of the URI; strip them so the link
    resolves against the served resource set.
    """
    seen: dict[str, None] = {}
    for raw in _LINK.findall(doc):
        seen.setdefault(raw.rstrip(").,;:`>"), None)
    return list(seen)


async def walk(surface: Surface) -> WalkResult:
    """Drive the golden path over ``surface`` and return every stall (empty == green)."""
    visited: list[str] = ["orient"]
    stalls: list[Stall] = []

    tools = await surface.tool_names()
    resources = await surface.resource_uris()
    prompts = await surface.prompt_names()

    index = await surface.read(AGENT_INDEX_URI) if AGENT_INDEX_URI in resources else ""
    if not index.strip():
        # Without the entry doc the agent cannot even orient; the walk cannot proceed.
        stalls.append(Stall("orient", f"entry doc {AGENT_INDEX_URI} not served or empty"))
        return WalkResult(tuple(visited), tuple(stalls))

    section = _session_section(index)
    stages = _stage_tools(section, tools)
    if not stages:
        stalls.append(Stall("typical-session", "no numbered golden-path stages found"))
    for number, named in stages:
        visited.append(f"stage-{number}")
        if not named:
            stalls.append(Stall(f"stage-{number}", "names no live tool — no next action"))

    visited.append("wind-down")
    missing_wind_down = [t for t in WIND_DOWN_TOOLS if t not in tools]
    if missing_wind_down:
        stalls.append(Stall("wind-down", f"cannot reach wind-down tools: {missing_wind_down}"))

    visited.append("gateway")
    missing_gateway = [t for t in GATEWAY_TOOLS if t not in tools]
    if missing_gateway:
        stalls.append(Stall("gateway", f"always-available gateway absent: {missing_gateway}"))

    visited.append("links")
    for uri in _advertised_links(index):
        if uri == AGENT_INDEX_URI:
            continue
        if uri not in resources or not (await surface.read(uri)).strip():
            stalls.append(Stall("links", f"advertised link is a dead end: {uri}"))

    visited.append("prompts")
    for name in NAMED_PROMPTS:
        if name not in prompts:
            stalls.append(Stall("prompts", f"named lifecycle prompt not listed: {name}"))
        elif not (await surface.render(name)).strip():
            stalls.append(Stall("prompts", f"named lifecycle prompt renders empty: {name}"))

    return WalkResult(tuple(visited), tuple(stalls))
