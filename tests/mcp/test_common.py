"""Tests for the shared MCP tool-boundary helpers (`mcp/tools/_common.py`)."""

from __future__ import annotations

from kdive.domain.errors import ErrorCategory
from kdive.mcp.tools._common import (
    ConfigErrorReason,
    authz_denied,
    config_error_reason,
    invalid_uuid_error,
)


def test_config_error_reason_carries_invalid_version() -> None:
    resp = config_error_reason(
        "vanilla", ConfigErrorReason.INVALID_VERSION, detail="not a recognized kernel version"
    )
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.data["reason"] == "invalid_version"


def test_config_error_reason_surfaces_reason_and_detail() -> None:
    resp = config_error_reason("not-a-uuid", ConfigErrorReason.INVALID_UUID, detail="bad id")
    assert resp.status == "error"
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.object_id == "not-a-uuid"
    assert resp.data["reason"] == "invalid_uuid"
    assert resp.detail == "bad id"
    # configuration_error is not a suppressed category, so detail passes through (ADR-0123).
    assert "accepted_values" not in resp.data


def test_config_error_reason_includes_sorted_accepted_values() -> None:
    resp = config_error_reason(
        "bogus",
        ConfigErrorReason.INVALID_STATE,
        accepted_values=["ready", "defined", "active"],
        detail="unknown state",
    )
    assert resp.data["reason"] == "invalid_state"
    # accepted_values is sorted for a stable wire order.
    assert resp.data["accepted_values"] == ["active", "defined", "ready"]


def test_config_error_reason_defaults_detail_to_none_keeps_reason() -> None:
    resp = config_error_reason("x", ConfigErrorReason.MISSING_REQUIRED_FIELD)
    # AC#1: at least one of detail or data.reason is present.
    assert resp.data["reason"] == "missing_required_field"


def test_invalid_uuid_error_names_field_and_reason() -> None:
    resp = invalid_uuid_error("run_id", "not-a-uuid")
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.object_id == "not-a-uuid"
    assert resp.data["reason"] == "invalid_uuid"
    assert resp.detail is not None
    assert "run_id" in resp.detail and "not-a-uuid" in resp.detail


def test_invalid_uuid_error_bounds_the_echoed_id() -> None:
    # An oversized malformed id must not be reflected whole into detail (ADR-0166/0174 echo
    # rule); the full value still rides as object_id, but detail stays bounded.
    huge = "z" * 5000
    resp = invalid_uuid_error("run_id", huge)
    assert resp.object_id == huge
    assert resp.detail is not None
    assert len(resp.detail) < 200
    assert "…" in resp.detail


def test_authz_denied_surfaces_missing_checks() -> None:
    resp = authz_denied("sys-123", ["admin_role"])
    assert resp.status == "error"
    assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
    assert resp.object_id == "sys-123"
    assert resp.data["missing_checks"] == ["admin_role"]
    # No-leak seam (ADR-0123): detail stays the suppressed constant, never a resource name.
    assert resp.detail == "access denied"


def test_authz_denied_preserves_multiple_check_order() -> None:
    resp = authz_denied("sys-9", ["operator_role", "profile_opt_in"])
    assert resp.data["missing_checks"] == ["operator_role", "profile_opt_in"]
