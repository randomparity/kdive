"""Unit tests for the live coverage evidence seam (ADR-0715); no stack required."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.coverage_campaign.contract import build_contract, digest
from scripts.coverage_campaign.evidence import Context, EvidenceError, Outcome
from tests.integration.live_stack.evidence import (
    EvidenceWriter,
    RunIdentity,
    assemble,
    build_record,
    identity_problems,
    os_identity,
    run_identity,
)

_HEAD = "a" * 40
_OTHER = "b" * 40
_NODE = "tests/integration/live_stack/test_evidence.py::test_artifacts_are_content_addressed"
_BASE = "http://127.0.0.1:8000/mcp"
_SLOTS = "kdive-live-worker@1.service loaded active running\nkdive-live-worker@2.service x\n"


def _git(*args: str) -> str | None:
    return {"rev-parse": _HEAD, "status": ""}[args[0]]


def _identity(
    reports: dict[str, str | None] | None = None,
    *,
    git: Callable[..., str | None] = _git,
    read: Callable[[str], str | None] = lambda _path: _HEAD,
    running_workers: Callable[[], str] = lambda: _SLOTS,
) -> RunIdentity:
    served: dict[str, str | None] = {
        ":9464/": "abc1234",
        ":9466/": "abc1234",
        ":9465/": "abc1234",
        ":9470/": "abc1234",
        **(reports or {}),
    }

    def fetch(url: str) -> dict[str, object] | None:
        commit = next((v for k, v in served.items() if k in url), None)
        return None if commit is None else {"commit": commit}

    def resolve(commit: str) -> str | None:
        return {"abc1234": _HEAD, "def5678": _OTHER, _HEAD: _HEAD}.get(commit)

    return run_identity(
        _BASE,
        git=git,
        fetch=fetch,
        resolve=resolve,
        read=read,
        running_workers=running_workers,
        matrix=lambda: "c" * 64,
        os_release=lambda: 'ID=ubuntu\nVERSION_ID="26.04"\n',
    )


def test_run_identity_resolves_every_role_to_the_candidate() -> None:
    identity = _identity()
    assert identity.candidate_sha == _HEAD
    assert identity.host_os == "ubuntu:26.04"
    assert identity.deployed_roles == dict.fromkeys(
        ("server", "reconciler", "worker", "authority"), _HEAD
    )
    assert identity_problems(identity, ("server", "worker", "reconciler", "authority")) == []


def test_unreadable_roles_are_omitted_and_reported_missing() -> None:
    identity = _identity(reports={":9466/": None}, read=lambda _path: None)
    assert set(identity.deployed_roles) == {"server", "worker"}
    assert identity_problems(identity, ("server", "reconciler", "authority")) == [
        "missing:reconciler",
        "missing:authority",
    ]


def test_no_running_worker_slot_omits_the_worker_role() -> None:
    identity = _identity(running_workers=lambda: "")
    assert "worker" not in identity.deployed_roles


def test_a_stale_worker_slot_is_the_recorded_worker_revision() -> None:
    identity = _identity(reports={":9470/": "def5678"})
    assert identity.deployed_roles["worker"] == _OTHER
    assert identity_problems(identity, ("worker",)) == [f"mismatch:worker:{_OTHER}"]


def test_a_dirty_checkout_is_an_identity_problem() -> None:
    identity = _identity(git=lambda *a: {"rev-parse": _HEAD, "status": " M x"}[a[0]])
    assert identity_problems(identity, ()) == ["dirty-checkout"]


def test_os_identity_reads_quoted_and_bare_values() -> None:
    assert os_identity('NAME="Rocky"\nID="rocky"\nVERSION_ID="9.8"\n') == "rocky:9.8"
    with pytest.raises(ValueError, match="os-release"):
        os_identity("NAME=x\n")


def _record(identity: RunIdentity, writer: EvidenceWriter):
    cell = replace(
        next(c for c in build_contract().cells if c.id.startswith("image-smoke/")),
        node_id=_NODE,
    )
    context = Context(
        host_os="ubuntu:26.04", host_arch="x86_64", guest_arch="x86_64", accelerator="kvm"
    )
    artifact = writer.artifact({"probe": "ok"})
    return cell, build_record(
        cell,
        identity,
        outcome=Outcome.FAILURE,
        context=context,
        duration_s=1.5,
        assertions={"acquire": artifact},
    )


def test_records_round_trip_through_the_qualifier_reader(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    cell, record = _record(_identity(), writer)
    assert record.input_sha256 == digest(record.context)
    assert (record.node_id, record.scenario_id) == (_NODE, cell.scenario_id)
    writer.record(record)
    writer.record(record)  # a rerun replaces the cell's record
    other = record.model_copy(update={"candidate_sha": _OTHER})
    (tmp_path / "records" / "other.json").write_text(other.model_dump_json())
    out = tmp_path / "results.json"
    kept, dropped = assemble(tmp_path, _HEAD, out)
    assert (kept, dropped) == (1, 1)
    loaded = json.loads(out.read_text())
    assert [r["cell_id"] for r in loaded] == [cell.id]
    assert set(record.artifacts) == {p.name for p in (tmp_path / "artifacts").iterdir()}


def test_artifacts_are_content_addressed(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    assert writer.artifact({"a": 1, "b": 2}) == writer.artifact({"b": 2, "a": 1})
    assert writer.artifact({"a": 1}) != writer.artifact({"a": 2})


def test_assembly_refuses_an_invalid_record(tmp_path: Path) -> None:
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "bad.json").write_text(
        json.dumps({"version": 1, "candidate_sha": _HEAD})
    )
    with pytest.raises(EvidenceError):
        assemble(tmp_path, _HEAD, tmp_path / "results.json")


def test_an_unreadable_head_fails_instead_of_recording_an_empty_candidate() -> None:
    with pytest.raises(RuntimeError, match="git HEAD"):
        _identity(git=lambda *a: {"rev-parse": None, "status": ""}[a[0]])


def test_unasserted_artifacts_are_retained_on_the_record(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    cell, record = _record(_identity(), writer)
    extra = writer.artifact({"cleanup-attempt": {"error": "AssertionError"}})
    with_extra = build_record(
        cell,
        _identity(),
        outcome=Outcome.FAILURE,
        context=record.context,
        duration_s=1.0,
        assertions=record.assertions,
        artifacts=[extra],
    )
    assert set(with_extra.artifacts) == {*record.artifacts, extra}
