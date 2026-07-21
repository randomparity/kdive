"""Golden-path graph guard over the served ``agent-index.md`` (#1366, ADR-0407).

The served agent index (``resources/_content/agent-index.md``) encodes the canonical
investigation journey — the ordered stages an agent walks from orientation to wind-down.
This module treats that journey as a directed graph and guards its edge structure against
regression: the discovery fan-out and the wind-down ordering that #1362 repaired, and the
forward reachability from ``images.describe`` to ``runs.create`` that the build lane depends
on. Every graph node is validated against the live tool registry via ``list_tools()`` so a
renamed or removed tool trips the guard.

Scope boundary (ADR-0407). This guards the *doc-encoded* golden path, not the per-tool
``suggested_next_actions`` values a handler returns at runtime: those are constructed inside
each handler's ``ToolResponse`` and are **not** exposed on the ``Tool`` schema that
``list_tools()`` returns (its ``suggested_next_actions`` output-schema field is the generic
``array-of-string`` shape). A blanket "no tool self-loops" rule is likewise out of scope and
would be wrong — the clean registry has intentional retry/re-poll self-loops
(``accounting.estimate``, ``accounting.usage_project``, ``artifacts.fetch_raw``,
``fixtures.validate``, ``jobs.wait``). The true, guardable invariant is that no *golden-path
stage* is self-referential, which is what this module asserts.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from functools import cache
from importlib.resources import files

from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.mcp.dev_harness import AUDIENCE, ISSUER, make_keypair
from kdive.mcp.resources.registrar import DOC_RESOURCES
from kdive.security.secrets.secret_registry import SecretRegistry

# --- The golden path, encoded as reviewed data (source: served agent-index.md) --------------
#
# One entry per numbered stage of "The typical session". ``primary`` is the stage's
# first-called tool (the doc names "the first tool to call" first in each step); ``named`` is
# every registered tool the stage must mention. Both are asserted against the served doc and
# the live registry, so a doc edit that drops, renames, reorders, or self-loops a stage fails.

_GOLDEN_PATH: tuple[tuple[int, str, frozenset[str]], ...] = (
    (
        1,
        "session.whoami",
        frozenset(
            {
                "session.whoami",
                "resources.list",
                "resources.availability",
                "shapes.list",
                "accounting.estimate",
                "investigations.open",
            }
        ),
    ),
    (2, "allocations.request", frozenset({"allocations.request", "allocations.wait"})),
    (
        3,
        "images.describe",
        frozenset(
            {"images.describe", "systems.provision", "systems.define", "systems.provision_defined"}
        ),
    ),
    (
        4,
        "runs.create",
        frozenset(
            {
                "runs.create",
                "artifacts.expected_uploads",
                "artifacts.create_run_upload",
                "runs.complete_build",
            }
        ),
    ),
    (5, "runs.install", frozenset({"runs.install", "runs.boot"})),
    (6, "systems.authorize_ssh_key", frozenset({"systems.authorize_ssh_key", "jobs.wait"})),
    (7, "runs.get", frozenset({"runs.get", "artifacts.get", "artifacts.list"})),
    (8, "debug.start_session", frozenset({"debug.start_session", "introspect.run"})),
    (
        9,
        "control.force_crash",
        frozenset({"control.force_crash", "vmcore.fetch", "postmortem.triage"}),
    ),
    (
        10,
        "systems.teardown",
        frozenset({"systems.teardown", "allocations.release", "investigations.close"}),
    ),
)

# The ordered wind-down edge #1362 repaired (agent-index step 10): the doc must name these
# three in this release order.
_WIND_DOWN_ORDER: tuple[str, ...] = (
    "systems.teardown",
    "allocations.release",
    "investigations.close",
)

_SESSION_HEADING = "## The typical session"
_STEP = re.compile(r"^(\d+)\.\s(.+?)(?=^\d+\.\s|\Z)", re.MULTILINE | re.DOTALL)
_BACKTICKED = re.compile(r"`([^`]+)`")


@cache
def _registered_tool_names() -> frozenset[str]:
    """Return every tool name in the live registry (the ``list_tools()`` harness)."""
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())

    async def _names() -> frozenset[str]:
        return frozenset(t.name for t in await app.list_tools())

    return asyncio.run(_names())


@cache
def _served_agent_index() -> str:
    """Return the served ``agent-index.md`` snapshot bytes the server hands to agents."""
    entry = next(e for e in DOC_RESOURCES if e.name == "agent-index")
    return (files("kdive.mcp.resources") / "_content" / entry.content_file).read_text(
        encoding="utf-8"
    )


def _session_section(doc: str) -> str:
    """Return the body of the numbered 'typical session' section (up to the next heading)."""
    start = doc.index(_SESSION_HEADING) + len(_SESSION_HEADING)
    rest = doc[start:]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: next_heading.start()] if next_heading else rest


def _stage_named_tools() -> dict[int, list[str]]:
    """Map each numbered stage to the registered tools it names, in first-mention order.

    Backticked tokens that are not live tool names (resource URIs, field names, shell
    snippets) are dropped, so the first surviving token of a step is that stage's primary.
    """
    registry = _registered_tool_names()
    section = _session_section(_served_agent_index())
    stages: dict[int, list[str]] = {}
    for number, body in _STEP.findall(section):
        seen: list[str] = []
        for token in _BACKTICKED.findall(body):
            if token in registry and token not in seen:
                seen.append(token)
        stages[int(number)] = seen
    return stages


def _served_spine() -> list[str]:
    """Return each stage's primary (first named tool) in golden-path order.

    Fails with a readable message if a stage names no registered tool at all, rather than
    letting the callers' first-element indexing raise a bare ``IndexError``.
    """
    stages = _stage_named_tools()
    spine: list[str] = []
    for number, _, _ in _GOLDEN_PATH:
        named = stages.get(number, [])
        assert named, f"golden-path stage {number} names no registered tool"
        spine.append(named[0])
    return spine


def test_golden_path_nodes_are_registered_tools() -> None:
    """Every node in the golden path resolves to a live tool (a rename/removal trips this)."""
    registry = _registered_tool_names()
    nodes = {primary for _, primary, _ in _GOLDEN_PATH}
    nodes |= {tool for _, _, named in _GOLDEN_PATH for tool in named}
    missing = sorted(nodes - registry)
    assert not missing, f"golden-path tools absent from the live registry: {missing}"


def test_served_primaries_match_golden_path_and_progress() -> None:
    """The doc's per-stage primary sequence equals the reviewed path and never self-loops.

    The primary is the first registered tool each step names. A stage that regressed to point
    back at itself (or at a prior stage) — the shape of the P1-6 self-loop, at the doc level —
    would repeat a primary and fail the distinctness assertion.
    """
    served_primaries = _served_spine()
    expected_primaries = [primary for _, primary, _ in _GOLDEN_PATH]
    assert served_primaries == expected_primaries, (
        f"served: {served_primaries}; expected: {expected_primaries}"
    )
    assert len(set(served_primaries)) == len(served_primaries), (
        f"a golden-path stage is self-referential (repeated primary): {served_primaries}"
    )


def test_each_stage_names_its_required_tools() -> None:
    """Each stage names every tool the golden path requires of it (discovery fan-out etc.)."""
    stages = _stage_named_tools()
    for number, _, required in _GOLDEN_PATH:
        named = set(stages[number])
        missing = sorted(required - named)
        assert not missing, f"stage {number} no longer names required tools: {missing}"


def test_images_describe_reaches_runs_create() -> None:
    """The build edge holds: ``images.describe`` reaches ``runs.create`` along the path."""
    spine = _served_spine()
    adjacency = {spine[i]: spine[i + 1] for i in range(len(spine) - 1)}
    reached: set[str] = set()
    queue: deque[str] = deque(["images.describe"])
    while queue:
        node = queue.popleft()
        if node in reached:
            continue
        reached.add(node)
        if node in adjacency:
            queue.append(adjacency[node])
    assert "images.describe" in spine, "images.describe dropped from the provision stage"
    assert "runs.create" in reached, (
        f"runs.create no longer reachable from images.describe; spine={spine}"
    )


def test_wind_down_edge_order_preserved() -> None:
    """The wind-down stage names teardown → release → close in that repaired order (#1362)."""
    stages = _stage_named_tools()
    wind_down = stages[_GOLDEN_PATH[-1][0]]
    positions = [wind_down.index(tool) for tool in _WIND_DOWN_ORDER if tool in wind_down]
    assert len(positions) == len(_WIND_DOWN_ORDER), (
        f"wind-down stage dropped a tool; names={wind_down}"
    )
    assert positions == sorted(positions), (
        f"wind-down order regressed; got {wind_down}, want {list(_WIND_DOWN_ORDER)}"
    )
