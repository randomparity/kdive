"""tool_index.py: deterministic search ranking + namespace TOC (ADR-0267, #866)."""

from __future__ import annotations

from kdive.mcp.tool_index import (
    GATEWAY_INSTRUCTIONS,
    NAMESPACE_TOC,
    TOOL_KEYWORDS,
    rank_tools,
)

_CANDIDATES = [
    ("runs.boot", "Boot a built, installed Run and capture the console."),
    ("runs.build", "Build the kernel for a Run."),
    ("debug.read_memory", "Read guest memory in a live debug session."),
    ("accounting.set_budget", "Set the project budget."),
]


def test_rank_tools_is_deterministic() -> None:
    """Same query + candidates yields identical order across calls."""
    first = rank_tools("boot kernel", _CANDIDATES, limit=8)
    second = rank_tools("boot kernel", _CANDIDATES, limit=8)
    assert first == second


def test_rank_tools_surfaces_intent_match() -> None:
    """A capability phrase finds the matching tool even when the word is only a keyword."""
    result = rank_tools("power on the vm", _CANDIDATES, limit=8)
    assert "runs.boot" in result


def test_rank_tools_respects_limit() -> None:
    """No more than ``limit`` results are returned."""
    result = rank_tools("run", _CANDIDATES, limit=2)
    assert len(result) <= 2


def test_rank_tools_ties_break_by_name() -> None:
    """Equal-scoring tools are ordered lexicographically by name, not input order."""
    # Two tools with identical text → score tie; lexicographic order is build before boot? No:
    # "runs.boot" < "runs.build" lexicographically, so boot precedes build on a tie.
    candidates = [
        ("runs.build", "kernel"),
        ("runs.boot", "kernel"),
    ]
    result = rank_tools("kernel", candidates, limit=8)
    assert result == ["runs.boot", "runs.build"]


def test_rank_tools_empty_query_returns_empty() -> None:
    """An empty or whitespace query matches nothing (the caller turns this into a rejection)."""
    assert rank_tools("   ", _CANDIDATES, limit=8) == []
    assert rank_tools("", _CANDIDATES, limit=8) == []


def test_rank_tools_unmatched_query_returns_empty() -> None:
    """A query that matches no tool returns no results (the search-miss signal)."""
    assert rank_tools("zzzqqq nonsense", _CANDIDATES, limit=8) == []


def test_tool_with_no_keyword_entry_still_ranks_on_description() -> None:
    """A tool absent from TOOL_KEYWORDS still matches via tokenised name + description."""
    assert "accounting.set_budget" not in TOOL_KEYWORDS
    result = rank_tools("budget", _CANDIDATES, limit=8)
    assert "accounting.set_budget" in result


def test_namespace_toc_and_instructions_are_present() -> None:
    """The TOC and gateway preamble are non-empty and mention the search affordance."""
    assert NAMESPACE_TOC
    assert "runs" in NAMESPACE_TOC
    assert "tools.search" in GATEWAY_INSTRUCTIONS
