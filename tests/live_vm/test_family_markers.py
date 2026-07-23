"""Non-gated guard: the live_vm family sub-markers are additive (#1290, epic #1289).

Every test carrying live_vm_throwaway, live_vm_provisioned, or live_vm_remote must ALSO carry the
bare live_vm marker, so `-m live_vm` still selects every family and the shipped test-live recipe
(-m "live_vm and not live_vm_tcg") is unaffected. This asserts additivity only — NOT completeness
(every live_vm test has a family sub-marker): the debug/panic tests are un-migrated until sub-issue
E, and some live_vm tests (e.g. the retained-vmcore introspect test) fit neither family, so a
completeness guard would red-fail now. Runs in ordinary CI, like test_live_vm_tcg_tier.py.
"""

from __future__ import annotations

import ast
import pathlib
from functools import cache

_TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
_FAMILY_SUBMARKERS = ("live_vm_throwaway", "live_vm_provisioned", "live_vm_remote")


def _mark_name(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "mark"
    ):
        return target.attr
    return None


def _marks_in(node: ast.expr) -> set[str]:
    exprs = node.elts if isinstance(node, ast.List | ast.Tuple) else [node]
    return {name for expr in exprs if (name := _mark_name(expr)) is not None}


def _module_markers(tree: ast.Module) -> set[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is not None and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets
        ):
            return _marks_in(node.value)
    return set()


@cache
def _functions_with_any(markers: tuple[str, ...]) -> dict[str, set[str]]:
    # Effective markers = module-level ``pytestmark`` ∪ the function's own decorators. Class-scoped
    # ``pytestmark`` and class-decorator markers are NOT walked (mirrors test_live_vm_tcg_tier.py):
    # every kdive live_vm test is a module-level function, so a class-based test would be a new
    # convention to handle here first. If one is ever added, extend this walk to ClassDef scope.
    found: dict[str, set[str]] = {}
    for path in _TESTS_ROOT.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_marks = _module_markers(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                effective = module_marks | {
                    name for dec in node.decorator_list for name in _marks_in(dec)
                }
                if effective & set(markers):
                    found[node.name] = effective
    return found


def test_every_family_submarker_test_also_carries_live_vm() -> None:
    carriers = _functions_with_any(_FAMILY_SUBMARKERS)
    offenders = {name for name, marks in carriers.items() if "live_vm" not in marks}
    assert not offenders, (
        "live_vm_throwaway/live_vm_provisioned/live_vm_remote are ADDITIVE — every carrier must "
        f"also carry the bare live_vm marker; missing on: {sorted(offenders)}"
    )
