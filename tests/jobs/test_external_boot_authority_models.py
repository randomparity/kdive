"""Typed worker-boundary contracts for external boot authority results."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from kdive.jobs.models import (
    ExternalBootAuthorityFailureV1,
    ExternalBootAuthoritySuccessV1,
)

_DIGEST = "sha256:" + "a" * 64


def _carrier(result: dict[str, object]) -> dict[str, object]:
    return {
        "authority_id": uuid4(),
        "generation": 1,
        "activation_id": uuid4(),
        "run_id": uuid4(),
        "system_id": uuid4(),
        "plan_identity": _DIGEST,
        "purpose": "activate",
        "provider_kind": "local-libvirt",
        "authority_instance": "provider-1",
        "operation_identity": "activate-1",
        "operation_digest": _DIGEST,
        "journal_sequence": 1,
        "journal_digest": _DIGEST,
        "result": result,
    }


def test_success_carrier_rejects_failure_operation() -> None:
    with pytest.raises(ValidationError, match="success carrier"):
        ExternalBootAuthoritySuccessV1.model_validate(
            _carrier(
                {
                    "schema": "external-boot-authority-result-v1",
                    "operation": "fail",
                    "error_category": "boot_timeout",
                    "failure_context": {},
                    "terminal": True,
                }
            )
        )


def test_failure_carrier_rejects_success_operation() -> None:
    with pytest.raises(ValidationError, match="failure carrier"):
        ExternalBootAuthorityFailureV1.model_validate(
            _carrier(
                {
                    "schema": "external-boot-authority-result-v1",
                    "operation": "activate",
                    "result_ref": None,
                    "evidence": {},
                }
            )
        )


def test_carrier_rejects_malformed_binding_and_result_schema() -> None:
    malformed = _carrier({"schema": "wrong", "operation": "activate"})
    malformed["operation_digest"] = "not-a-digest"
    with pytest.raises(ValidationError) as raised:
        ExternalBootAuthoritySuccessV1.model_validate(malformed)
    locations = {error["loc"] for error in raised.value.errors()}
    assert ("operation_digest",) in locations
    assert ("result",) in locations
