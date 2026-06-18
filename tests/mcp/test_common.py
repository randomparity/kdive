"""Tests for the shared MCP tool-boundary helpers (`mcp/tools/_common.py`)."""

from __future__ import annotations

from kdive.domain.errors import ErrorCategory
from kdive.mcp.tools._common import ConfigErrorReason, authz_denied, config_error_reason


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
