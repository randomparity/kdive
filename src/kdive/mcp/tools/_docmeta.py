"""Shared documentation metadata for the `@app.tool` wrappers (ADR-0047).

`read_only` / `destructive` / `mutating` build the three MCP `ToolAnnotations`
classes once, so each registration spells its class by name rather than
re-listing hint flags. `DESTRUCTIVE_TOOLS` is the reviewed destructive-
administration set the guard test (`tests/mcp/test_tool_docs.py`) holds the
`destructiveHint` to; its membership is a reviewed judgement (ADR-0047).
"""

from __future__ import annotations

from typing import Literal, cast

from mcp.types import ToolAnnotations

ToolMaturityValue = Literal["implemented", "partial", "planned"]
Maturity = ToolMaturityValue

TOOL_MATURITY_VALUES: frozenset[ToolMaturityValue] = frozenset(
    {"implemented", "partial", "planned"}
)


def maturity_meta(maturity: Maturity) -> dict[str, object]:
    """Build the `@app.tool(meta=...)` dict for the closed maturity contract."""
    return {"maturity": maturity}


def normalize_maturity(value: object) -> ToolMaturityValue:
    """Return a closed maturity value, or fail fast on malformed tool metadata."""
    if value in TOOL_MATURITY_VALUES:
        return cast("ToolMaturityValue", value)
    choices = ", ".join(sorted(TOOL_MATURITY_VALUES))
    raise ValueError(f"invalid tool maturity {value!r}; expected one of: {choices}")


DESTRUCTIVE_TOOLS = frozenset(
    {
        "control.force_crash",
        "systems.teardown",
        "ops.force_teardown",
        "ops.force_release",
        "ops.reconcile_systems",
        "resources.drain",
        "resources.deregister",
        "images.delete",
        "images.prune_expired",
        "images.extend",
        # The gateway dispatcher can reach a destructive inner tool, so it carries the
        # destructive hint (errs toward client prompting). Membership here is hint-only:
        # the real gate is OPT_IN_DESTRUCTIVE_JOB_KINDS / assert_destructive_allowed on the
        # re-entered inner call, not this set (ADR-0268, #866).
        "tools.invoke",
    }
)


def read_only() -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=True)


def destructive() -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def mutating() -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=False, destructiveHint=False)
