"""Shared documentation metadata for the `@app.tool` wrappers (ADR-0047).

`read_only` / `destructive` / `mutating` build the three MCP `ToolAnnotations`
classes once, so each registration spells its class by name rather than
re-listing hint flags. `DESTRUCTIVE_TOOLS` is the reviewed destructive-
administration set the guard test (`tests/mcp/test_tool_docs.py`) holds the
`destructiveHint` to; its membership is a reviewed judgement (ADR-0047).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from mcp.types import ToolAnnotations

Maturity = Literal["implemented", "partial", "planned"]


class MaturityReason(StrEnum):
    """Why a `partial` tool is not yet `implemented` (ADR-0175).

    A closed vocabulary so a black-box agent can branch on the *category* of the
    limitation; the human specifics live in the free-text ``detail`` alongside it.
    """

    PROVIDER_SUPPORT = "provider_support"
    LIVE_DEPENDENCY = "live_dependency"
    UNPROVEN_WORKER_PATH = "unproven_worker_path"
    OPERATOR_GATE = "operator_gate"
    DEGRADED_STUB = "degraded_stub"


def _one_line(field: str, value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"maturity_meta: {field} must be a non-empty string")
    if "|" in text or "\n" in text:
        raise ValueError(f"maturity_meta: {field} has a table-breaking character (| or newline)")
    return text


def maturity_meta(
    maturity: Maturity,
    *,
    reason: MaturityReason | None = None,
    detail: str | None = None,
    promotion: str | None = None,
    providers: str | None = None,
) -> dict[str, Any]:
    """Build the `@app.tool(meta=...)` dict, enforcing the ADR-0175 invariants.

    A ``partial`` tool must carry a ``reason`` (closed enum), a one-line ``detail``
    (why it is partial today), and a one-line ``promotion`` (the bar to reach
    ``implemented``); ``providers`` is an optional one-line pointer for
    provider-dependent tools. A non-``partial`` tool must carry none of these — a
    leftover reason after promotion is a coding error.

    Args:
        maturity: The tool's maturity marker.
        reason: Required for ``partial``; forbidden otherwise.
        detail: One-line explanation; required for ``partial``, forbidden otherwise.
        promotion: One-line promotion bar; required for ``partial``, forbidden otherwise.
        providers: Optional one-line provider-support pointer (``partial`` only).

    Returns:
        The ``meta`` dict: ``{"maturity": ...}`` plus a ``maturity_detail`` object
        when ``partial``.

    Raises:
        ValueError: When the maturity/field combination violates the invariants.
    """
    if maturity != "partial":
        if any(v is not None for v in (reason, detail, promotion, providers)):
            raise ValueError(
                f"maturity_meta: {maturity!r} tool must not carry maturity_detail fields"
            )
        return {"maturity": maturity}

    if reason is None or detail is None or promotion is None:
        raise ValueError("maturity_meta: 'partial' requires reason, detail, and promotion")
    detail_obj: dict[str, str] = {
        "reason": reason.value,
        "detail": _one_line("detail", detail),
        "promotion": _one_line("promotion", promotion),
    }
    if providers is not None:
        detail_obj["providers"] = _one_line("providers", providers)
    return {"maturity": maturity, "maturity_detail": detail_obj}


DESTRUCTIVE_TOOLS = frozenset(
    {
        "control.power",
        "control.force_crash",
        "systems.teardown",
        "systems.reprovision",
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
