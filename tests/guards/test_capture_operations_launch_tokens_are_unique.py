"""Guard: no capture-operations test hard-codes a well-formed launch token (#2063).

``scan_launch_token`` enumerates all of ``/proc`` and matches every process sharing the
caller's euid and interpreter whose argv carries the token, and the launcher's cleanup path
SIGKILLs each match. Production is safe because
``0112_capture_operation_supervision.sql`` declares ``launch_token`` UNIQUE, so no two
operations ever hold the same one.

Tests broke that invariant by sharing the literal ``"a" * 64``. Under xdist every worker
runs as the same uid with the same interpreter, so one worker's cleanup scan matched another
worker's live capture-bootstrap child and killed it; the victim failed with ``-9``. The
failure only reproduces on a host with enough cores to run the colliding tests at the same
instant, so a returning literal would pass CI and redden on a developer's machine.

The fix is the ``launch_token`` fixture in ``tests/jobs/capture_operations/conftest.py``.
This guard is what keeps it: a future test copying a token literal from a neighbour reddens
here instead of reintroducing a cross-worker kill.

The check is default-deny — *any* constant that folds to a well-formed token is rejected,
whatever position it occupies. A token reaches the scan through a keyword, a spelled-out
argv, a bare positional argument, or a local alias, and an allowlist of positions missed
several of those. The only exemption is a request digest, which is a content hash that never
enters a process argv and so cannot collide.

A deliberately malformed constant is also fine: the product's own validator refuses it, and
it can never match a live child, which by construction carries a well-formed token.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_CAPTURE_OPERATION_TESTS = Path(__file__).parents[1] / "jobs" / "capture_operations"
_WELL_FORMED_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_NAMES = frozenset({"request_digest"})


def _folded_text(node: ast.expr) -> str | None:
    """Fold the constant expressions tests actually write, or return None.

    Covers plain ``str``/``bytes`` literals and the ``"a" * 64`` repetition form that caused
    #2063 — ``ast.literal_eval`` rejects the latter, so fold it here rather than reach for it.
    Bytes are decoded because an argv assertion spells the token as ``b"a" * 64``.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        if isinstance(node.value, bytes):
            return node.value.decode("ascii", "replace")
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        for text_node, count_node in ((node.left, node.right), (node.right, node.left)):
            count = getattr(count_node, "value", None)
            if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 1024:
                continue
            text = _folded_text(text_node) if isinstance(text_node, ast.Constant) else None
            if text is not None:
                return text * count
    return None


def _digest_expressions(tree: ast.AST) -> set[int]:
    """Node ids of expressions supplying a request digest, which is never scanned."""
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            exempt.update(
                id(keyword.value) for keyword in node.keywords if keyword.arg in _DIGEST_NAMES
            )
        elif isinstance(node, ast.Dict):
            exempt.update(
                id(value)
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and key.value in _DIGEST_NAMES
            )
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in _DIGEST_NAMES for target in node.targets
        ):
            exempt.add(id(node.value))
    return exempt


def _offending_lines(source: str) -> list[int]:
    tree = ast.parse(source)
    exempt = _digest_expressions(tree)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant | ast.BinOp)
        and id(node) not in exempt
        and (folded := _folded_text(node)) is not None
        and _WELL_FORMED_TOKEN.match(folded)
    )


@pytest.mark.parametrize(
    "module",
    sorted(_CAPTURE_OPERATION_TESTS.glob("*.py")),
    ids=lambda path: path.name,
)
def test_capture_operations_tests_take_launch_tokens_from_the_fixture(module: Path) -> None:
    offenders = _offending_lines(module.read_text())
    assert offenders == [], (
        f"{module.relative_to(_CAPTURE_OPERATION_TESTS.parents[1])} hard-codes a well-formed "
        f"launch token at line(s) {offenders}. Two tests holding one token let a cleanup scan "
        "on one xdist worker SIGKILL another worker's capture child (#2063) — request the "
        "`launch_token` fixture from tests/jobs/capture_operations/conftest.py instead."
    )


def test_guard_rejects_the_token_literals_that_caused_the_defect() -> None:
    """The guard has to bite on every shape #2063 removed, and only on those."""
    # Keyword, spelled-out argv (str and bytes), dict value, bare positional, local alias.
    assert _offending_lines('_operation(launch_token="a" * 64)') == [1]
    assert _offending_lines('main(["--launch-token", "b" * 64, "--gate-fd", "9"])') == [1]
    assert _offending_lines('assert argv == [b"--launch-token", b"c" * 64]') == [1]
    assert _offending_lines('assert vars(args) == {"launch_token": "d" * 64}') == [1]
    assert _offending_lines('child.run_capture_child("e" * 64, -1)') == [1]
    assert _offending_lines('token = "0123456789abcdef" * 4\nscan(token)') == [1]

    # A token taken from the fixture, or from the operation being restated, is the fix.
    assert _offending_lines("_operation(launch_token=launch_token)") == []
    assert _offending_lines("_operation(launch_token=operation.launch_token)") == []
    # Constants that are not well-formed tokens cannot match a live child.
    assert _offending_lines('scan_launch_token(token="not-a-hex-token")') == []
    assert _offending_lines('_operation(launch_token="A" * 64)') == []
    assert _offending_lines('_operation(launch_token="a" * 63)') == []
    # A digest is never scanned, so a shared constant one is harmless.
    assert _offending_lines('_operation(request_digest="a" * 64)') == []
    assert _offending_lines('replace(operation, request_digest="0" * 64)') == []
    assert _offending_lines('request_digest = "a" * 64') == []
