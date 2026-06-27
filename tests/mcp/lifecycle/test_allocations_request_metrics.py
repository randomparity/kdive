"""Server-side admission decision translation for the request handler (ADR-0190 D)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from kdive.domain.capacity.state import AllocationState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.mcp.tools.lifecycle.allocations.request import _outcome_for_metrics
from kdive.services.allocation.admission.core import AdmissionOutcome
from kdive.services.allocation.admission.metrics import (
    AdmissionDecision,
    _AdmissionReason,
    classify,
)
from kdive.services.allocation.admission.request import RequestAdmissionResult


def _alloc(state: AllocationState) -> Allocation:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Allocation(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        principal="alice",
        agent_session=None,
        project="proj",
        resource_id=None,
        state=state,
        lease_expiry=None,
        requested_vcpus=1,
        requested_memory_gb=1,
        requested_disk_gb=1,
        shape="vm",
        pcie_claim=[],
    )


def test_grant_result_classifies_as_granted() -> None:
    result = RequestAdmissionResult("obj", "proj", allocation=_alloc(AllocationState.GRANTED))
    outcome = _outcome_for_metrics(result)
    assert outcome is not None
    assert classify(outcome) == (AdmissionDecision.GRANTED, _AdmissionReason.NONE)


def test_enqueue_result_classifies_as_queued() -> None:
    result = RequestAdmissionResult("obj", "proj", allocation=_alloc(AllocationState.REQUESTED))
    outcome = _outcome_for_metrics(result)
    assert outcome is not None
    assert classify(outcome) == (AdmissionDecision.QUEUED, _AdmissionReason.NONE)


def test_denial_result_passes_through_the_outcome() -> None:
    denial = AdmissionOutcome(granted=False, allocation=None, category=ErrorCategory.QUOTA_EXCEEDED)
    result = RequestAdmissionResult("obj", "proj", denial=denial)
    assert _outcome_for_metrics(result) is denial


def test_pre_admission_error_classifies_by_category() -> None:
    result = RequestAdmissionResult(
        "obj",
        "proj",
        error=CategorizedError("bad", category=ErrorCategory.QUOTA_EXCEEDED),
    )
    outcome = _outcome_for_metrics(result)
    assert outcome is not None
    # The error outcome is recorded as a non-grant under the error's own category.
    assert outcome.granted is False
    assert outcome.category is ErrorCategory.QUOTA_EXCEEDED
    assert classify(outcome)[0] is AdmissionDecision.REJECTED


def test_no_schedulable_resource_records_the_supplied_category() -> None:
    result = RequestAdmissionResult("obj", "proj", category=ErrorCategory.QUOTA_EXCEEDED)
    outcome = _outcome_for_metrics(result)
    assert outcome is not None
    assert outcome.granted is False
    # The result's own category is used when present (not defaulted away).
    assert outcome.category is ErrorCategory.QUOTA_EXCEEDED
    assert classify(outcome)[0] is AdmissionDecision.REJECTED


def test_no_schedulable_resource_without_category_defaults_to_configuration() -> None:
    result = RequestAdmissionResult("obj", "proj", category=None)
    outcome = _outcome_for_metrics(result)
    assert outcome is not None
    assert outcome.granted is False
    # An absent category falls back to CONFIGURATION_ERROR.
    assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
    assert classify(outcome)[0] is AdmissionDecision.REJECTED
