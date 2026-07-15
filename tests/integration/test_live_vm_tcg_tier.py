"""Non-gated guard: pin exactly the four proofs to both live_stack and live_vm_tcg (#1154).

Runs in ordinary CI (no live marker), like tests/images/test_exit_criteria.py's tier pin. Because
``just test-live-tcg`` tolerates "no tests collected" as a clean skip, an emptied ``-m
live_vm_tcg`` selection would read green; this guard fails at the source if a marker is dropped or
strays.

It reads markers from **both** idioms the repo uses — per-function ``@pytest.mark.NAME`` decorators
and a module-level ``pytestmark = ...`` assignment (single mark or a list/tuple) — so a future
proof added via the module-level form cannot evade the pin.
"""

from __future__ import annotations

import ast
import pathlib

_TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
_EXPECTED = {
    "test_ppc64le_guest_is_ssh_reachable_over_the_wire",
    "test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire",
    "test_ppc64le_kdump_captures_a_vmcore_under_tcg",
    "test_ppc64le_fadump_captures_a_vmcore_under_tcg",
}


def _mark_name(node: ast.expr) -> str | None:
    """The ``NAME`` in a ``pytest.mark.NAME`` expression (bare or called), else ``None``."""
    target = node.func if isinstance(node, ast.Call) else node
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "mark"
    ):
        return target.attr
    return None


def _marks_in(node: ast.expr) -> set[str]:
    """Mark names in a single mark expr or a list/tuple of them (a decorator or ``pytestmark``)."""
    exprs = node.elts if isinstance(node, ast.List | ast.Tuple) else [node]
    return {name for expr in exprs if (name := _mark_name(expr)) is not None}


def _module_markers(tree: ast.Module) -> set[str]:
    """Marks from a top-level ``pytestmark = ...`` assignment (single mark or list/tuple)."""
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


def _functions_with_marker(marker: str) -> dict[str, set[str]]:
    """Map every test function in the tree whose effective markers include ``marker``.

    Effective markers = module-level ``pytestmark`` ∪ the function's own decorators. Only functions
    named ``test*`` (pytest's collection convention) are considered, so helpers are ignored; both
    sync and async defs are walked so an async carrier cannot evade the guard.
    """
    found: dict[str, set[str]] = {}
    for path in _TESTS_ROOT.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_marks = _module_markers(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                markers = module_marks | {
                    name for dec in node.decorator_list for name in _marks_in(dec)
                }
                if marker in markers:
                    found[node.name] = markers
    return found


def test_exactly_the_four_proofs_carry_live_vm_tcg() -> None:
    carriers = _functions_with_marker("live_vm_tcg")
    assert set(carriers) == _EXPECTED, (
        "live_vm_tcg must tag exactly the four ppc64le spine proofs; "
        f"unexpected/missing: {set(carriers) ^ _EXPECTED}"
    )


def test_each_live_vm_tcg_proof_is_also_live_stack() -> None:
    carriers = _functions_with_marker("live_vm_tcg")
    for name, markers in carriers.items():
        assert "live_stack" in markers, f"{name} carries live_vm_tcg but not live_stack"


def test_no_live_vm_tcg_proof_is_also_native_live_vm() -> None:
    # The tiers are disjoint: a live_vm_tcg proof must NOT carry live_vm, or `just test-live`
    # (-m "live_vm and not live_vm_tcg") would still exclude it but the marker intent would be
    # muddied. Pin the disjointness at the source rather than leaning only on the recipe filter.
    carriers = _functions_with_marker("live_vm_tcg")
    for name, markers in carriers.items():
        assert "live_vm" not in markers, f"{name} carries both live_vm and live_vm_tcg"
