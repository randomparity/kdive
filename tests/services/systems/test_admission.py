"""System admission service helper tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from kdive.domain.capacity.state import AllocationState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.mcp.tools.lifecycle.systems.provision import _admission_response
from kdive.services.systems import admission
from tests.mcp.systems_support import provisioning_profile

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_ALLOC_ID = UUID("00000000-0000-0000-0000-00000000ad01")
_SYSTEM_ID = UUID("00000000-0000-0000-0000-00000000ad02")


def _allocation(
    *,
    vcpus: int | None = 4,
    memory_gb: int | None = 8,
    disk_gb: int | None = 40,
) -> Allocation:
    return Allocation(
        id=_ALLOC_ID,
        created_at=_DT,
        updated_at=_DT,
        principal="agent",
        agent_session="sess",
        project="proj",
        state=AllocationState.GRANTED,
        requested_vcpus=vcpus,
        requested_memory_gb=memory_gb,
        requested_disk_gb=disk_gb,
        shape="medium",
    )


def _profile(**sizing: int) -> dict[str, object]:
    data = provisioning_profile()
    for key in ("vcpu", "memory_mb", "disk_gb"):
        data.pop(key, None)
    data.update(sizing)
    return data


def test_failure_from_error_keeps_only_json_safe_scalar_details() -> None:
    exc = CategorizedError(
        "bad profile",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "path": "/tmp/rootfs.qcow2",
            "ok": True,
            "count": 3,
            "ratio": 1.5,
            "nan": float("nan"),
            "nested": {"drop": "me"},
        },
    )

    failure = admission._failure_from_error(_SYSTEM_ID, exc)

    assert failure.subject_id == _SYSTEM_ID
    assert failure.reason is admission.AdmissionFailureReason.PROVIDER_POLICY_REJECTED
    assert failure.category is ErrorCategory.CONFIGURATION_ERROR
    assert failure.failure_details == {
        "path": "/tmp/rootfs.qcow2",
        "ok": True,
        "count": 3,
        "ratio": 1.5,
    }


def test_failure_from_error_threads_detail_and_structured_errors() -> None:
    exc = CategorizedError(
        "invalid provisioning profile",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "errors": [{"loc": ("provider", "kind"), "msg": "field required", "type": "missing"}]
        },
    )

    failure = admission._failure_from_error(_SYSTEM_ID, exc)

    assert failure.failure_message == "invalid provisioning profile"
    assert failure.failure_details is not None
    assert failure.failure_details["errors"] == [
        {"loc": ["provider", "kind"], "msg": "field required", "type": "missing"}
    ]


def test_failure_from_error_threads_enumeration_list_to_data() -> None:
    # End-to-end (#731, ADR-0224): the reserved enumeration keys survive safe_error_details
    # through the admission failure_details and into the provision response data, on the path
    # systems.provision actually exercises. Before the safe_error_details reservation the list
    # was dropped here as a non-scalar.
    exc = CategorizedError(
        "unknown rootfs catalog name: no-such",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "provider": "local-libvirt",
            "name": "no-such",
            "available": ["local-libvirt/known"],
        },
    )

    failure = admission._failure_from_error(_SYSTEM_ID, exc)
    response = _admission_response(failure)

    assert failure.failure_details is not None
    assert failure.failure_details["available"] == ["local-libvirt/known"]
    available = response.data["available"]
    assert available == ["local-libvirt/known"]
    # The caller-submitted bad name is never echoed into the enumeration (no-leak, ADR-0123).
    assert isinstance(available, list)
    assert "no-such" not in available


def test_failure_from_error_threads_accepted_values_list_to_data() -> None:
    exc = CategorizedError(
        "local component path is outside provider allowed roots",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"accepted_values": ["/srv/images", "/var/lib/kdive/images"]},
    )

    failure = admission._failure_from_error(_SYSTEM_ID, exc)
    response = _admission_response(failure)

    assert response.data["accepted_values"] == ["/srv/images", "/var/lib/kdive/images"]


def test_failure_from_error_suppresses_detail_for_not_found() -> None:
    exc = CategorizedError(
        "system 11111111-2222-3333-4444-555555555555 was not found",
        category=ErrorCategory.NOT_FOUND,
    )

    failure = admission._failure_from_error(_SYSTEM_ID, exc)
    response = _admission_response(failure)

    assert failure.failure_message == "system 11111111-2222-3333-4444-555555555555 was not found"
    assert response.detail == "not found"


def test_admission_response_maps_failure_fields_into_envelope() -> None:
    failure = admission.AdmissionFailure(
        subject_id=_SYSTEM_ID,
        category=ErrorCategory.CONFIGURATION_ERROR,
        reason=admission.AdmissionFailureReason.SYSTEM_RECYCLE_REQUIRED,
        current_status="failed",
        failure_message="allocation is not provisionable",
        failure_details={"failing_job_id": "job-7"},
        recovery=admission.AdmissionRecovery.RECYCLE_ALLOCATION,
    )

    response = _admission_response(failure)

    assert response.status == "error"
    assert response.object_id == str(_SYSTEM_ID)
    assert response.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert response.detail == "allocation is not provisionable"
    assert response.suggested_next_actions == [
        "allocations.release",
        "allocations.request",
    ]
    assert response.data["failing_job_id"] == "job-7"
    assert response.data["current_status"] == "failed"


def test_admission_response_without_recovery_emits_no_actions() -> None:
    failure = admission.AdmissionFailure(
        subject_id=_SYSTEM_ID,
        category=ErrorCategory.CONFIGURATION_ERROR,
        reason=admission.AdmissionFailureReason.QUOTA_EXCEEDED,
        failure_message="quota exceeded",
        recovery=None,
    )

    response = _admission_response(failure)

    assert response.suggested_next_actions == []


def test_admission_failure_data_omits_current_status_when_absent() -> None:
    failure = admission.AdmissionFailure(
        subject_id=_SYSTEM_ID,
        category=ErrorCategory.CONFIGURATION_ERROR,
        reason=admission.AdmissionFailureReason.QUOTA_EXCEEDED,
        current_status=None,
        failure_details={"limit": 3},
    )

    response = _admission_response(failure)

    assert response.data == {"limit": 3}
    assert "current_status" not in response.data


def test_admission_response_flags_already_defined_recovery_hint() -> None:
    failure = admission.AdmissionFailure(
        subject_id=_SYSTEM_ID,
        category=ErrorCategory.CONFIGURATION_ERROR,
        reason=admission.AdmissionFailureReason.SYSTEM_ALREADY_DEFINED,
        current_status="defined",
        failure_message="system already defined",
        recovery=admission.AdmissionRecovery.PROVISION_DEFINED_SYSTEM,
    )

    response = _admission_response(failure)

    assert response.data["reason"] == "use_systems.provision_defined"
    assert response.data["current_status"] == "defined"
    assert response.suggested_next_actions == ["systems.provision_defined"]


def test_admission_response_other_reason_has_no_reason_hint() -> None:
    failure = admission.AdmissionFailure(
        subject_id=_SYSTEM_ID,
        category=ErrorCategory.CONFIGURATION_ERROR,
        reason=admission.AdmissionFailureReason.QUOTA_EXCEEDED,
        failure_details={"limit": 3},
    )

    response = _admission_response(failure)

    assert "reason" not in response.data


def test_stored_profile_fills_sizing_from_allocation_snapshot() -> None:
    stored = admission._stored_profile_for(_profile(), _allocation())

    assert stored.vcpu == 4
    assert stored.memory_mb == 8192
    assert stored.disk_gb == 40


def test_stored_profile_rejects_conflicting_allocation_sizing_restatement() -> None:
    with pytest.raises(CategorizedError) as exc:
        admission._stored_profile_for(_profile(vcpu=8), _allocation())

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_stored_profile_requires_concrete_sizing_without_allocation_snapshot() -> None:
    with pytest.raises(CategorizedError) as exc:
        admission._stored_profile_for(
            _profile(),
            _allocation(vcpus=None, memory_gb=None, disk_gb=None),
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_stored_profile_partial_snapshot_falls_to_no_reconcile_lane() -> None:
    with pytest.raises(CategorizedError) as exc:
        admission._stored_profile_for(
            _profile(),
            _allocation(vcpus=4, memory_gb=8, disk_gb=None),
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_stored_profile_parses_concrete_profile_in_no_snapshot_lane() -> None:
    stored = admission._stored_profile_for(
        _profile(vcpu=2, memory_mb=4096, disk_gb=20),
        _allocation(vcpus=None, memory_gb=None, disk_gb=None),
    )

    assert stored.vcpu == 2
    assert stored.memory_mb == 4096
    assert stored.disk_gb == 20
