"""Layering guard: kdive.inventory must not import kdive.mcp (ADR-0115 §1).

The shared name/coeff rule lives in kdive.domain precisely so the inventory model can
validate a [[cost_class]] without importing kdive.mcp.tools.ops — a core→tool inversion.
This static check walks every inventory module's imports and fails if any reaches kdive.mcp.
"""

from __future__ import annotations

import ast
from pathlib import Path

_INVENTORY = Path(__file__).resolve().parents[2] / "src" / "kdive" / "inventory"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_inventory_never_imports_mcp() -> None:
    offenders = {
        path.relative_to(_INVENTORY).as_posix(): sorted(
            m for m in _imported_modules(path) if m == "kdive.mcp" or m.startswith("kdive.mcp.")
        )
        for path in sorted(_INVENTORY.rglob("*.py"))
    }
    bad = {p: ms for p, ms in offenders.items() if ms}
    assert not bad, f"inventory must not import kdive.mcp (layering inversion): {bad}"
