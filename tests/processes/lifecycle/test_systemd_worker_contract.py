"""Bounded wire models for the systemd worker lifecycle witness."""

from __future__ import annotations

import json
from typing import cast

import pytest
from pydantic import ValidationError

from kdive.processes.lifecycle.systemd_worker_contract import (
    MAX_REQUEST_BYTES,
    LifecycleRequest,
    LifecycleResponse,
    ResponseCode,
    SlotPhase,
    SlotResult,
    client_exit_status,
)


def start_payload(**overrides: object) -> dict[str, object]:
    """Return the complete, explicitly allowed worker-runtime request."""
    settings: dict[str, object] = {
        "python": "/usr/bin/python3",
        "source_root": "/srv/kdive",
        "rootfs_dir": "/var/lib/kdive/rootfs",
        "build_workspace": "/var/lib/kdive/build",
        "build_component_roots": "/srv/kdive/fixtures",
        "install_staging": "/var/lib/kdive/install",
        "fixture_catalog_path": "/srv/kdive/fixtures/catalog.yaml",
        "worker_database_url": "postgresql://worker:password@db/kdive",  # pragma: allowlist secret
        "libvirt_uri": "qemu+unix:///session?socket=/run/libvirt/virtqemud-sock",
        "s3_endpoint_url": "http://minio:9000",
        "s3_bucket": "kdive-artifacts",
        "s3_region": "us-east-1",
        "aws_access_key_id": "access-key",
        "aws_secret_access_key": "secret-key",  # pragma: allowlist secret
        "accepted_lanes": ["default", "state-fenced"],
        "build_user": "builder",
        "log_level": "INFO",
        "health_binds": {"1": "127.0.0.1:9465", "2": "127.0.0.1:9470"},
    }
    payload: dict[str, object] = {
        "operation": "start",
        "worker_count": 2,
        "settings": settings,
    }
    for name in tuple(overrides):
        if name in settings:
            settings[name] = overrides.pop(name)
    payload.update(overrides)
    return payload


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


def test_response_serialization_rejects_multibyte_frame_above_byte_ceiling() -> None:
    response = LifecycleResponse(
        ok=False,
        code="diagnostics_withheld",
        message="diagnostics were withheld",
        retry_action="operator_recovery",
        diagnostics="😀" * 300_000,
    )

    with pytest.raises(ValueError, match="1,114,112"):
        response.to_json_bytes()
