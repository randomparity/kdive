"""Guard: a denial envelope never steers the caller at the tool that denied it (ADR-0471).

An ``authorization_denied`` envelope used to set ``suggested_next_actions=[<the denied tool>]``
at 21 sites, so an agent following the navigation contract was steered straight back into a call
it cannot complete without a grant it does not hold (#1596). ADR-0471 replaced every hand-rolled
denial with :meth:`ToolResponse.denied`, whose breadcrumb is the fixed
:data:`~kdive.mcp.responses.DENIAL_NEXT_ACTIONS` constant and which accepts no caller-supplied
actions at all.

Two independent layers, because either alone can rot:

1. **Source (this module's AST walk).** No call under ``src/kdive/`` may pass
   ``ErrorCategory.AUTHORIZATION_DENIED`` to anything. ``responses.py`` — where ``denied`` builds
   the envelope — is the single allowlisted definition site. A new tool that hand-rolls
   ``ToolResponse.failure(obj, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL])``
   trips this immediately.
2. **Registry (the breadcrumb-is-public assertions).** Every name the constant carries must be a
   live, *public* tool. Only a **gated** tool can raise an authorization denial in the first
   place, so a breadcrumb drawn exclusively from ``PUBLIC_TOOLS`` can never name its own denier —
   the invariant holds by construction, not by per-site vigilance.

Scope boundary. ``tests/mcp/core/test_next_actions_graph.py`` guards the *doc-encoded* golden
path; this module guards the runtime ``suggested_next_actions`` of the denial envelope, which
that module explicitly places out of its own scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kdive.mcp.exposure import CLASSIFIED_TOOLS, PUBLIC_TOOLS
from kdive.mcp.responses import DENIAL_NEXT_ACTIONS, ToolResponse

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kdive"

# The whole package, not just `mcp/`: a denial envelope built in a service or provider module
# would be just as wrong, and scoping the walk to where the defect happened to live would leave
# the guard blind to the next one.
#
# The one module allowed to name the category in a call: `ToolResponse.denied` builds the
# envelope by delegating to `cls.failure(..., ErrorCategory.AUTHORIZATION_DENIED, ...)`.
# `domain/errors.py` defines the member and the ADR-0123 suppression map, but does so in an
# enum body and a dict literal — neither is a call, so it needs no allowlist entry.
_ALLOWED = _SRC_ROOT / "mcp" / "responses.py"

_CATEGORY = "AUTHORIZATION_DENIED"


def _names_denial_category(node: ast.expr) -> bool:
    """True if ``node`` is an ``ErrorCategory.AUTHORIZATION_DENIED`` attribute access."""
    return isinstance(node, ast.Attribute) and node.attr == _CATEGORY


class _DenialCallVisitor(ast.NodeVisitor):
    """Collect the line of every call that passes the denial category as an argument."""

    def __init__(self) -> None:
        self.hits: list[int] = []

    def visit_Call(self, node: ast.Call) -> None:
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        if any(_names_denial_category(arg) for arg in arguments):
            self.hits.append(node.lineno)
        self.generic_visit(node)


def test_denial_envelopes_are_built_only_by_tool_response_denied() -> None:
    """No module hand-rolls an ``authorization_denied`` envelope (#1596, ADR-0471)."""
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path == _ALLOWED:
            continue
        visitor = _DenialCallVisitor()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        offenders += [
            f"{path.relative_to(_SRC_ROOT.parents[1])}:{line}" for line in sorted(visitor.hits)
        ]
    assert not offenders, (
        "an authorization_denied envelope is being built outside ToolResponse.denied, which "
        "lets it name the tool that denied the caller (#1596); use ToolResponse.denied "
        f"instead at: {offenders}"
    )


def test_denial_breadcrumb_is_public_and_registered() -> None:
    """The fixed breadcrumb names live, public tools the denied caller can actually invoke."""
    registry = PUBLIC_TOOLS | CLASSIFIED_TOOLS
    named = set(DENIAL_NEXT_ACTIONS)
    assert named <= registry, f"denial breadcrumb names no registered tool: {named - registry}"
    assert named <= PUBLIC_TOOLS, (
        "a denial breadcrumb must be invokable by the caller that was just denied, so every "
        f"entry must be public; gated entries: {sorted(named - PUBLIC_TOOLS)}"
    )


def test_a_denial_can_never_name_a_gated_tool() -> None:
    """A denial envelope names no gated tool — so it can never name its own denier.

    Only a gated tool can deny (a public tool enforces no role), so a breadcrumb disjoint from
    ``CLASSIFIED_TOOLS`` is disjoint from the set of possible deniers.
    """
    named = set(ToolResponse.denied("any-object").suggested_next_actions)
    assert not named & CLASSIFIED_TOOLS, (
        f"denial envelope names a gated tool the caller may not hold: {sorted(named)}"
    )
