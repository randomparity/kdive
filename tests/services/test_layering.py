"""Layering guard: kdive.services must not import kdive.mcp."""

from __future__ import annotations

import ast
from pathlib import Path

_SERVICES = Path(__file__).resolve().parents[2] / "src" / "kdive" / "services"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_services_never_import_mcp() -> None:
    offenders = {
        path.relative_to(_SERVICES).as_posix(): sorted(
            m for m in _imported_modules(path) if m == "kdive.mcp" or m.startswith("kdive.mcp.")
        )
        for path in sorted(_SERVICES.rglob("*.py"))
    }
    bad = {p: ms for p, ms in offenders.items() if ms}
    assert not bad, f"services must not import kdive.mcp (layering inversion): {bad}"


def test_shared_system_teardown_policy_has_a_service_owner() -> None:
    path = _SERVICES / "systems/teardown.py"
    assert path.is_file(), "shared teardown routing belongs in a concrete service"
    from kdive.services.systems.teardown import route_external_boot_teardown

    assert route_external_boot_teardown.__module__ == "kdive.services.systems.teardown"


def test_breakglass_teardown_imports_policy_from_services() -> None:
    path = _SERVICES.parent / "mcp/tools/ops/security/breakglass.py"
    imports = _imported_modules(path)
    assert "kdive.mcp.tools.lifecycle.systems.admin" not in imports
    assert "kdive.services.systems.teardown" in imports
