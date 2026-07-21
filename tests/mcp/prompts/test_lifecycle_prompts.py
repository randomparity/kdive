"""Pure prompts registrar: rendering, maturity disclosure, and fail-fast (ADR-0202)."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP
from fastmcp.prompts.base import Prompt
from mcp.types import TextContent

from kdive.mcp.prompts.registrar import (
    CANONICAL_PROMPTS,
    PromptSpec,
    Step,
    ToolMaturity,
    _render_body,
    _validate_preconditions,
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


def test_build_boot_debug_leads_with_external_upload_loop() -> None:
    # ADR-0234: the build_boot_debug journey is the external upload loop end to end.
    spec = next(s for s in CANONICAL_PROMPTS if s.name == "build_boot_debug")
    tools = [step.tool for step in spec.steps]
    upload_loop = [
        "runs.create",
        "artifacts.expected_uploads",
        "artifacts.create_run_upload",
        "runs.complete_build",
    ]
    assert tools[: len(upload_loop)] == upload_loop
    # runs.build (the warm-tree enqueue verb) is no longer a step in the journey.
    assert "runs.build" not in tools
    assert "runs.complete_build" in spec.summary
    assert "upload" in spec.summary.lower()


def test_render_numbers_steps_sequentially_from_one_on_their_own_lines() -> None:
    spec = PromptSpec(
        name="probe",
        title="Probe",
        description="d",
        summary="s",
        steps=(
            Step(tool="a.first", purpose="p1"),
            Step(tool="a.second", purpose="p2"),
            Step(tool="a.third", purpose="p3"),
        ),
    )
    maturity = {
        "a.first": ToolMaturity(maturity="implemented", reason=None),
        "a.second": ToolMaturity(maturity="implemented", reason=None),
        "a.third": ToolMaturity(maturity="implemented", reason=None),
    }
    body = _render_body(spec, maturity)
    # Numbering starts at 1 (not 0 and not 2) and each step is on its own line.
    assert "1. a.first — p1" in body
    assert "2. a.second — p2" in body
    assert "3. a.third — p3" in body
    assert "0. a.first" not in body
    assert "2. a.first" not in body
    # The steps are newline-separated, not joined into a single run.
    assert "1. a.first — p1\n2. a.second — p2\n3. a.third — p3" in body


def test_every_canonical_step_precondition_is_satisfied_upstream() -> None:
    # #1369: every step's `requires` capability must be provided by an earlier step in
    # the same journey — the machine-checkable form of the prose preconditions.
    for spec in CANONICAL_PROMPTS:
        _validate_preconditions(spec)


def test_build_boot_debug_introspect_requires_live_session_from_start_session() -> None:
    # The P1-4 stall made structural: introspect.run's precondition is exactly the
    # capability debug.start_session provides, and start_session is earlier in the journey.
    spec = next(s for s in CANONICAL_PROMPTS if s.name == "build_boot_debug")
    by_tool = {step.tool: step for step in spec.steps}
    assert by_tool["introspect.run"].requires == ("drgn-live-session",)
    assert "drgn-live-session" in by_tool["debug.start_session"].provides
    tools = [step.tool for step in spec.steps]
    assert tools.index("debug.start_session") < tools.index("introspect.run")


def test_first_step_of_every_journey_has_no_requires() -> None:
    # Cross-journey prerequisites stay in prose; a journey's opening step never carries a
    # journey-local `requires` (it would be unsatisfiable and wrongly fail the guard).
    for spec in CANONICAL_PROMPTS:
        assert spec.steps[0].requires == (), spec.name


def test_precondition_validator_catches_unsatisfied_requirement() -> None:
    # The deliberately-broken journey: introspect.run demands a live session no earlier
    # step provides. This is the violation the guard must reject (mirrors the P1-4 stall).
    broken = PromptSpec(
        name="broken_boot_debug",
        title="Broken",
        description="d",
        summary="s",
        steps=(
            Step(tool="runs.boot", purpose="boot", provides=("booted-system",)),
            Step(tool="introspect.run", purpose="introspect", requires=("drgn-live-session",)),
        ),
    )
    with pytest.raises(RuntimeError, match="drgn-live-session"):
        _validate_preconditions(broken)


def test_precondition_validator_rejects_requirement_provided_only_later() -> None:
    # Order matters: a capability provided by a *later* step does not satisfy an earlier
    # step's precondition, even though the tag appears somewhere in the journey.
    out_of_order = PromptSpec(
        name="out_of_order",
        title="Out of order",
        description="d",
        summary="s",
        steps=(
            Step(tool="introspect.run", purpose="introspect", requires=("drgn-live-session",)),
            Step(
                tool="debug.start_session",
                purpose="attach",
                provides=("drgn-live-session",),
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="drgn-live-session"):
        _validate_preconditions(out_of_order)


def test_register_fails_fast_when_a_journey_precondition_is_unmet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # register() validates preconditions before rendering, so a mis-ordered journey is
    # rejected at registration, not only by the unit test above.
    broken = PromptSpec(
        name="broken",
        title="Broken",
        description="d",
        summary="s",
        steps=(Step(tool="introspect.run", purpose="p", requires=("drgn-live-session",)),),
    )
    monkeypatch.setattr("kdive.mcp.prompts.registrar.CANONICAL_PROMPTS", (broken,))
    maturity = {"introspect.run": ToolMaturity(maturity="implemented", reason=None)}
    with pytest.raises(RuntimeError, match="drgn-live-session"):
        register(FastMCP("probe"), tool_maturity=maturity)


def test_registered_prompts_carry_their_spec_title_and_description() -> None:
    app = FastMCP("probe")
    register(app, tool_maturity=_full_maturity_map())
    specs = {spec.name: spec for spec in CANONICAL_PROMPTS}

    async def _listed() -> dict[str, Prompt]:
        return {p.name: p for p in await app.list_prompts()}

    listed = asyncio.run(_listed())
    for name, spec in specs.items():
        prompt = listed[name]
        assert prompt.title == spec.title
        assert prompt.description == spec.description
