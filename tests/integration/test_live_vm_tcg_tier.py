"""Non-gated guard: pin exactly the four proofs to both live_stack and live_vm_tcg (#1154).

Runs in ordinary CI (no live marker), like tests/images/test_exit_criteria.py's tier pin. Because
``just test-live-tcg`` tolerates "no tests collected" as a clean skip, an emptied ``-m
live_vm_tcg`` selection would read green; this guard fails at the source if a marker is dropped or
strays.
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


def _marker_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The ``@pytest.mark.NAME`` names on a function (with or without call args)."""
    names: set[str] = set()
    for dec in func.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        ):
            names.add(node.attr)
    return names


def _functions_with_marker(marker: str) -> dict[str, set[str]]:
    """Map every test function in the tree carrying ``marker`` to its full marker set."""
    found: dict[str, set[str]] = {}
    for path in _TESTS_ROOT.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # Both sync and async test defs — an async stray carrier must not evade the guard.
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                markers = _marker_names(node)
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
