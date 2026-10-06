from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.coverage_campaign.evidence import EvidenceError, read_bindings, read_results
from tests.scripts.coverage_support import complete_evidence


def test_json_round_trip(tmp_path: Path) -> None:
    _, bindings, results = complete_evidence()
    path = tmp_path / "inputs.json"
    path.write_text(bindings.model_dump_json())
    assert read_bindings(path) == bindings
    path.write_text(json.dumps([r.model_dump(mode="json") for r in results]))
    assert read_results(path) == results


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("version", True),
        ("candidate_sha", "main"),
        ("matrix_sha256", "ABC"),
        ("duration_seconds", -1),
        ("duration_seconds", "1"),
        ("duration_seconds", float("nan")),
        ("duration_seconds", float("inf")),
        ("unknown", "private.internal"),
        ("cell_id", "x" * 513),
        ("artifacts", ["https://private.internal/log"]),
        ("deployed_roles", {"unknown": "a" * 40}),
    ],
)
def test_invalid_evidence_fails_with_no_submitted_values(
    tmp_path: Path, field: str, value: object
) -> None:
    record = complete_evidence()[2][0].model_dump(mode="json")
    record[field] = value
    path = tmp_path / "private.json"
    path.write_text(json.dumps([record]))
    with pytest.raises(EvidenceError) as error:
        read_results(path)
    assert "private" not in str(error.value)
    assert "https:" not in str(error.value)
    assert "invalid" in str(error.value)


@pytest.mark.parametrize("value", ["private.internal", "127.0.0.1", "ubuntu:private", "x" * 256])
def test_platform_identity_excludes_private_locators(tmp_path: Path, value: str) -> None:
    record = complete_evidence()[2][0].model_dump(mode="json")
    record["context"]["host_os"] = value
    path = tmp_path / "results.json"
    path.write_text(json.dumps([record]))
    with pytest.raises(EvidenceError, match="invalid"):
        read_results(path)


@pytest.mark.parametrize(
    "text",
    [
        '[{"version":1,"version":1}]',
        '[{"assertions":{"effect":"a","effect":"b"}}]',
        "[",
        '{"private.internal":true}',
        "[NaN]",
        "[" * 2000,
    ],
)
def test_malformed_or_duplicate_json_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "results.json"
    path.write_text(text)
    with pytest.raises(EvidenceError):
        read_results(path)


def test_input_size_and_record_bounds_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.coverage_campaign import evidence

    path = tmp_path / "results.json"
    path.write_bytes(b" " * 65)
    monkeypatch.setattr(evidence, "_MAX_BYTES", 64)
    with pytest.raises(EvidenceError, match="size limit"):
        read_results(path)
    path.write_text("[{},{}]")
    monkeypatch.setattr(evidence, "_MAX_RECORDS", 1)
    with pytest.raises(EvidenceError, match="invalid-evidence"):
        read_results(path)


def test_missing_file_reports_no_private_path(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError) as error:
        read_bindings(tmp_path / "private.internal")
    assert str(error.value) == "unreadable-input: check the supplied file and permissions"


def test_every_catalog_platform_can_be_recorded() -> None:
    from scripts.coverage_campaign.contract import read_inventory
    from scripts.coverage_campaign.evidence import Context

    for entry in read_inventory().images.values():
        Context(host_os=f"{entry.distro}:{entry.version}", host_arch="x86_64", accelerator="none")


def test_manifest_and_evidence_share_dotted_parameter_node_ids() -> None:
    from scripts.coverage_campaign.contract import _validate_node
    from scripts.coverage_campaign.evidence import Evidence

    node = (
        "tests/scripts/test_coverage_evidence.py::"
        "test_platform_identity_excludes_private_locators[private.internal]"
    )
    _validate_node(node)
    record = complete_evidence()[2][0].model_dump(mode="json")
    record["node_id"] = node
    assert Evidence.model_validate(record).node_id == node
