"""Layering guard: kdive.kernel_config must not import kdive.services."""

from __future__ import annotations

import ast
from pathlib import Path

_KERNEL_CONFIG = Path(__file__).resolve().parents[2] / "src" / "kdive" / "kernel_config"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_kernel_config_never_imports_services() -> None:
    offenders = {
        path.relative_to(_KERNEL_CONFIG).as_posix(): sorted(
            module
            for module in _imported_modules(path)
            if module == "kdive.services" or module.startswith("kdive.services.")
        )
        for path in sorted(_KERNEL_CONFIG.rglob("*.py"))
    }
    bad = {path: modules for path, modules in offenders.items() if modules}
    assert not bad, f"kernel_config must not import kdive.services (layering inversion): {bad}"
