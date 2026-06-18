"""ToolResponse envelope tests (ADR-0019) — pure, no DB."""

from __future__ import annotations

import datetime as dt
from typing import cast
from uuid import UUID, uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.jobs import Job, JobKind
from kdive.domain.state import JobState
from kdive.mcp.responses import (
    _RETRYABLE_BY_CATEGORY,
    ResponseData,
    ToolResponse,
    current_status_data,
    reason_data,
)
from kdive.mcp.tools import _common

_NOW = dt.datetime(2026, 6, 3, 12, 0, tzinfo=dt.UTC)
_BUILD_JOB = Job(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    created_at=_NOW,
    updated_at=_NOW,
    kind=JobKind.BUILD,
    payload={},
    state=JobState.RUNNING,
    max_attempts=3,
    result_ref=None,
    error_category=None,
    failure_context={},
    authorizing={"principal": "p", "agent_session": None, "project": "proj"},
    dedup_key="build-job",
)


def test_from_job_running_has_no_refs_and_polling_actions() -> None:
    job = _BUILD_JOB
    resp = ToolResponse.from_job(job)
    assert resp.object_id == str(job.id)
    assert resp.status == "running"
    assert resp.data == {"kind": "build"}
    assert resp.refs == {}
    assert resp.error_category is None
    assert resp.suggested_next_actions == ["jobs.wait", "jobs.cancel"]


def test_from_job_succeeded_exposes_result_ref() -> None:
    job = _BUILD_JOB.model_copy(
        update={"state": JobState.SUCCEEDED, "result_ref": "tenant/run/abc/kernel"}
    )
    resp = ToolResponse.from_job(job)
    assert resp.status == "succeeded"
    assert resp.refs == {"result": "tenant/run/abc/kernel"}
    assert resp.suggested_next_actions == ["jobs.get"]


def test_from_job_failed_carries_category() -> None:
    job = _BUILD_JOB.model_copy(
        update={"state": JobState.FAILED, "error_category": ErrorCategory.BUILD_FAILURE}
    )
    resp = ToolResponse.from_job(job)
    assert resp.status == "failed"
    assert resp.error_category == "build_failure"
    assert resp.suggested_next_actions == ["jobs.get"]


def test_from_job_failed_exposes_failure_context() -> None:
    job = _BUILD_JOB.model_copy(
        update={
            "state": JobState.FAILED,
            "error_category": ErrorCategory.BUILD_FAILURE,
            "failure_context": {
                "failure_message": "make failed",
                "failure_detail_run_id": "r1",
            },
        }
    )
    resp = ToolResponse.from_job(job)
    assert resp.data == {
        "kind": "build",
        "failure_message": "make failed",
        "failure_detail_run_id": "r1",
    }


def test_from_job_canceled_has_no_actions() -> None:
    resp = ToolResponse.from_job(_BUILD_JOB.model_copy(update={"state": JobState.CANCELED}))
    assert resp.status == "canceled"
    assert resp.suggested_next_actions == []


def test_category_without_failure_is_rejected() -> None:
    with pytest.raises(ValueError, match="error_category"):
        ToolResponse(object_id="x", status="running", error_category="build_failure")


def test_failure_without_category_is_rejected() -> None:
    # The validator treats status in {"failed", "error"} as a failure status, which
    # therefore requires a category.
    with pytest.raises(ValueError, match="error_category"):
        ToolResponse(object_id="x", status="error", error_category=None)


def test_success_factory_builds_non_failure_envelope() -> None:
    resp = ToolResponse.success(
        "alloc-1", "granted", suggested_next_actions=["allocations.release"], data={"k": "v"}
    )
    assert resp.object_id == "alloc-1"
    assert resp.status == "granted"
    assert resp.error_category is None
    assert resp.suggested_next_actions == ["allocations.release"]
    assert resp.data == {"k": "v"}


def test_data_accepts_nested_json_values_and_rejects_other_objects() -> None:
    resp = ToolResponse.success(
        "inventory",
        "ok",
        data={"rows": [{"id": "a", "count": 1, "enabled": True, "note": None}]},
    )

    assert resp.data["rows"] == [{"id": "a", "count": 1, "enabled": True, "note": None}]
    with pytest.raises(ValueError, match="non-JSON"):
        ToolResponse.success("bad", "ok", data=cast(ResponseData, {"when": _NOW}))


def test_success_factory_on_failure_status_raises() -> None:
    # "failed" is a failure status; building it via success() (no category) is misuse.
    with pytest.raises(ValueError, match="error_category"):
        ToolResponse.success("alloc-1", "failed")


def test_failure_factory_sets_error_status_and_category() -> None:
    resp = ToolResponse.failure(
        "res-1", ErrorCategory.ALLOCATION_DENIED, data={"reason": "at_capacity"}
    )
    assert resp.status == "error"
    assert resp.error_category == "allocation_denied"
    assert resp.data == {"reason": "at_capacity"}
    assert resp.suggested_next_actions == []


def test_common_detail_helpers_build_named_payloads() -> None:
    assert reason_data("bad_id") == {"reason": "bad_id"}
    assert current_status_data("released") == {"current_status": "released"}


def test_failure_from_error_carries_safe_scalar_details() -> None:
    exc = CategorizedError(
        "bad component",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "field": "rootfs",
            "retryable": False,
            "attempt": 2,
            "ratio": 1.5,
            "nested": {"internal": "do not expose"},
            "items": ["do", "not", "expose"],
        },
    )

    resp = ToolResponse.failure_from_error("profile", exc, data={"reason": "invalid"})

    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    assert resp.data == {
        "field": "rootfs",
        "retryable": False,
        "attempt": 2,
        "ratio": 1.5,
        "reason": "invalid",
    }


def test_failure_from_error_rejects_non_finite_float_detail() -> None:
    exc = CategorizedError(
        "bad component",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"ratio": float("inf")},
    )

    resp = ToolResponse.failure_from_error("profile", exc)

    assert resp.data == {}


# ---------------------------------------------------------------------------
# Task: human-readable detail + structured errors (#450, ADR-0123)
# ---------------------------------------------------------------------------


def test_detail_is_none_on_success() -> None:
    resp = ToolResponse.success("id", "ok", data={"x": 1})
    assert resp.detail is None


def test_failure_carries_detail_kwarg() -> None:
    resp = ToolResponse.failure(
        "res-1", ErrorCategory.CONFIGURATION_ERROR, detail="bad field 'rootfs'"
    )
    assert resp.detail == "bad field 'rootfs'"


def test_failure_from_error_populates_detail_from_message() -> None:
    exc = CategorizedError(
        "invalid provisioning profile", category=ErrorCategory.CONFIGURATION_ERROR
    )
    resp = ToolResponse.failure_from_error("profile", exc)
    assert resp.detail == "invalid provisioning profile"


def test_seam_suppresses_not_found_detail_and_name() -> None:
    exc = CategorizedError(
        "system 11111111-2222-3333-4444-555555555555 was not found",
        category=ErrorCategory.NOT_FOUND,
        details={"object_id": "11111111-2222-3333-4444-555555555555"},
    )
    resp = ToolResponse.failure_from_error("11111111-2222-3333-4444-555555555555", exc)
    assert resp.detail == "not found"
    # The seam must collapse the message; the embedded id must not ride in detail/data.
    assert "was not found" not in resp.model_dump_json()


def test_seam_suppresses_authorization_denied_detail() -> None:
    exc = CategorizedError(
        "project 'tenant-a' is not granted to 'p'",
        category=ErrorCategory.AUTHORIZATION_DENIED,
    )
    resp = ToolResponse.failure_from_error("systems.provision", exc)
    assert resp.detail == "access denied"
    assert "tenant-a" not in resp.model_dump_json()


def test_failure_kwarg_detail_ignored_for_suppressed_category() -> None:
    resp = ToolResponse.failure("obj", ErrorCategory.NOT_FOUND, detail="leak me secret-name")
    assert resp.detail == "not found"
    assert "secret-name" not in resp.model_dump_json()


def test_failure_from_error_preserves_structured_errors() -> None:
    exc = CategorizedError(
        "invalid provisioning profile",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "errors": [
                {
                    "loc": ("provider", "kind"),
                    "msg": "field required",
                    "type": "missing",
                    "input": "SECRET_SUBMITTED_VALUE",
                    "ctx": {"internal": "noise"},
                }
            ]
        },
    )
    resp = ToolResponse.failure_from_error("profile", exc)
    assert resp.data["errors"] == [
        {"loc": ["provider", "kind"], "msg": "field required", "type": "missing"}
    ]
    assert "SECRET_SUBMITTED_VALUE" not in resp.model_dump_json()


def test_errors_list_bounded_to_20() -> None:
    entries = [{"loc": (f"field_{i}",), "msg": "bad", "type": "value_error"} for i in range(25)]
    exc = CategorizedError(
        "many errors", category=ErrorCategory.CONFIGURATION_ERROR, details={"errors": entries}
    )
    resp = ToolResponse.failure_from_error("profile", exc)
    errors = resp.data["errors"]
    assert isinstance(errors, list)
    assert len(errors) == 20


def test_errors_entries_keep_only_reserved_subkeys() -> None:
    exc = CategorizedError(
        "one error",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "errors": [
                {
                    "loc": ("a",),
                    "msg": "bad",
                    "type": "value_error",
                    "url": "https://errors.pydantic.dev/x",
                    "ctx": {"nested": {"deep": 1}},
                }
            ]
        },
    )
    resp = ToolResponse.failure_from_error("profile", exc)
    assert resp.data["errors"] == [{"loc": ["a"], "msg": "bad", "type": "value_error"}]


def test_errors_loc_may_carry_caller_key_name() -> None:
    # An extra-key rejection puts the caller's key name in loc; for the profile surface this is a
    # field path, not secret material — the no-leak invariant is "no submitted value echoes".
    exc = CategorizedError(
        "extra key",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"errors": [{"loc": ("MY_EXTRA_KEY",), "msg": "extra", "type": "extra_forbidden"}]},
    )
    resp = ToolResponse.failure_from_error("profile", exc)
    assert resp.data["errors"] == [
        {"loc": ["MY_EXTRA_KEY"], "msg": "extra", "type": "extra_forbidden"}
    ]


def test_errors_loc_int_segments_preserved_as_ints() -> None:
    exc = CategorizedError(
        "list index error",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"errors": [{"loc": ("items", 3, "name"), "msg": "bad", "type": "missing"}]},
    )
    resp = ToolResponse.failure_from_error("profile", exc)
    assert resp.data["errors"] == [{"loc": ["items", 3, "name"], "msg": "bad", "type": "missing"}]


def test_non_errors_list_keys_still_dropped() -> None:
    exc = CategorizedError(
        "scalar only",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"field": "rootfs", "items": ["a", "b"], "nested": {"x": 1}},
    )
    resp = ToolResponse.failure_from_error("profile", exc)
    assert resp.data == {"field": "rootfs"}


def test_collection_factory_wraps_item_envelopes() -> None:
    first = ToolResponse.success("a", "available", refs={"object": "tenant/a"})
    second = ToolResponse.failure("b", ErrorCategory.INFRASTRUCTURE_FAILURE)

    resp = ToolResponse.collection(
        "artifacts",
        "ok",
        [first, second],
        suggested_next_actions=["artifacts.get"],
        data={"owner": "system-1"},
    )

    assert resp.object_id == "artifacts"
    assert resp.status == "ok"
    assert resp.data["count"] == "2"
    assert resp.data["owner"] == "system-1"
    assert resp.suggested_next_actions == ["artifacts.get"]
    assert resp.items == [first, second]


def test_common_as_uuid_parses_valid_uuid_and_rejects_bad_value() -> None:
    uid = uuid4()

    assert _common.as_uuid(str(uid)) == uid
    assert _common.as_uuid("not-a-uuid") is None


def test_common_failure_helpers_build_expected_error_envelopes() -> None:
    config = _common.config_error("obj-1", data={"reason": "bad_id"})
    stale = _common.stale_handle("obj-2", current_status="released")

    assert config.status == "error"
    assert config.error_category == "configuration_error"
    assert config.data == {"reason": "bad_id"}
    assert stale.status == "error"
    assert stale.error_category == "stale_handle"
    assert stale.data == {"current_status": "released"}


def test_common_not_found_carries_generic_constant_detail() -> None:
    # The by-id miss helper (ADR-0097) inherits the seam constant; the object id never rides detail.
    resp = _common.not_found("11111111-2222-3333-4444-555555555555")
    assert resp.error_category == "not_found"
    assert resp.detail == "not found"


def test_common_job_envelope_preserves_job_fields_and_adds_object_key() -> None:
    object_id = uuid4()
    job = _BUILD_JOB.model_copy(
        update={"state": JobState.SUCCEEDED, "result_ref": "tenant/run/abc/kernel"}
    )

    resp = _common.job_envelope(job, "run_id", object_id)

    assert resp.object_id == str(job.id)
    assert resp.status == "succeeded"
    assert resp.refs == {"result": "tenant/run/abc/kernel"}
    assert resp.suggested_next_actions == ["jobs.get"]
    assert resp.data == {"kind": "build", "run_id": str(object_id)}


# ---------------------------------------------------------------------------
# Task 3: derived retryable field (ADR-0118)
# ---------------------------------------------------------------------------


def test_retryable_table_is_exhaustive_over_error_category() -> None:
    # Every category is classified; none stale. A new ErrorCategory must be a deliberate edit.
    assert set(_RETRYABLE_BY_CATEGORY) == set(ErrorCategory)


def test_retryable_is_none_on_success() -> None:
    resp = ToolResponse.success("id", "ok", data={"x": 1})
    assert resp.retryable is None


def test_retryable_derived_on_failure() -> None:
    transient = ToolResponse.failure("id", ErrorCategory.QUEUE_TIMEOUT)
    terminal = ToolResponse.failure("id", ErrorCategory.ALLOCATION_DENIED)
    assert transient.retryable is True
    assert terminal.retryable is False


def test_retryable_is_never_caller_set() -> None:
    # A caller-supplied value is overwritten by the derived one.
    forced = ToolResponse(
        object_id="id",
        status="error",
        error_category=ErrorCategory.CONFIGURATION_ERROR.value,
        retryable=True,
    )
    assert forced.retryable is False  # configuration_error is terminal


def test_every_category_has_an_explicit_expected_bool() -> None:
    # Pin each category's classification so a reclassification is a visible diff.
    expected = {
        ErrorCategory.INFRASTRUCTURE_FAILURE: True,
        ErrorCategory.PROVISIONING_FAILURE: True,
        ErrorCategory.BOOT_TIMEOUT: True,
        ErrorCategory.READINESS_FAILURE: True,
        ErrorCategory.TRANSPORT_FAILURE: True,
        ErrorCategory.TRANSPORT_CONFLICT: True,
        ErrorCategory.DEBUG_ATTACH_FAILURE: True,
        ErrorCategory.CONTROL_FAILURE: True,
        ErrorCategory.CAPACITY_EXHAUSTED: True,
        ErrorCategory.QUEUE_TIMEOUT: True,
        ErrorCategory.CONFIGURATION_ERROR: False,
        ErrorCategory.MISSING_DEPENDENCY: False,
        ErrorCategory.BUILD_FAILURE: False,
        ErrorCategory.INSTALL_FAILURE: False,
        ErrorCategory.STALE_HANDLE: False,
        ErrorCategory.LEASE_EXPIRED: False,
        ErrorCategory.NOT_IMPLEMENTED: False,
        ErrorCategory.NOT_FOUND: False,
        ErrorCategory.CONFLICT: False,
        ErrorCategory.AUTHORIZATION_DENIED: False,
        ErrorCategory.QUOTA_EXCEEDED: False,
        ErrorCategory.ALLOCATION_DENIED: False,
    }
    assert expected == _RETRYABLE_BY_CATEGORY
