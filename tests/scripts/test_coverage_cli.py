from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.coverage_campaign import __main__ as cli
from scripts.coverage_campaign.contract import build_contract
from tests.scripts.coverage_support import complete_evidence


def test_real_fast_check_distinguishes_ownership_from_proof(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["check"]) == 0
    output = capsys.readouterr().out
    assert "126 tools" in output
    assert "21 catalog images" in output
    assert "qualification unproven" in output


@pytest.mark.parametrize(
    "fault", [None, "missing", "skip", "stale-fixture", "deployed-sha", "failure"]
)
def test_cli_positive_and_controlled_negative_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str | None,
) -> None:
    contract, inputs, results = complete_evidence()
    monkeypatch.setattr(cli, "build_contract", lambda: contract)
    rows = [r.model_dump(mode="json") for r in results]
    if fault == "missing":
        rows.pop(0)
    elif fault == "skip":
        rows[0]["outcome"] = "not-run"
    elif fault == "stale-fixture":
        rows[0]["context"]["image_sha256"] = "d" * 64
    elif fault == "deployed-sha":
        rows[0]["deployed_roles"]["worker"] = "d" * 40
    elif fault == "failure":
        rows[0]["outcome"] = "failure"
    binding_path, result_path = tmp_path / "inputs.json", tmp_path / "results.json"
    binding_path.write_text(inputs.model_dump_json())
    result_path.write_text(json.dumps(rows))
    assert cli.main(["qualify", "--inputs", str(binding_path), "--results", str(result_path)]) == (
        0 if fault is None else 1
    )
    output = capsys.readouterr().out
    assert all(c.id in output for c in contract.cells)


def test_shipped_pending_set_reports_every_missing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = build_contract()
    monkeypatch.setattr(cli, "build_contract", lambda: contract)
    inputs = tmp_path / "inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "version": 1,
                "candidate_sha": "a" * 40,
                "matrix_sha256": contract.matrix_sha256,
                "cells": {},
            }
        )
    )
    results = tmp_path / "results.json"
    results.write_text("[]")
    assert cli.main(["qualify", "--inputs", str(inputs), "--results", str(results)]) == 1
    output = capsys.readouterr().out
    pending = [c for c in contract.cells if c.node_id is None]
    assert output.count("pending-implementation") == len(pending)
    assert output.count("missing-result") == len(contract.cells)
    assert "success=0" in output
    assert f"not-run={len(contract.cells)}" in output


def test_cli_parser_error_is_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "private.internal"
    path.write_text('{"unknown":"do-not-print"}')
    assert cli.main(["qualify", "--inputs", str(path), "--results", str(path)]) == 2
    error = capsys.readouterr().err
    assert "invalid-evidence" in error
    assert "private" not in error and "do-not-print" not in error


def test_catalog_validation_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kdive.domain.errors import CategorizedError, ErrorCategory

    def broken_contract():
        raise CategorizedError("private diagnostic", category=ErrorCategory.CONFIGURATION_ERROR)

    monkeypatch.setattr(cli, "build_contract", broken_contract)
    assert cli.main(["check"]) == 2
    output = capsys.readouterr()
    assert "invalid-contract" in output.err and "private" not in output.err
