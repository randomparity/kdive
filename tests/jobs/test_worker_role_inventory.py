"""Structural coverage for the handler worker-role execution inventory (#2347)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.domain.operations.jobs import ACTIVE_JOB_KINDS

_ROOT = Path(__file__).resolve().parents[2]
_INVENTORY = _ROOT / "tests/jobs/handlers/worker_role_inventory.json"
_CLASSES = {"worker-act", "owner-only"}
_PRIVILEGES = {"INSERT", "UPDATE", "DELETE"}
_ROUTES = {"direct", "security-definer"}


def _load(path: Path = _INVENTORY) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text()))


def _violations(inventory: object) -> list[str]:
    if not isinstance(inventory, dict) or set(inventory) != {
        "format_version",
        "modules",
        "worker_write_coverage",
    }:
        return ["inventory must contain format_version, modules, and worker_write_coverage"]
    record = cast(dict[str, object], inventory)
    if record["format_version"] != 2:
        return ["format_version must be 2"]
    modules = record["modules"]
    coverage = record["worker_write_coverage"]
    if not isinstance(modules, list):
        return ["modules must be a list"]
    if not isinstance(coverage, list):
        return ["worker_write_coverage must be a list"]
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
    coverage_ids: list[str] = []
    job_kinds = {kind.value for kind in ACTIVE_JOB_KINDS}
    for entry in coverage:
        if not isinstance(entry, dict):
            violations.append("worker write coverage must be an object")
            continue
        write = cast(dict[str, object], entry)
        route = write.get("route")
        expected_fields = {"id", "job_kind", "table", "privilege", "route"}
        if route == "security-definer":
            expected_fields.add("function")
        if set(write) != expected_fields:
            violations.append("worker write coverage has invalid fields")
            continue
        write_id = write["id"]
        if not isinstance(write_id, str) or not write_id:
            violations.append("worker write coverage id must be a non-empty string")
            continue
        coverage_ids.append(write_id)
        if write["job_kind"] not in job_kinds:
            violations.append(f"{write_id} job kind is not active")
        table = write["table"]
        if not isinstance(table, str) or not table.startswith("public."):
            violations.append(f"{write_id} table must be public-qualified")
        if write["privilege"] not in _PRIVILEGES:
            violations.append(f"{write_id} privilege is invalid")
        if route not in _ROUTES:
            violations.append(f"{write_id} route is invalid")
        if route == "security-definer" and write["function"] != (
            "public.discharge_system_mutation_obligations(uuid)"
        ):
            violations.append(f"{write_id} function is not an approved signature")
    if coverage_ids != sorted(coverage_ids) or len(coverage_ids) != len(set(coverage_ids)):
        violations.append("worker write coverage ids must be unique and sorted")
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
        (
            lambda inventory: inventory["worker_write_coverage"].append(
                deepcopy(inventory["worker_write_coverage"][0])
            ),
            "worker write coverage ids must be unique and sorted",
        ),
        (
            lambda inventory: inventory["worker_write_coverage"][0].__setitem__(
                "privilege", "SELECT"
            ),
            "privilege is invalid",
        ),
        (
            lambda inventory: next(
                entry
                for entry in inventory["worker_write_coverage"]
                if entry["route"] == "security-definer"
            ).__setitem__("function", "public.not_a_worker_boundary(uuid)"),
            "function is not an approved signature",
        ),
    ],
)
def test_inventory_rejects_structural_mutations(mutator: Any, expected: str) -> None:
    inventory = _load()
    mutator(inventory)
    assert any(expected in violation for violation in _violations(inventory))
