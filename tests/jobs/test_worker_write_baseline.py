"""Structural checks for the worker-handler write baseline (#2345)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.domain.operations.jobs import ACTIVE_JOB_KINDS

_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_PATH = _ROOT / "tests/jobs/worker_write_baseline.json"
_EVIDENCE_FIELDS = ("handler_source", "write_source", "authority_source", "grant_source")
_ROUTES = {"direct", "security-definer"}
_VERDICTS = {"covered", "definer-mediated", "LEAK"}
_WRITE_FIELDS = {
    "id",
    "table",
    "operation",
    "handler_source",
    "write_source",
    "role",
    "route",
    "authority_source",
    "grant_source",
    "verdict",
}


def _load_baseline(path: Path = _BASELINE_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text()))


def _evidence_violations(value: object, field: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be an object"]
    evidence = cast(dict[str, object], value)
    if set(evidence) != {"path", "line", "text"}:
        return [f"{field} must contain path, line, and text"]

    path = evidence["path"]
    line = evidence["line"]
    text = evidence["text"]
    if not isinstance(path, str) or not path:
        return [f"{field}.path must be a non-empty string"]
    if Path(path).is_absolute() or ".." in Path(path).parts:
        return [f"{field}.path must be repository-relative"]
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return [f"{field}.line must be a positive integer"]
    if not isinstance(text, str) or not text:
        return [f"{field}.text must be a non-empty string"]

    source_path = _ROOT / path
    if not source_path.is_file():
        return [f"{field}.path does not name a file: {path}"]
    source_lines = source_path.read_text().splitlines()
    if line > len(source_lines) or text not in source_lines[line - 1]:
        return [f"{field} does not match {path}:{line}"]
    return []


def _violations(baseline: object) -> list[str]:
    if not isinstance(baseline, dict):
        return ["baseline must be an object"]
    record = cast(dict[str, object], baseline)
    if set(record) != {"format_version", "handlers", "confirmed_leaks"}:
        return ["baseline must contain format_version, handlers, and confirmed_leaks"]
    if record["format_version"] != 1:
        return ["format_version must be 1"]

    handlers = record["handlers"]
    confirmed_leaks = record["confirmed_leaks"]
    if not isinstance(handlers, list):
        return ["handlers must be a list"]
    if not isinstance(confirmed_leaks, list) or not all(
        isinstance(leak, str) for leak in confirmed_leaks
    ):
        return ["confirmed_leaks must be a list of strings"]

    violations: list[str] = []
    job_kinds: list[str] = []
    leak_ids: list[str] = []
    write_ids: list[str] = []
    for handler in handlers:
        if not isinstance(handler, dict) or set(handler) != {"job_kind", "writes"}:
            violations.append("each handler must contain job_kind and writes")
            continue
        handler_record = cast(dict[str, object], handler)
        job_kind = handler_record["job_kind"]
        writes = handler_record["writes"]
        if not isinstance(job_kind, str) or not job_kind:
            violations.append("handler job_kind must be a non-empty string")
            continue
        job_kinds.append(job_kind)
        if not isinstance(writes, list):
            violations.append(f"{job_kind}.writes must be a list")
            continue

        previous_write_id = ""
        for write in writes:
            if not isinstance(write, dict) or set(write) != _WRITE_FIELDS:
                violations.append(f"{job_kind} write must contain the required fields")
                continue
            write_record = cast(dict[str, object], write)
            write_id = write_record["id"]
            if not isinstance(write_id, str) or not write_id:
                violations.append(f"{job_kind} write id must be a non-empty string")
                continue
            if write_id <= previous_write_id:
                violations.append(f"{job_kind} writes must be sorted by id")
            previous_write_id = write_id
            write_ids.append(write_id)
            if write_record["role"] != "kdive_worker":
                violations.append(f"{write_id} role must be kdive_worker")
            if write_record["route"] not in _ROUTES:
                violations.append(f"{write_id} route is invalid")
            if write_record["verdict"] not in _VERDICTS:
                violations.append(f"{write_id} verdict is invalid")
            if not isinstance(write_record["table"], str) or not write_record["table"]:
                violations.append(f"{write_id} table must be a non-empty string")
            if write_record["operation"] not in {"INSERT", "UPDATE", "DELETE"}:
                violations.append(f"{write_id} operation is invalid")
            for field in _EVIDENCE_FIELDS:
                violations.extend(
                    f"{write_id}: {message}"
                    for message in _evidence_violations(write_record[field], field)
                )
            if write_record["route"] == "direct" and write_record["verdict"] == "definer-mediated":
                violations.append(f"{write_id} direct route cannot be definer-mediated")
            if (
                write_record["route"] == "security-definer"
                and write_record["verdict"] != "definer-mediated"
            ):
                violations.append(f"{write_id} security-definer route must be definer-mediated")
            if write_record["verdict"] == "LEAK":
                leak_ids.append(write_id)

    expected_job_kinds = sorted(kind.value for kind in ACTIVE_JOB_KINDS)
    if job_kinds != expected_job_kinds:
        violations.append("handlers must be sorted and cover exactly ACTIVE_JOB_KINDS")
    if len(write_ids) != len(set(write_ids)):
        violations.append("write ids must be unique")
    if confirmed_leaks != sorted(leak_ids):
        violations.append("confirmed_leaks must equal the sorted LEAK write ids")
    return violations


def test_committed_baseline_is_complete_and_valid() -> None:
    assert _violations(_load_baseline()) == []


def _first_writes(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    for handler in baseline["handlers"]:
        if handler["writes"]:
            return cast(list[dict[str, Any]], handler["writes"])
    raise AssertionError("baseline has no write records")


def _first_write(baseline: dict[str, Any]) -> dict[str, Any]:
    return _first_writes(baseline)[0]


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda baseline: baseline["handlers"].pop(), "handlers must be sorted"),
        (
            lambda baseline: _first_writes(baseline).append(deepcopy(_first_write(baseline))),
            "writes must be sorted",
        ),
        (
            lambda baseline: _first_write(baseline).__setitem__("role", "kdive_server"),
            "role must be kdive_worker",
        ),
        (
            lambda baseline: _first_write(baseline)["write_source"].__setitem__(
                "text", "not-a-write"
            ),
            "write_source does not match",
        ),
    ],
)
def test_validation_rejects_structural_mutations(mutator: Any, expected: str) -> None:
    baseline = _load_baseline()
    mutator(baseline)
    assert any(expected in violation for violation in _violations(baseline))


def test_validation_rejects_leak_list_mismatch() -> None:
    baseline = _load_baseline()
    baseline["confirmed_leaks"] = ["not-a-recorded-leak"]
    assert "confirmed_leaks must equal the sorted LEAK write ids" in _violations(baseline)
