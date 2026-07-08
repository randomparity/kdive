"""Register canonical lifecycle workflows as MCP prompts (ADR-0202).

`build_app()` registers tools and doc resources but no MCP prompts, so the only
server-authored guidance is the reactive `suggested_next_actions` envelope field
(ADR-0019). This module registers a fixed, code-defined set of three canonical
lifecycle prompts (`start_investigation`, `build_boot_debug`, `triage_panic`) as *thin
pointers* into the real tools: each is an ordered list of registered tool names with a
one-line purpose per step.

Maturity (ADR-0175) is respected by *disclosure*, not omission: rather than dropping a
`partial` step (which could empty a journey), each `partial` step is tagged with its
maturity `reason` so an agent is never silently steered into a not-yet-proven tool. Every
lifecycle step is `implemented` today, but the disclosure survives for any future `partial`
step. A referenced tool that is unknown to
the live registry or marked `planned` (advertised but unavailable) raises at registration
— a fail-fast that also catches an accidental registrar reordering. `register` takes the
maturity map explicitly, so it is pure with respect to FastMCP internals and unit-testable
with a fabricated map; the live map is assembled in `mcp/app.py` from the registered tool
metas.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastmcp import FastMCP
from fastmcp.prompts import Prompt


@dataclass(frozen=True, slots=True)
class ToolMaturity:
    """The live maturity of one referenced tool.

    Attributes:
        maturity: The maturity marker (``implemented`` / ``partial`` / ``planned``).
        reason: The ``maturity_detail.reason`` for a ``partial`` tool, else ``None``.
    """

    maturity: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class Step:
    """One step of a journey: a real registered tool and its one-line purpose."""

    tool: str
    purpose: str


@dataclass(frozen=True, slots=True)
class PromptSpec:
    """One canonical journey rendered as an MCP prompt.

    Attributes:
        name: The prompt name advertised in ``ListMcpPrompts``.
        title: Human title.
        description: One-line description shown in the listing.
        summary: Orientation rendered at the top of the body (includes any precondition).
        steps: The ordered tool sequence.
    """

    name: str
    title: str
    description: str
    summary: str
    steps: tuple[Step, ...]


_NOTES = (
    "Notes:\n"
    "- Poll long-running steps with jobs.wait / jobs.get.\n"
    "- Read any tool's full contract before calling it; see "
    "resource://kdive/docs/guide/response-envelope.md for how to read results.\n"
    "- [partial] steps are not yet proven end-to-end; check the tool's maturity_detail."
)


CANONICAL_PROMPTS: tuple[PromptSpec, ...] = (
    PromptSpec(
        name="start_investigation",
        title="Start a kdive investigation",
        description="Orient and acquire capacity for a new kernel investigation.",
        summary="Open an investigation and acquire a target system to work on.",
        steps=(
            Step("investigations.open", "open an investigation to group related runs"),
            Step("resources.list", "see which resources you can allocate"),
            Step("allocations.request", "request capacity on a resource"),
            Step("allocations.wait", "wait until the allocation is granted"),
            Step("systems.define", "define the target system to build/boot on"),
        ),
    ),
    PromptSpec(
        name="build_boot_debug",
        title="Build, boot, and debug a kernel",
        description="Build a kernel, boot it on a system, and attach a live debug session.",
        summary=(
            "Build a kernel, boot it, and attach a live debug session. Build the kernel "
            "yourself and upload it (ADR-0234): upload a prebuilt artifact via "
            "artifacts.expected_uploads -> artifacts.create_run_upload -> runs.complete_build. "
            "Prerequisite: an open investigation and a defined, allocated system "
            "(see start_investigation)."
        ),
        steps=(
            Step("runs.create", "create a run with source='external' (the default upload lane)"),
            Step("artifacts.expected_uploads", "learn the exact artifact bytes to produce"),
            Step("artifacts.create_run_upload", "upload the prebuilt kernel artifact"),
            Step("runs.complete_build", "finalize the externally built run"),
            Step("runs.install", "install the built kernel onto the system"),
            Step("runs.boot", "boot the system into the built kernel"),
            Step("debug.start_session", "attach a live debug session"),
            Step("introspect.run", "inspect kernel state in the live session"),
            Step("debug.end_session", "detach when done"),
        ),
    ),
    PromptSpec(
        name="triage_panic",
        title="Triage a kernel panic",
        description="Turn a crash into a captured vmcore and a postmortem.",
        summary=(
            "Capture and analyze a crash. "
            "Prerequisite: a booted system on a kdump-capable run (see build_boot_debug)."
        ),
        steps=(
            Step("control.force_crash", "induce a crash (or react to an observed panic)"),
            Step("vmcore.fetch", "capture the vmcore from the crashed system"),
            Step("vmcore.list", "confirm the captured vmcore reference"),
            Step("postmortem.triage", "run the first-pass crash triage"),
            Step("introspect.from_vmcore", "inspect kernel state from the captured vmcore"),
        ),
    ),
)


def _step_line(index: int, step: Step, maturity: Mapping[str, ToolMaturity]) -> str:
    """Render one numbered step line, tagging a partial step with its reason.

    Raises:
        RuntimeError: If the step's tool is unknown to ``maturity`` or marked ``planned``
            — both mean the prompt would steer an agent into a tool it cannot use.
    """
    record = maturity.get(step.tool)
    if record is None:
        raise RuntimeError(
            f"prompt step references unknown tool {step.tool!r}; it is not a registered "
            "tool (ADR-0202)"
        )
    if record.maturity == "planned":
        raise RuntimeError(
            f"prompt step references planned (unavailable) tool {step.tool!r}; prompts "
            "must not steer into planned tools (ADR-0202)"
        )
    line = f"{index}. {step.tool} — {step.purpose}"
    if record.maturity == "partial":
        tag = f"[partial: {record.reason}]" if record.reason else "[partial]"
        line = f"{line}  {tag}"
    return line


def _render_body(spec: PromptSpec, tool_maturity: Mapping[str, ToolMaturity]) -> str:
    """Render a prompt's markdown body from its spec and the live tool maturity."""
    lines = [_step_line(i, step, tool_maturity) for i, step in enumerate(spec.steps, start=1)]
    sequence = "\n".join(lines)
    return f"{spec.summary}\n\nCanonical tool sequence:\n{sequence}\n\n{_NOTES}"


def _body_prompt(spec: PromptSpec, body: str) -> Prompt:
    """Build a no-argument `FunctionPrompt` whose render returns the pre-rendered body.

    The body is bound as a default argument so the three prompts do not share a
    late-bound closure variable.
    """

    def _render(_body: str = body) -> str:
        return _body

    return Prompt.from_function(
        _render, name=spec.name, title=spec.title, description=spec.description
    )


def register(app: FastMCP, *, tool_maturity: Mapping[str, ToolMaturity]) -> int:
    """Register every canonical lifecycle prompt on ``app``.

    Renders each prompt's body against ``tool_maturity`` (failing fast on an unknown or
    ``planned`` referenced tool) and registers it as a `FunctionPrompt`.

    Args:
        app: The FastMCP app to register prompts on.
        tool_maturity: Live maturity per referenced tool name, assembled from the
            registered tool metas.

    Returns:
        The number of prompts registered.

    Raises:
        RuntimeError: If any referenced tool is unknown or ``planned``.
    """
    for spec in CANONICAL_PROMPTS:
        body = _render_body(spec, tool_maturity)
        app.add_prompt(_body_prompt(spec, body))
    return len(CANONICAL_PROMPTS)
