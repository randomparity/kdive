"""Tests for vmcore response rendering helpers."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.tools.lifecycle.vmcore._vmcore_targets import (
    CONSOLE_CRASH,
    EXPECTED_CONSOLE_CRASH,
    NO_BUILD,
    NO_VMCORE,
)
from kdive.mcp.tools.lifecycle.vmcore.view import (
    CONSOLE_CRASH_GUIDANCE,
    console_crash_redirect,
    postmortem_success_response,
)
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry


def test_console_crash_redirect_maps_expected_no_vmcore_to_console_guidance() -> None:
    exc = CategorizedError(
        "run has no vmcore",
        category=ErrorCategory.NOT_FOUND,
        details={"reason": NO_VMCORE, "expected_boot_failure": CONSOLE_CRASH},
    )

    resp = console_crash_redirect("run-id", exc)

    assert resp is not None
    assert resp.status == "error"
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.detail == CONSOLE_CRASH_GUIDANCE
    assert resp.suggested_next_actions == ["runs.get", "artifacts.list"]
    assert resp.data == {
        "reason": EXPECTED_CONSOLE_CRASH,
        "expected_boot_failure": CONSOLE_CRASH,
    }


@pytest.mark.parametrize(
    "details",
    [
        {"reason": NO_BUILD, "expected_boot_failure": CONSOLE_CRASH},
        {"reason": NO_VMCORE},
        {"reason": NO_VMCORE, "expected_boot_failure": "none"},
    ],
)
def test_console_crash_redirect_ignores_non_console_crash_misses(
    details: dict[str, object],
) -> None:
    exc = CategorizedError(
        "run does not resolve to a captured vmcore target",
        category=ErrorCategory.NOT_FOUND,
        details=details,
    )

    assert console_crash_redirect("run-id", exc) is None


def test_postmortem_success_response_redacts_transcript_and_preserves_truncation() -> None:
    registry = SecretRegistry()
    registry.register("root-password", scope=None)

    resp = postmortem_success_response(
        "run-id",
        transcript="panic log root-password",
        truncated=True,
        secret_registry=registry,
    )

    assert resp.status == "succeeded"
    assert resp.suggested_next_actions == ["postmortem.crash", "artifacts.list"]
    assert resp.data["transcript"] == f"panic log {REDACTION}"
    assert resp.data["truncated"] is True
