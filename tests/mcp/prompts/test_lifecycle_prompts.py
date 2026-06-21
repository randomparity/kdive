"""Pure prompts registrar: rendering, maturity disclosure, and fail-fast (ADR-0202)."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP
from mcp.types import TextContent

from kdive.mcp.prompts.registrar import (
    CANONICAL_PROMPTS,
    PromptSpec,
    Step,
    ToolMaturity,
    _render_body,
    register,
)


def _full_maturity_map() -> dict[str, ToolMaturity]:
    """An `implemented` record for every tool referenced by any canonical prompt."""
    return {
        step.tool: ToolMaturity(maturity="implemented", reason=None)
        for spec in CANONICAL_PROMPTS
        for step in spec.steps
    }


def test_render_tags_partial_steps_with_reason() -> None:
    spec = PromptSpec(
        name="probe",
        title="Probe",
        description="d",
        summary="s",
        steps=(Step(tool="a.ready", purpose="p1"), Step(tool="a.wip", purpose="p2")),
    )
    maturity = {
        "a.ready": ToolMaturity(maturity="implemented", reason=None),
        "a.wip": ToolMaturity(maturity="partial", reason="live_dependency"),
    }
    body = _render_body(spec, maturity)
    assert "a.ready — p1" in body
    assert "[partial" not in body.split("a.ready — p1")[1].split("\n")[0]
    assert "a.wip — p2  [partial: live_dependency]" in body


def test_render_partial_without_reason_falls_back_to_bare_tag() -> None:
    spec = PromptSpec(
        name="probe",
        title="Probe",
        description="d",
        summary="s",
        steps=(Step(tool="a.wip", purpose="p"),),
    )
    maturity = {"a.wip": ToolMaturity(maturity="partial", reason=None)}
    body = _render_body(spec, maturity)
    assert "a.wip — p  [partial]" in body


def test_unknown_tool_raises() -> None:
    spec = PromptSpec(
        name="probe",
        title="Probe",
        description="d",
        summary="s",
        steps=(Step(tool="a.missing", purpose="p"),),
    )
    with pytest.raises(RuntimeError, match="a.missing"):
        _render_body(spec, {})


def test_planned_tool_raises() -> None:
    spec = PromptSpec(
        name="probe",
        title="Probe",
        description="d",
        summary="s",
        steps=(Step(tool="a.future", purpose="p"),),
    )
    maturity = {"a.future": ToolMaturity(maturity="planned", reason=None)}
    with pytest.raises(RuntimeError, match="a.future"):
        _render_body(spec, maturity)


def test_register_returns_count_and_lists_three_prompts() -> None:
    app = FastMCP("probe")
    count = register(app, tool_maturity=_full_maturity_map())
    assert count == len(CANONICAL_PROMPTS) == 3

    async def _names() -> set[str]:
        return {p.name for p in await app.list_prompts()}

    assert {spec.name for spec in CANONICAL_PROMPTS} == asyncio.run(_names())


def test_each_prompt_renders_nonempty_body_naming_every_step_tool() -> None:
    app = FastMCP("probe")
    register(app, tool_maturity=_full_maturity_map())

    async def _body(name: str) -> str:
        result = await app.render_prompt(name, {})
        content = result.messages[0].content
        assert isinstance(content, TextContent)
        return content.text

    for spec in CANONICAL_PROMPTS:
        body = asyncio.run(_body(spec.name))
        assert body.strip()
        for step in spec.steps:
            assert step.tool in body, f"{spec.name} body omits {step.tool}"
