"""Whole-tree sweep: a new SystemState must join every live-state set (#1254, ADR-0378).

A hand-picked list of state-keyed sites is provably incomplete — this repo's earlier snapshot work
found the console-rotate and power gates missing from one. So this guard scans **every**
``src/kdive/**/*.py`` for a set/tuple/frozenset literal of ``SystemState`` members and identifies a
"live-state set" by a structural signal: it enumerates the live System states, so it contains the
crash-window trio ``READY`` + ``CRASHING`` + ``CRASHED`` (the four such sets — admission's
quota-holding set, the reconciler's allocation-liveness set, and the two console live/seal sets —
all share it, while adjacency successor sets, terminal sets, and narrow gates do not). Every such
set must also contain every live state (``RESTORING``, ``PAUSED``) unless it is on
``INTENTIONALLY_PARTIAL`` with a reason.

Follows the whole-tree AST-scan precedent (``test_no_service_import``, ``test_provider_boundary``).
When a future ``SystemState`` value is added, extend ``_LIVE_STATES_REQUIRED`` (and, where a site
deliberately excludes it, add an allow-list entry) — a missed site fails here, not in production.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kdive"

# A literal that enumerates the live System states contains this crash-window trio.
_LIVE_STATE_SIGNAL = frozenset({"READY", "CRASHING", "CRASHED"})
# The live states such a set is expected to also contain.
_LIVE_STATES_REQUIRED = frozenset({"RESTORING", "PAUSED"})

# Live-state sets that deliberately exclude a live state, keyed by (repo-relative path, frozenset
# of the SystemState member names in the literal) -> reason. Empty today: all four live-state sets
# include RESTORING and PAUSED.
INTENTIONALLY_PARTIAL: dict[tuple[str, frozenset[str]], str] = {}


def _system_state_member(node: ast.expr) -> str | None:
    """Return ``X`` for a ``SystemState.X`` (or ``SystemState.X.value``) node, else ``None``."""
    # Unwrap a trailing ``.value``.
    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "SystemState"
    ):
        return node.attr
    return None


def _state_literals(tree: ast.AST) -> list[tuple[int, frozenset[str]]]:
    """Find set/tuple/frozenset(...) literals of SystemState members: (lineno, member names)."""
    found: list[tuple[int, frozenset[str]]] = []
    for node in ast.walk(tree):
        elements: list[ast.expr] | None = None
        lineno = 0
        if isinstance(node, ast.Set | ast.Tuple):
            elements = list(node.elts)
            lineno = node.lineno
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "frozenset"
            and node.args
            and isinstance(node.args[0], ast.Set | ast.Tuple | ast.List)
        ):
            elements = list(node.args[0].elts)
            lineno = node.lineno
        if not elements:
            continue
        members = {m for e in elements if (m := _system_state_member(e)) is not None}
        # Only a literal whose elements are *all* SystemState members is a state set (a mixed
        # literal — e.g. a tuple of (state, id) — is not a membership set).
        if members and len(members) == len(elements):
            found.append((lineno, frozenset(members)))
    return found


def test_every_live_state_set_includes_the_new_live_states() -> None:
    violations: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, members in _state_literals(tree):
            if not members >= _LIVE_STATE_SIGNAL:
                continue  # not a live-state set (no crash-window READY+CRASHING+CRASHED trio)
            missing = _LIVE_STATES_REQUIRED - members
            if not missing:
                continue
            if (rel, members) in INTENTIONALLY_PARTIAL:
                continue
            violations.append(
                f"{rel}:{lineno} live-state set {sorted(members)} is missing {sorted(missing)} "
                f"(add the state, or add an INTENTIONALLY_PARTIAL entry with a reason)"
            )
    assert not violations, "state-keyed sites missing a live state:\n" + "\n".join(violations)


def test_allowlist_entries_still_exist() -> None:
    # A stale allow-list entry silently weakens the guard; every entry must match a real literal.
    seen: set[tuple[str, frozenset[str]]] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for _, members in _state_literals(tree):
            seen.add((rel, members))
    stale = [key for key in INTENTIONALLY_PARTIAL if key not in seen]
    assert not stale, f"stale INTENTIONALLY_PARTIAL entries (no matching literal): {stale}"
