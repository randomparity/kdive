"""Structural coverage for the handler worker-role execution inventory (#2347)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_INVENTORY = _ROOT / "tests/jobs/handlers/worker_role_inventory.json"
_CLASSES = {"worker-act", "owner-only"}


def _load(path: Path = _INVENTORY) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text()))


def _violations(inventory: object) -> list[str]:
    if not isinstance(inventory, dict) or set(inventory) != {"format_version", "modules"}:
        return ["inventory must contain format_version and modules"]
    record = cast(dict[str, object], inventory)
    if record["format_version"] != 1:
        return ["format_version must be 1"]
    modules = record["modules"]
    if not isinstance(modules, list):
        return ["modules must be a list"]
    expected = sorted(
        str(path.relative_to(_ROOT)) for path in (_ROOT / "tests/jobs/handlers").rglob("test_*.py")
    )
    paths: list[str] = []
    violations: list[str] = []
    for module in modules:
        if not isinstance(module, dict):
            violations.append("module must be an object")
            continue
        classification = module.get("classification")
        path = module.get("path")
        evidence = module.get("evidence")
        if classification not in _CLASSES:
            violations.append("module classification is invalid")
            continue
        if not isinstance(path, str) or not path:
            violations.append("module path must be a non-empty string")
            continue
        paths.append(path)
        source = _ROOT / path
        if not source.is_file():
            violations.append(f"module path does not name a file: {path}")
            continue
        if not isinstance(evidence, str) or not evidence or evidence not in source.read_text():
            violations.append(f"module evidence does not match: {path}")
        if classification == "worker-act" and "reason" in module:
            violations.append("worker-act module cannot carry an owner-only reason")
        if classification == "owner-only" and not isinstance(module.get("reason"), str):
            violations.append("owner-only module needs a reason")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        violations.append("module paths must be unique and sorted")
    if paths != expected:
        violations.append("module paths must cover every handler test module")
    return violations


def test_committed_inventory_is_complete_and_valid() -> None:
    assert _violations(_load()) == []


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda inventory: inventory["modules"].pop(), "cover every handler test module"),
        (
            lambda inventory: inventory["modules"].append(deepcopy(inventory["modules"][0])),
            "unique and sorted",
        ),
        (
            lambda inventory: inventory["modules"][0].__setitem__("classification", "owner"),
            "classification is invalid",
        ),
        (
            lambda inventory: inventory["modules"][0].__setitem__("evidence", "absent-token"),
            "evidence does not match",
        ),
    ],
)
def test_inventory_rejects_structural_mutations(mutator: Any, expected: str) -> None:
    inventory = _load()
    mutator(inventory)
    assert any(expected in violation for violation in _violations(inventory))
