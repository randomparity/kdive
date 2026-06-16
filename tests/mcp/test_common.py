"""Tests for the shared MCP tool-boundary helpers (`mcp/tools/_common.py`)."""

from __future__ import annotations

from kdive.domain.errors import ErrorCategory
from kdive.mcp.tools._common import authz_denied


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
