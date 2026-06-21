"""Unit tests for the shared MCP tool-boundary helpers (`kdive.mcp.tools._common`)."""

from __future__ import annotations

from kdive.mcp.tools._common import (
    ConfigErrorReason,
    config_error,
    config_error_reason,
    not_found,
)


def test_not_found_builds_a_not_found_failure_envelope() -> None:
    resp = not_found("abc")
    assert resp.status == "error"
    assert resp.error_category == "not_found"
    assert resp.object_id == "abc"
    assert resp.data == {}


def test_not_found_carries_optional_data() -> None:
    resp = not_found("abc", data={"hint": "gone"})
    assert resp.error_category == "not_found"
    assert resp.data == {"hint": "gone"}


def test_config_error_stays_configuration_error() -> None:
    # The two helpers must remain distinct: a malformed id stays configuration_error.
    resp = config_error("nope")
    assert resp.error_category == "configuration_error"


def test_config_error_surfaces_supplied_detail() -> None:
    # configuration_error is not a suppressed category, so a supplied detail must reach the wire
    # unchanged — dropping or nulling it would hide the parse-failure reason from the caller.
    resp = config_error("nope", detail="id 'nope' is not a valid UUID")
    assert resp.error_category == "configuration_error"
    assert resp.detail == "id 'nope' is not a valid UUID"


def test_config_error_detail_defaults_to_none() -> None:
    resp = config_error("nope")
    assert resp.detail is None


def test_config_error_reason_surfaces_supplied_detail() -> None:
    # The reason lands in data.reason; a supplied human one-liner must still reach data.detail.
    resp = config_error_reason(
        "nope",
        ConfigErrorReason.INVALID_CURSOR,
        detail="cursor 'nope' is not usable",
    )
    assert resp.error_category == "configuration_error"
    assert resp.data["reason"] == "invalid_cursor"
    assert resp.detail == "cursor 'nope' is not usable"


def test_config_error_reason_detail_defaults_to_none() -> None:
    resp = config_error_reason("nope", ConfigErrorReason.INVALID_UUID)
    assert resp.detail is None
    assert resp.data["reason"] == "invalid_uuid"
