"""Tests for the inline worker-result codec (ADR-0164)."""

from __future__ import annotations

import pytest

from kdive.diagnostics.checks import (
    AUTHORITY_READINESS_ID,
    DEPMOD_TOOLCHAIN_ID,
    GDBSTUB_ACL_ID,
    GUEST_ARCH_ACCEL_ID,
    MULTIARCH_GDB_ID,
    PROVIDER_TLS_ID,
    PSERIES_FADUMP_ID,
    CheckResult,
    CheckStatus,
)
from kdive.diagnostics.contributions.depmod_toolchain import depmod_toolchain_worker_descriptor
from kdive.diagnostics.contributions.guest_arch_accel import guest_arch_accel_worker_descriptor
from kdive.diagnostics.contributions.pseries_fadump import pseries_fadump_worker_descriptor
from kdive.diagnostics.result_codec import (
    _ALLOWED_IDS,
    ResultCodecError,
    deserialize_results,
    serialize_results,
)
from kdive.domain.errors import ErrorCategory


def test_roundtrip_preserves_three_state_and_fields() -> None:
    src = [
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        CheckResult(
            GDBSTUB_ACL_ID,
            CheckStatus.FAIL,
            "blocked",
            fix="open the ACL",
            provider="remote-libvirt",
            failure_category=ErrorCategory.CONFIGURATION_ERROR,
        ),
    ]
    out = deserialize_results(serialize_results(src))
    assert [(r.check_id, r.status, r.fix, r.failure_category) for r in out] == [
        (PROVIDER_TLS_ID, CheckStatus.PASS, None, None),
        (
            GDBSTUB_ACL_ID,
            CheckStatus.FAIL,
            "open the ACL",
            ErrorCategory.CONFIGURATION_ERROR,
        ),
    ]


def test_multiarch_gdb_id_survives_roundtrip() -> None:
    src = [
        CheckResult(
            MULTIARCH_GDB_ID,
            CheckStatus.FAIL,
            "no multiarch gdb",
            fix="install gdb-multiarch",
            provider="local-libvirt",
            failure_category=ErrorCategory.MISSING_DEPENDENCY,
        )
    ]
    [result] = deserialize_results(serialize_results(src))
    assert result.check_id == MULTIARCH_GDB_ID
    assert result.status is CheckStatus.FAIL
    assert result.failure_category is ErrorCategory.MISSING_DEPENDENCY


def test_pseries_fadump_id_survives_roundtrip() -> None:
    src = [
        CheckResult(
            PSERIES_FADUMP_ID,
            CheckStatus.FAIL,
            "fadump not configured",
            fix="enable fadump",
            provider="local-libvirt",
            failure_category=ErrorCategory.CONFIGURATION_ERROR,
        )
    ]
    [result] = deserialize_results(serialize_results(src))
    assert result.check_id == PSERIES_FADUMP_ID
    assert result.status is CheckStatus.FAIL
    assert result.failure_category is ErrorCategory.CONFIGURATION_ERROR


def test_guest_arch_accel_id_survives_roundtrip() -> None:
    src = [
        CheckResult(
            GUEST_ARCH_ACCEL_ID,
            CheckStatus.PASS,
            "accel available",
            provider="local-libvirt",
        )
    ]
    [result] = deserialize_results(serialize_results(src))
    assert result.check_id == GUEST_ARCH_ACCEL_ID
    assert result.status is CheckStatus.PASS


def test_authority_readiness_id_survives_roundtrip() -> None:
    src = [
        CheckResult(
            AUTHORITY_READINESS_ID,
            CheckStatus.ERROR,
            "authority unavailable",
            provider="remote-libvirt",
            failure_category=ErrorCategory.READINESS_FAILURE,
        )
    ]
    [result] = deserialize_results(serialize_results(src))
    assert result.check_id == AUTHORITY_READINESS_ID
    assert result.status is CheckStatus.ERROR
    assert result.failure_category is ErrorCategory.READINESS_FAILURE


def test_depmod_toolchain_id_survives_roundtrip() -> None:
    src = [
        CheckResult(
            DEPMOD_TOOLCHAIN_ID,
            CheckStatus.FAIL,
            "depmod was not found in any of /usr/sbin:/usr/bin:/sbin:/bin",
            fix="install kmod",
            provider="local-libvirt",
            failure_category=ErrorCategory.MISSING_DEPENDENCY,
        )
    ]
    [result] = deserialize_results(serialize_results(src))
    assert result.check_id == DEPMOD_TOOLCHAIN_ID
    assert result.status is CheckStatus.FAIL
    assert result.failure_category is ErrorCategory.MISSING_DEPENDENCY
    assert result.fix == "install kmod"


def test_allowed_ids_matches_registered_worker_vantage_descriptors() -> None:
    """`_ALLOWED_IDS` must track every worker-vantage id the diagnostics contributions register.

    Regression guard for the bug this codec fixed: a registered `WorkerVantageDescriptor` id
    silently missing from `_ALLOWED_IDS` degraded (or, before per-item isolation, poisoned) its
    worker result. Calling the real descriptor-producing functions — rather than restating their
    ids as literals — means renaming or removing a registered id without updating the allowlist
    fails this test.
    """
    expected_ids = {
        PROVIDER_TLS_ID,
        GDBSTUB_ACL_ID,
        AUTHORITY_READINESS_ID,
        MULTIARCH_GDB_ID,
        pseries_fadump_worker_descriptor().id,
        guest_arch_accel_worker_descriptor().id,
        depmod_toolchain_worker_descriptor().id,
    }
    assert expected_ids == _ALLOWED_IDS


def test_roundtrip_preserves_resource_id() -> None:
    src = [
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", resource_id="ub26"),
        CheckResult(GDBSTUB_ACL_ID, CheckStatus.PASS, "ok"),
    ]
    out = deserialize_results(serialize_results(src))
    assert [r.resource_id for r in out] == ["ub26", None]


def test_payload_without_resource_id_reconstructs_none() -> None:
    payload = '{"results": [{"check_id": "provider_tls", "status": "pass", "detail": "ok"}]}'
    [result] = deserialize_results(payload)
    assert result.resource_id is None


def test_roundtrip_preserves_provider_and_detail() -> None:
    src = [
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "tls verified", provider="remote-libvirt"),
        CheckResult(GDBSTUB_ACL_ID, CheckStatus.PASS, "acl open", provider="local-libvirt"),
    ]
    out = deserialize_results(serialize_results(src))
    assert [(r.detail, r.provider) for r in out] == [
        ("tls verified", "remote-libvirt"),
        ("acl open", "local-libvirt"),
    ]


def test_serialize_emits_compact_json() -> None:
    src = [CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt")]
    raw = serialize_results(src)
    assert ", " not in raw
    assert ": " not in raw
    assert '"provider":"remote-libvirt"' in raw


def test_empty_payload_message() -> None:
    with pytest.raises(ResultCodecError) as excinfo:
        deserialize_results("")
    assert str(excinfo.value) == "empty diagnostics result"


def test_invalid_json_message() -> None:
    with pytest.raises(ResultCodecError, match="diagnostics result is not valid JSON"):
        deserialize_results("not json")


def test_missing_results_list_message() -> None:
    with pytest.raises(ResultCodecError) as excinfo:
        deserialize_results("{}")
    assert str(excinfo.value) == "diagnostics result has no 'results' list"


@pytest.mark.parametrize("raw", [None, "", "not json", "{}", '{"results": 3}', "[]"])
def test_malformed_payload_raises(raw: str | None) -> None:
    # The payload itself cannot be parsed at all, so there is no per-item boundary to isolate.
    with pytest.raises(ResultCodecError):
        deserialize_results(raw)


def test_unexpected_check_id_degrades_to_error_result() -> None:
    payload = '{"results": [{"check_id": "secret_ref", "status": "pass", "detail": "x"}]}'
    [result] = deserialize_results(payload)
    assert result.check_id == "secret_ref"
    assert result.status is CheckStatus.ERROR
    assert "unexpected worker-vantage check id" in result.detail
    assert result.failure_category is ErrorCategory.INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize(
    "payload",
    [
        # bad enum value
        '{"results": [{"check_id": "provider_tls", "status": "weird", "detail": "x"}]}',
        # fail without a fix violates CheckResult.__post_init__
        '{"results": [{"check_id": "provider_tls", "status": "fail", "detail": "x"}]}',
    ],
)
def test_invalid_item_degrades_to_error_result(payload: str) -> None:
    [result] = deserialize_results(payload)
    assert result.check_id == PROVIDER_TLS_ID
    assert result.status is CheckStatus.ERROR
    assert "invalid diagnostics result item" in result.detail


def test_non_dict_item_degrades_to_error_result_with_unknown_id() -> None:
    # No dict, so no check_id to attribute the failure to.
    [result] = deserialize_results('{"results": [3]}')
    assert result.check_id == "unknown"
    assert result.status is CheckStatus.ERROR
    assert result.detail == "diagnostics result item is not an object"


def test_one_bad_id_does_not_poison_the_batch() -> None:
    payload = (
        '{"results": ['
        '{"check_id": "provider_tls", "status": "pass", "detail": "ok"},'
        '{"check_id": "secret_ref", "status": "pass", "detail": "x"}'
        "]}"
    )
    good, bad = deserialize_results(payload)
    assert good.check_id == PROVIDER_TLS_ID
    assert good.status is CheckStatus.PASS
    assert good.detail == "ok"
    assert bad.check_id == "secret_ref"
    assert bad.status is CheckStatus.ERROR
