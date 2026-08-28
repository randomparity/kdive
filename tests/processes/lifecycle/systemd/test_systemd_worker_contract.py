"""Bounded wire models for the systemd worker lifecycle witness."""

from __future__ import annotations

import json
from typing import cast

import pytest
from pydantic import ValidationError

from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
    LIFECYCLE_PROTOCOL_VERSION,
    MAX_REQUEST_BYTES,
    LifecycleRequest,
    LifecycleResponse,
    ResponseCode,
    SlotPhase,
    SlotResult,
    _protocol_identity,
    client_exit_status,
    lifecycle_protocol_identity,
)
from tests.processes.lifecycle.systemd.systemd_worker_support import start_payload


def test_lifecycle_protocol_identity_is_deterministic_and_versioned() -> None:
    identity = lifecycle_protocol_identity()

    assert identity == lifecycle_protocol_identity()
    assert identity.startswith(f"{LIFECYCLE_PROTOCOL_VERSION}:")
    assert len(identity.removeprefix(f"{LIFECYCLE_PROTOCOL_VERSION}:")) == 64


def test_lifecycle_protocol_identity_changes_for_semantics_or_schema() -> None:
    request_schema = LifecycleRequest.model_json_schema()
    response_schema = LifecycleResponse.model_json_schema()

    baseline = _protocol_identity(1, request_schema, response_schema)

    assert _protocol_identity(2, request_schema, response_schema) != baseline
    assert _protocol_identity(1, {**request_schema, "changed": True}, response_schema) != baseline


def test_non_start_request_rejects_settings() -> None:
    with pytest.raises(ValidationError):
        LifecycleRequest.model_validate(
            {"operation": "status", "worker_count": 1, "settings": {"python": "/x"}}
        )


def test_start_rejects_ninth_slot_and_long_string() -> None:
    with pytest.raises(ValidationError):
        LifecycleRequest.model_validate(start_payload(worker_count=9))
    with pytest.raises(ValidationError):
        LifecycleRequest.model_validate(start_payload(python="/" + "x" * 4096))


def test_start_string_and_secret_allow_exact_utf8_4_kib_boundary() -> None:
    exact_path = "/" + "€" * 1365
    exact_secret = "€" * 1365 + "x"

    request = LifecycleRequest.model_validate(
        start_payload(source_root=exact_path, worker_database_url=exact_secret)
    )

    assert request.settings is not None
    assert request.settings.source_root == exact_path
    assert request.settings.worker_database_url.get_secret_value() == exact_secret


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_root", "/" + "€" * 1366),
        ("worker_database_url", "€" * 1366 + "x"),
    ],
)
def test_start_rejects_multibyte_string_over_utf8_4_kib(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="4096 UTF-8 bytes"):
        LifecycleRequest.model_validate(start_payload(**{field: value}))


def test_request_rejects_whitespace_frame_above_32_kib() -> None:
    payload = b'{"operation":"status"}'
    oversized = payload + b" " * (MAX_REQUEST_BYTES - len(payload) + 1)

    with pytest.raises(ValueError, match="32 KiB"):
        LifecycleRequest.model_validate_json(oversized)


def test_request_accepts_exact_32_kib_frame() -> None:
    payload = b'{"operation":"status"}'
    exact = payload + b" " * (MAX_REQUEST_BYTES - len(payload))

    assert LifecycleRequest.model_validate_json(exact).operation == "status"


@pytest.mark.parametrize("lanes", [["default", "default"], ["default", "state-fenced", "default"]])
def test_start_rejects_duplicate_or_excess_accepted_lanes(lanes: list[str]) -> None:
    with pytest.raises(ValidationError):
        LifecycleRequest.model_validate(start_payload(accepted_lanes=lanes))


def test_start_requires_the_complete_allowlisted_settings() -> None:
    payload = start_payload()
    settings = cast(dict[str, object], payload["settings"])
    settings["unexpected"] = "value"

    with pytest.raises(ValidationError):
        LifecycleRequest.model_validate(payload)


def test_start_rejects_relative_worker_paths() -> None:
    payload = start_payload()
    settings = cast(dict[str, object], payload["settings"])
    settings["source_root"] = "relative/source"

    with pytest.raises(ValidationError):
        LifecycleRequest.model_validate(payload)


def test_secret_settings_are_not_rendered_by_model_dump() -> None:
    request = LifecycleRequest.model_validate(start_payload())

    rendered = request.model_dump_json()

    assert "postgresql://worker:password@db/kdive" not in rendered  # pragma: allowlist secret
    assert "access-key" not in rendered
    assert "secret-key" not in rendered


def test_unresolved_evidence_response_is_never_success() -> None:
    response = LifecycleResponse(
        ok=False,
        code="evidence_rejected",
        message="termination evidence was not accepted",
        retry_action="retry_same_operation",
        slots=(SlotResult(slot=1, unit="kdive-live-worker@1.service", phase="started"),),
    )
    assert response.ok is False
    assert client_exit_status(response) == 4


@pytest.mark.parametrize(
    ("code", "ok", "expected"),
    [
        ("ok", True, 0),
        ("invalid_request", False, 2),
        ("busy", False, 3),
        ("deadline_exceeded", False, 4),
        ("conflict", False, 4),
        ("dependency_unavailable", False, 4),
        ("evidence_rejected", False, 4),
        ("diagnostics_withheld", False, 4),
        ("internal_error", False, 4),
    ],
)
def test_client_exit_status_maps_each_response_code(
    code: ResponseCode, ok: bool, expected: int
) -> None:
    response = LifecycleResponse(
        ok=ok,
        code=code,
        message="bounded response",
        retry_action="none",
    )

    assert client_exit_status(response) == expected


@pytest.mark.parametrize(
    ("code", "ok"),
    [("ok", False), ("busy", True), ("evidence_rejected", True)],
)
def test_client_exit_status_rejects_contradictory_response(code: ResponseCode, ok: bool) -> None:
    response = LifecycleResponse(
        ok=ok,
        code=code,
        message="bounded response",
        retry_action="none",
    )

    assert client_exit_status(response) == 5


def test_response_round_trips_each_code_with_bounded_slots() -> None:
    response = LifecycleResponse(
        ok=False,
        code="diagnostics_withheld",
        message="diagnostics were withheld",
        retry_action="operator_recovery",
        slots=(SlotResult(slot=1, unit="kdive-live-worker@1.service", phase=SlotPhase.STARTED),),
        diagnostics="redacted diagnostic text",
    )

    restored = LifecycleResponse.model_validate_json(response.model_dump_json())

    assert restored == response
    assert len(json.dumps(response.model_dump(mode="json")).encode()) < 1_114_112


def test_response_rejects_multibyte_diagnostics_above_utf8_byte_ceiling() -> None:
    with pytest.raises(ValidationError, match="1048576 UTF-8 bytes"):
        LifecycleResponse(
            ok=False,
            code="diagnostics_withheld",
            message="diagnostics were withheld",
            retry_action="operator_recovery",
            diagnostics="😀" * 300_000,
        )


def test_result_and_response_strings_use_utf8_byte_limits() -> None:
    exact_result = SlotResult(
        slot=1,
        unit="kdive-live-worker@1.service",
        message="é" * 512,
    )
    exact_response = LifecycleResponse(
        ok=False,
        code="conflict",
        message="é" * 2048,
        retry_action="retry_same_operation",
        slots=(exact_result,),
    )

    assert exact_response.slots == (exact_result,)
    with pytest.raises(ValidationError, match="1024 UTF-8 bytes"):
        SlotResult(slot=1, unit="kdive-live-worker@1.service", message="é" * 513)
    with pytest.raises(ValidationError, match="4096 UTF-8 bytes"):
        LifecycleResponse(
            ok=False,
            code="conflict",
            message="é" * 2049,
            retry_action="retry_same_operation",
        )
