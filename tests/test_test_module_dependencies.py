"""Test support must not depend on helpers defined inside collected test modules."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parent
_TESTS_PACKAGE = "tests"


def test_test_modules_do_not_import_other_test_modules() -> None:
    offenders: list[str] = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in _collected_test_imports(node):
                relative = path.relative_to(_TESTS_ROOT.parent)
                offenders.append(f"{relative}:{node.lineno} imports {module}")

    assert offenders == [], (
        "move shared helpers into a domain support module instead of importing a collected "
        f"test module:\n{'\n'.join(offenders)}"
    )


def _collected_test_imports(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names if _is_collected_test_module(alias.name))

    prefix = "." * node.level
    if node.module is not None:
        prefix += node.module
    candidates: list[str] = []
    if node.module is not None and _is_collected_test_module(node.module, relative=node.level > 0):
        candidates.append(prefix)

    package_is_tests = (
        node.level > 0
        or node.module == "tests"
        or bool(node.module and node.module.startswith("tests."))
    )
    if package_is_tests:
        separator = "." if node.module else ""
        candidates.extend(
            f"{prefix}{separator}{alias.name}"
            for alias in node.names
            if alias.name.startswith("test_")
        )
    return tuple(candidates)


def _is_collected_test_module(module: str, *, relative: bool = False) -> bool:
    return (relative or module.startswith("tests.")) and module.rsplit(".", 1)[-1].startswith(
        "test_"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"import {_TESTS_PACKAGE}.mcp.test_tool_index", ("tests.mcp.test_tool_index",)),
        (
            f"from {_TESTS_PACKAGE}.mcp.test_tool_index import helper",
            ("tests.mcp.test_tool_index",),
        ),
        ("from tests.mcp import test_tool_index", ("tests.mcp.test_tool_index",)),
        ("from .test_tool_index import helper", (".test_tool_index",)),
        ("from . import test_tool_index", (".test_tool_index",)),
        ("from tests.mcp import auth_support", ()),
        ("from . import auth_support", ()),
    ],
)
def test_collected_test_import_detection(source: str, expected: tuple[str, ...]) -> None:
    tree = ast.parse(source)
    node = next(item for item in ast.walk(tree) if isinstance(item, (ast.Import, ast.ImportFrom)))
    assert _collected_test_imports(node) == expected
