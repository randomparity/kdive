"""Guard against reintroducing stringified boolean flags in MCP tool ``data`` (ADR-0263).

Several MCP tools used to flatten boolean response fields to JSON strings
(``str(flag).lower()`` or ``"true" if flag else "false"``), forcing an agent to compare
``data["truncated"] == "true"`` instead of reading a native ``bool``. ADR-0263 retired that
convention. This test AST-walks ``src/kdive/mcp/tools/`` and fails if either idiom returns.

Scope limit: it catches only the two *boolean*-stringification idioms, which are statically
unambiguous. A stringified count (``str(n)``) is indistinguishable from ``str(uuid)`` /
``str(enum)`` by AST, so numeric regressions are covered by per-tool ``isinstance(..., int)``
assertions in the individual tool tests, not here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TOOLS_ROOT = Path(__file__).resolve().parents[2] / "src" / "kdive" / "mcp" / "tools"


def _is_str_call(node: ast.expr) -> bool:
    """True if ``node`` is a ``str(...)`` call."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str"


class _FlagStringificationVisitor(ast.NodeVisitor):
    """Collect ``(lineno, idiom)`` for each boolean-stringification site."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.hits: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        # str(<expr>).lower() — the `.lower()` call wrapping a str(...) call.
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "lower" and _is_str_call(func.value):
            self.hits.append((node.lineno, "str(...).lower()"))
        self.generic_visit(node)

    def _flag_literal(self, node: ast.expr) -> None:
        if isinstance(node, ast.Constant) and node.value in ("true", "false"):
            self.hits.append((node.lineno, f'"{node.value}" literal'))

    def visit_Dict(self, node: ast.Dict) -> None:
        for value in node.values:
            self._flag_literal(value)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._flag_literal(node.body)
        self._flag_literal(node.orelse)
        self.generic_visit(node)


def test_no_stringified_boolean_flags_in_tool_data() -> None:
    offenders: list[str] = []
    for path in sorted(_TOOLS_ROOT.rglob("*.py")):
        visitor = _FlagStringificationVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        rel = path.relative_to(_TOOLS_ROOT.parents[3])
        offenders.extend(f"{rel}:{lineno}: {idiom}" for lineno, idiom in visitor.hits)

    assert not offenders, (
        "stringified boolean flag(s) in MCP tool data — emit a native bool instead "
        "(ADR-0263):\n" + "\n".join(offenders)
    )
