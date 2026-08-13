"""Wire-contract tests for supervised capture children (ADR-0558)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from kdive.domain.errors import ErrorCategory
from kdive.jobs.capture_operations.protocol import CaptureRequest, CaptureResult


def _request() -> CaptureRequest:
    return CaptureRequest(
        job_id=uuid4(),
        provider_kind="local-libvirt",
        resource_id=uuid4(),
        system_id=uuid4(),
        domain_name="kdive-test-domain",
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=2,
    )


def test_capture_request_canonical_json_is_stable_and_round_trips() -> None:
    request = _request()
    encoded = request.to_canonical_json()

    assert encoded.endswith(b"\n")
    assert CaptureRequest.from_canonical_json(encoded) == request
    assert request.digest == CaptureRequest.from_canonical_json(encoded).digest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_kind", "fault-inject"),
        ("domain_name", ""),
        ("domain_name", "x" * 256),
        ("snaplen", 0),
        ("snaplen", 262_145),
        ("max_bytes", 1_048_575),
        ("max_bytes", 536_870_913),
        ("max_polls", 0),
        ("max_polls", 601),
    ],
)
def test_capture_request_rejects_out_of_contract_values(field: str, value: object) -> None:
    payload = _request().model_dump(mode="json")
    payload[field] = value

    with pytest.raises(ValidationError):
        CaptureRequest.model_validate(payload)


def test_capture_request_rejects_extra_and_malformed_json() -> None:
    payload = _request().model_dump(mode="json") | {"database_url": "postgresql://secret"}
    with pytest.raises(ValidationError):
        CaptureRequest.model_validate(payload)
    with pytest.raises(ValueError, match="canonical JSON"):
        CaptureRequest.from_canonical_json(b"{not-json")


def test_capture_result_is_bounded_and_strict() -> None:
    result = CaptureResult(
        outcome="failure",
        size_bytes=0,
        truncated=False,
        error_category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        terminal=False,
        reason="provider_execution_not_installed",
        details={"phase": "bootstrap"},
    )
    assert CaptureResult.from_canonical_json(result.to_canonical_json()) == result

    with pytest.raises(ValidationError):
        CaptureResult.model_validate(result.model_dump() | {"unknown": True})
    with pytest.raises(ValidationError):
        CaptureResult.model_validate(result.model_dump() | {"reason": "x" * 257})
    with pytest.raises(ValueError, match="result JSON"):
        CaptureResult.from_canonical_json(b"[]")
