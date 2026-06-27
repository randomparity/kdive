"""Cover the pure decision helpers of the run-admission service.

The async, Postgres-locked create/bind flow stays a Postgres-backed (bucket-1) target; these
are the connectionless helpers that build the failure envelopes and re-validate snapshots.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from kdive.domain.capacity.state import AllocationState, SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.profiles.types import ExpectedBootFailureInput
from kdive.providers.core.resolver import ProviderResolver
from kdive.services.runs.admission import (
    RunCreateError,
    _allocation_block_error,
    _config_failure,
    _parse_expected_boot_failure,
    _parse_uuid,
    _run_create_failure,
    _stale_failure,
    _system_block_error,
    _validate_unbound_target_kind,
)

_A_KIND = next(iter(ResourceKind))


def test_parse_uuid_accepts_valid() -> None:
    value = uuid4()
    assert _parse_uuid(str(value)) == value


def test_parse_uuid_rejects_invalid_as_config_error() -> None:
    with pytest.raises(RunCreateError) as exc:
        _parse_uuid("not-a-uuid")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.object_id == "not-a-uuid"
    assert str(exc.value) == "invalid run creation request"  # the default config detail


def test_config_failure_fields() -> None:
    err = _config_failure("obj-1", detail="bad thing", data={"reason": "x"})
    assert err.object_id == "obj-1"
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(err) == "bad thing"
    assert err.details == {"reason": "x"}


def test_stale_failure_carries_current_status() -> None:
    err = _stale_failure("sys-1", current_status="failed")
    assert err.category is ErrorCategory.STALE_HANDLE
    assert str(err) == "stale run creation target"
    assert err.details == {"current_status": "failed"}


def test_run_create_failure_preserves_message_category_and_details() -> None:
    src = CategorizedError("boom", category=ErrorCategory.BUILD_FAILURE, details={"k": "v"})
    err = _run_create_failure("obj-2", src)
    assert err.object_id == "obj-2"
    assert str(err) == "boom"
    assert err.category is ErrorCategory.BUILD_FAILURE
    assert err.details == {"k": "v"}


def _system(state: SystemState) -> System:
    return cast("System", SimpleNamespace(state=state))


def test_system_block_error_none_system_is_config() -> None:
    sid = uuid4()
    err = _system_block_error(None, sid)
    assert err is not None and err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.object_id == str(sid)
    assert str(err) == "invalid run creation request"  # the default config detail


def test_system_block_error_gone_state_is_stale() -> None:
    sid = uuid4()
    err = _system_block_error(_system(SystemState.FAILED), sid)
    assert err is not None and err.category is ErrorCategory.STALE_HANDLE
    assert err.object_id == str(sid)
    assert err.details == {"current_status": SystemState.FAILED.value}


def test_system_block_error_non_hostable_is_config_with_status() -> None:
    sid = uuid4()
    err = _system_block_error(_system(SystemState.PROVISIONING), sid)
    assert err is not None and err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.object_id == str(sid)
    assert err.details == {"current_status": SystemState.PROVISIONING.value}


def test_system_block_error_ready_passes() -> None:
    assert _system_block_error(_system(SystemState.READY), uuid4()) is None


def _alloc(state: AllocationState, lease_expiry: datetime | None = None) -> Allocation:
    return cast("Allocation", SimpleNamespace(state=state, lease_expiry=lease_expiry))


def test_allocation_block_error_none_is_stale_missing() -> None:
    sid = uuid4()
    err = _allocation_block_error(None, sid)
    assert err is not None and err.object_id == str(sid)
    assert err.details == {"current_status": "missing"}


def test_allocation_block_error_non_active_is_stale() -> None:
    sid = uuid4()
    err = _allocation_block_error(_alloc(AllocationState.RELEASING), sid)
    assert err is not None and err.object_id == str(sid)
    assert err.details == {"current_status": AllocationState.RELEASING.value}


def test_allocation_block_error_expired_lease_is_stale() -> None:
    sid = uuid4()
    past = datetime.now(UTC) - timedelta(minutes=1)
    err = _allocation_block_error(_alloc(AllocationState.ACTIVE, past), sid)
    assert err is not None and err.object_id == str(sid)
    assert err.details == {"current_status": "lease_expired"}


def test_allocation_block_error_active_unexpired_passes() -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    assert _allocation_block_error(_alloc(AllocationState.ACTIVE, future), uuid4()) is None


def test_allocation_block_error_active_no_lease_passes() -> None:
    assert _allocation_block_error(_alloc(AllocationState.ACTIVE, None), uuid4()) is None


class _Resolver:
    def __init__(self, kinds: set[ResourceKind]) -> None:
        self._kinds = kinds

    def registered_kinds(self) -> set[ResourceKind]:
        return self._kinds


def _resolver(kinds: set[ResourceKind]) -> ProviderResolver:
    return cast("ProviderResolver", _Resolver(kinds))


def test_validate_unbound_target_kind_none_is_required() -> None:
    with pytest.raises(RunCreateError) as exc:
        _validate_unbound_target_kind("obj", None, _resolver({_A_KIND}))
    assert exc.value.object_id == "obj"
    assert exc.value.details == {"reason": "target_kind_required"}


def test_validate_unbound_target_kind_unknown_value() -> None:
    with pytest.raises(RunCreateError) as exc:
        _validate_unbound_target_kind("obj", "nonsense-kind", _resolver({_A_KIND}))
    assert exc.value.object_id == "obj"
    assert exc.value.details == {"reason": "unknown_target_kind"}


def test_validate_unbound_target_kind_unregistered_value() -> None:
    # a valid ResourceKind that the resolver does not register is still rejected
    with pytest.raises(RunCreateError) as exc:
        _validate_unbound_target_kind("obj", _A_KIND.value, _resolver(set()))
    assert exc.value.object_id == "obj"
    assert exc.value.details == {"reason": "unknown_target_kind"}


def test_validate_unbound_target_kind_registered_passes() -> None:
    assert _validate_unbound_target_kind("obj", _A_KIND.value, _resolver({_A_KIND})) is _A_KIND


def test_parse_expected_boot_failure_none_returns_none() -> None:
    assert _parse_expected_boot_failure("obj", None) is None


def test_parse_expected_boot_failure_valid_dict_is_serialized() -> None:
    result = _parse_expected_boot_failure("obj", {"kind": "console_crash", "pattern": "panic"})
    assert result == {"kind": "console_crash", "pattern": "panic"}


def test_parse_expected_boot_failure_non_dict_rejected() -> None:
    with pytest.raises(RunCreateError) as exc:
        _parse_expected_boot_failure("obj", cast("ExpectedBootFailureInput", "not-a-dict"))
    assert exc.value.object_id == "obj"
    assert exc.value.details == {"reason": "bad_expected_boot_failure"}


def test_parse_expected_boot_failure_invalid_dict_rejected() -> None:
    with pytest.raises(RunCreateError) as exc:
        _parse_expected_boot_failure("obj", {"kind": "not-a-real-kind"})
    assert exc.value.object_id == "obj"
    assert exc.value.details == {"reason": "bad_expected_boot_failure"}
