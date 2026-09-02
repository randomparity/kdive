"""Closed provider-private documents for the ADR-0585 appliance protocol."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import unicodedata
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV1,
    RemoteModuleResultV1,
    identity_for,
)

ROOT = Path(__file__).resolve().parents[5]
APPLIANCE = ROOT / "deploy" / "remote_module_appliance"
SYSTEM_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000002"
DIGESTS = {letter: "sha256:" + letter * 64 for letter in "abcdef"}
NONCE = "b" * 32


def _appliance_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "task_1_remote_module_appliance", APPLIANCE / "appliance.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operation(operation: str = "capture_install", **changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol": "remote-module-operation-v1",
        "operation": operation,
        "system_id": SYSTEM_ID,
        "run_id": RUN_ID,
        "plan_identity": DIGESTS["a"],
        "operation_nonce": NONCE,
        "release": "6.12.0-kdive",
        "root_volume": {"key": "root-1", "identity": DIGESTS["c"]},
        "source_manifest": DIGESTS["d"],
        "appliance_image_digest": DIGESTS["e"],
    }
    document.update(changes)
    return document


def _result(**changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol": "remote-module-result-v1",
        "status": "success",
        "phase": "installed",
        "system_id": SYSTEM_ID,
        "run_id": RUN_ID,
        "plan_identity": DIGESTS["a"],
        "operation_nonce": NONCE,
        "appliance_image_digest": DIGESTS["e"],
        "release": "6.12.0-kdive",
        "root_volume_key": "root-1",
        "root_volume_identity": DIGESTS["c"],
        "source_manifest": DIGESTS["d"],
        "installed_manifest": DIGESTS["f"],
        "capture_manifest": DIGESTS["b"],
        "entry_count": 12,
        "content_bytes": 4096,
    }
    document.update(changes)
    return document


@pytest.mark.parametrize(
    "document",
    [
        _operation(),
        _operation(
            "restore",
            capture_manifest=DIGESTS["b"],
            installed_manifest=DIGESTS["f"],
        ),
        _operation(
            "restore",
            capture_absent=True,
            installed_manifest=DIGESTS["f"],
        ),
    ],
)
def test_operation_round_trips_canonical_json_and_matches_appliance_schema(
    document: dict[str, object],
) -> None:
    operation = RemoteModuleOperationV1.model_validate(document)
    encoded = operation.to_canonical_json()

    assert encoded == json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    assert RemoteModuleOperationV1.from_canonical_json(encoded) == operation
    schema = json.loads((APPLIANCE / "operation-v1.schema.json").read_text())
    assert not list(Draft202012Validator(schema).iter_errors(json.loads(encoded)))
    assert _appliance_module()._validate_operation(json.loads(encoded)) == document


def test_identity_is_stable_sha256_over_protocol_nul_canonical_json() -> None:
    operation = RemoteModuleOperationV1.model_validate(_operation())

    expected = hashlib.sha256(
        b"remote-module-operation-v1\0" + operation.to_canonical_json()
    ).hexdigest()
    assert identity_for(operation) == f"sha256:{expected}"
    reopened = RemoteModuleOperationV1.from_canonical_json(operation.to_canonical_json())
    assert identity_for(reopened) == identity_for(operation)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("system_id", "not-a-uuid"),
        ("run_id", "00000000-0000-0000-0000-000000000002"),
        ("plan_identity", "sha256:" + "A" * 64),
        ("operation_nonce", "b" * 31),
        ("release", "../escape"),
        ("source_manifest", "sha1:" + "d" * 64),
    ],
)
def test_operation_rejects_invalid_closed_identifiers(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        RemoteModuleOperationV1.model_validate(_operation(**{field: value}))


def test_documents_reject_extra_fields_unknown_versions_and_noncanonical_json() -> None:
    with pytest.raises(ValidationError):
        RemoteModuleOperationV1.model_validate(_operation(host_path="/var/lib/libvirt/images/root"))
    with pytest.raises(ValidationError):
        RemoteModuleOperationV1.model_validate(_operation(protocol="remote-module-operation-v2"))
    operation = RemoteModuleOperationV1.model_validate(_operation())
    with pytest.raises(ValueError, match="canonical"):
        RemoteModuleOperationV1.from_canonical_json(
            json.dumps(operation.model_dump(mode="json"), indent=2).encode()
        )


def test_operation_parser_enforces_16_kibibyte_cap_before_validation() -> None:
    with pytest.raises(ValueError, match="16384"):
        RemoteModuleOperationV1.from_canonical_json(b" " * 16_385)


def test_wire_form_is_the_framed_file_the_appliance_reader_accepts(tmp_path: Path) -> None:
    appliance = _appliance_module()
    operation = RemoteModuleOperationV1.model_validate(_operation())
    framed = operation.to_wire_bytes()
    path = tmp_path / "operation-v1.json"

    assert framed == operation.to_canonical_json() + b"\n"
    path.write_bytes(framed)
    assert appliance._read_operation(path) == _operation()

    path.write_bytes(operation.to_canonical_json())
    with pytest.raises(appliance.ApplianceError, match="INVALID_DOCUMENT"):
        appliance._read_operation(path)


def test_wire_parser_reads_back_what_the_appliance_writes() -> None:
    result = RemoteModuleResultV1.model_validate(_result())
    appliance_written = (
        json.dumps(json.loads(result.to_canonical_json()), sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()

    assert RemoteModuleResultV1.from_wire_bytes(appliance_written) == result
    assert RemoteModuleResultV1.from_wire_bytes(result.to_wire_bytes()) == result


@pytest.mark.parametrize("framing", [b"", b"\n\n"])
def test_wire_parser_names_a_framing_error_rather_than_a_canonical_one(framing: bytes) -> None:
    operation = RemoteModuleOperationV1.model_validate(_operation())

    with pytest.raises(ValueError, match="not newline-framed"):
        RemoteModuleOperationV1.from_wire_bytes(operation.to_canonical_json() + framing)
    with pytest.raises(ValueError, match="not canonical JSON"):
        RemoteModuleOperationV1.from_wire_bytes(operation.to_canonical_json() + b" \n")


def test_wire_parser_bounds_the_file_at_the_appliance_document_limit() -> None:
    with pytest.raises(ValueError, match="framed remote module document exceeds 16384 bytes"):
        RemoteModuleResultV1.from_wire_bytes(b" " * 16_384 + b"\n")


@pytest.mark.parametrize(
    "document",
    [
        _operation("restore", capture_absent=False, installed_manifest=DIGESTS["f"]),
        _result(capture_absent=False),
    ],
)
def test_documents_reject_capture_absent_false(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        if document["protocol"] == "remote-module-operation-v1":
            RemoteModuleOperationV1.model_validate(document)
        else:
            RemoteModuleResultV1.model_validate(document)


def test_identity_absent_early_failure_result_is_accepted_for_any_operation() -> None:
    operation = RemoteModuleOperationV1.model_validate(_operation())
    result = RemoteModuleResultV1.model_validate(
        {
            "protocol": "remote-module-result-v1",
            "status": "failure",
            "phase": "accepted",
            "error_code": "INVALID_DOCUMENT",
        }
    )

    result.validate_for(operation)


@pytest.mark.parametrize("field", ["root_volume", "root_volume_key"])
def test_volume_key_requires_nfc_text(field: str) -> None:
    decomposed = unicodedata.normalize("NFD", "root-\u00c5")
    assert decomposed != unicodedata.normalize("NFC", decomposed)

    with pytest.raises(ValidationError):
        if field == "root_volume":
            RemoteModuleOperationV1.model_validate(
                _operation(root_volume={"key": decomposed, "identity": DIGESTS["c"]})
            )
        else:
            RemoteModuleResultV1.model_validate(_result(root_volume_key=decomposed))


@pytest.mark.parametrize(
    "document",
    [
        _result(),
        _result(phase="captured", installed_manifest=None),
        _result(
            phase="restored",
            capture_manifest=None,
            capture_absent=True,
            entry_count=None,
            content_bytes=None,
        ),
        {
            "protocol": "remote-module-result-v1",
            "status": "failure",
            "phase": "accepted",
            "error_code": "INVALID_DOCUMENT",
        },
        _result(
            status="failure",
            phase="accepted",
            error_code="FILESYSTEM_FAILURE",
            installed_manifest=None,
            capture_manifest=None,
            entry_count=None,
            content_bytes=None,
        ),
        _result(status="failure", error_code="RECOVERY_CONFLICT"),
    ],
)
def test_result_round_trips_and_matches_appliance_schema(document: dict[str, object]) -> None:
    document = {key: value for key, value in document.items() if value is not None}
    result = RemoteModuleResultV1.model_validate(document)
    encoded = result.to_canonical_json()

    assert RemoteModuleResultV1.from_canonical_json(encoded) == result
    schema = json.loads((APPLIANCE / "result-v1.schema.json").read_text())
    assert not list(Draft202012Validator(schema).iter_errors(json.loads(encoded)))


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "success", "error_code": "RECOVERY_CONFLICT"},
        {"status": "failure", "error_code": None},
        {"phase": "accepted"},
        {"phase": "captured", "installed_manifest": DIGESTS["f"]},
        {"phase": "staging-intent", "entry_count": 1},
        {"phase": "installed", "capture_manifest": None, "capture_absent": None},
        {"phase": "restored", "entry_count": 1},
        {"capture_manifest": DIGESTS["b"], "capture_absent": True},
        {"entry_count": 200_001},
        {"content_bytes": 8_589_934_593},
    ],
)
def test_result_rejects_inconsistent_phase_evidence(changes: dict[str, object]) -> None:
    document = _result(**changes)
    document = {key: value for key, value in document.items() if value is not None}
    with pytest.raises(ValidationError):
        RemoteModuleResultV1.model_validate(document)


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "captured"},
        {"capture_absent": True},
        {"entry_count": 0},
    ],
)
def test_identity_absent_early_failure_rejects_phase_or_evidence(
    changes: dict[str, object],
) -> None:
    document: dict[str, object] = {
        "protocol": "remote-module-result-v1",
        "status": "failure",
        "phase": "accepted",
        "error_code": "INVALID_DOCUMENT",
        **changes,
    }

    with pytest.raises(ValidationError):
        RemoteModuleResultV1.model_validate(document)


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "accepted"},
        {"phase": "captured", "installed_manifest": DIGESTS["f"]},
        {"phase": "staging-intent", "entry_count": 12},
        {"phase": "installed", "capture_manifest": None, "capture_absent": None},
        {"phase": "restored", "entry_count": 12},
    ],
)
def test_identity_bearing_failure_rejects_phase_incompatible_evidence(
    changes: dict[str, object],
) -> None:
    document = _result(status="failure", error_code="RECOVERY_CONFLICT", **changes)
    document = {key: value for key, value in document.items() if value is not None}

    with pytest.raises(ValidationError):
        RemoteModuleResultV1.model_validate(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("system_id", "00000000-0000-4000-8000-000000000099"),
        ("run_id", "00000000-0000-4000-8000-000000000099"),
        ("plan_identity", DIGESTS["b"]),
        ("operation_nonce", "c" * 32),
    ],
)
def test_result_rejects_cross_attempt_identity_mismatch(field: str, value: str) -> None:
    operation = RemoteModuleOperationV1.model_validate(_operation())
    result = RemoteModuleResultV1.model_validate(_result(**{field: value}))

    with pytest.raises(ValueError, match=field):
        result.validate_for(operation)


@pytest.mark.parametrize(
    ("result_changes", "mismatch"),
    [
        ({"installed_manifest": DIGESTS["a"]}, "installed_manifest"),
        ({"capture_manifest": DIGESTS["c"]}, "capture_manifest"),
    ],
)
def test_restore_result_rejects_operation_manifest_mismatch(
    result_changes: dict[str, object], mismatch: str
) -> None:
    operation = RemoteModuleOperationV1.model_validate(
        _operation(
            "restore",
            capture_manifest=DIGESTS["b"],
            installed_manifest=DIGESTS["f"],
        )
    )
    result = RemoteModuleResultV1.model_validate(
        _result(
            phase="restored",
            entry_count=None,
            content_bytes=None,
            **result_changes,
        )
    )

    with pytest.raises(ValueError, match=mismatch):
        result.validate_for(operation)


@pytest.mark.parametrize(
    ("operation_kind", "result_changes"),
    [
        (
            "capture_install",
            {
                "phase": "restored",
                "entry_count": None,
                "content_bytes": None,
            },
        ),
        ("restore", {"phase": "captured", "installed_manifest": None}),
    ],
)
def test_result_rejects_phase_from_different_operation(
    operation_kind: str, result_changes: dict[str, object]
) -> None:
    operation_changes: dict[str, object] = {}
    if operation_kind == "restore":
        operation_changes = {
            "capture_manifest": DIGESTS["b"],
            "installed_manifest": DIGESTS["f"],
        }
    operation = RemoteModuleOperationV1.model_validate(
        _operation(operation_kind, **operation_changes)
    )
    result = RemoteModuleResultV1.model_validate(_result(**result_changes))

    with pytest.raises(ValueError, match="phase"):
        result.validate_for(operation)


def test_recovery_reference_has_golden_serialization_and_reopens_with_authority() -> None:
    authority = OpaqueProviderRef(ref="mutation/authority-1")
    reference = RemoteModuleRecoveryRefV1(
        system_id=SYSTEM_ID,
        run_id=RUN_ID,
        plan_identity=DIGESTS["a"],
        operation_nonce=NONCE,
        pool=OpaqueProviderRef(ref="pool/system"),
        root_volume=OpaqueProviderRef(ref="volumes/root"),
        source_volume=OpaqueProviderRef(ref="volumes/source"),
        scratch_volume=OpaqueProviderRef(ref="volumes/scratch"),
        operation_identity=DIGESTS["b"],
        result_identity=DIGESTS["c"],
        appliance_image_digest=DIGESTS["e"],
        authority_identity=RemoteModuleRecoveryRefV1.identity_for_authority(authority),
    )
    golden = (
        b'{"appliance_image_digest":"sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
        b'eeeeeeeeeeeeeeee","authority_identity":"sha256:96b36618c1e75fe815b16a5bfd5a6877deb2'
        b'432034e02892d6e21e35cbbcec6c","operation_identity":"sha256:bbbbbbbbbbbbbbbbbbbbb'
        b'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","operation_nonce":"bbbbbbbbbbbbbbbbbb'
        b'bbbbbbbbbbbbbb","plan_identity":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        b'aaaaaaaaaaaaaaaaa","pool":{"ref":"pool/system"},"protocol":"remote-module-recovery-ref-v1"'
        b',"result_identity":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
        b'cccc","root_volume":{"ref":"volumes/root"},"run_id":"00000000-0000-4000-8000-'
        b'000000000002","scratch_volume":{"ref":"volumes/scratch"},"source_volume":{"ref":'
        b'"volumes/source"},"system_id":"00000000-0000-4000-8000-000000000001"}'
    )

    assert reference.to_canonical_json() == golden
    reopened = RemoteModuleRecoveryRefV1.from_canonical_json(golden)
    reopened.validate_authority(OpaqueProviderRef.model_validate(authority.model_dump()))
    assert reopened == reference
    with pytest.raises(ValueError, match="authority"):
        reopened.validate_authority(OpaqueProviderRef(ref="mutation/authority-2"))


def test_recovery_reference_rejects_unknown_version_and_free_form_fields() -> None:
    document = cast(
        dict[str, object],
        json.loads(
            RemoteModuleRecoveryRefV1(
                system_id=SYSTEM_ID,
                run_id=RUN_ID,
                plan_identity=DIGESTS["a"],
                operation_nonce=NONCE,
                pool=OpaqueProviderRef(ref="pool/system"),
                root_volume=OpaqueProviderRef(ref="volumes/root"),
                source_volume=OpaqueProviderRef(ref="volumes/source"),
                scratch_volume=OpaqueProviderRef(ref="volumes/scratch"),
                operation_identity=DIGESTS["b"],
                result_identity=DIGESTS["c"],
                appliance_image_digest=DIGESTS["e"],
                authority_identity=DIGESTS["f"],
            ).to_canonical_json()
        ),
    )
    document["protocol"] = "remote-module-recovery-ref-v2"
    with pytest.raises(ValidationError):
        RemoteModuleRecoveryRefV1.model_validate(document)
    document["protocol"] = "remote-module-recovery-ref-v1"
    document["credentials"] = {"token": "secret"}
    with pytest.raises(ValidationError):
        RemoteModuleRecoveryRefV1.model_validate(document)
