"""Tests for the inline worker-result codec (ADR-0164)."""

from __future__ import annotations

import pytest

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    CheckResult,
    CheckStatus,
)
from kdive.diagnostics.result_codec import (
    ResultCodecError,
    deserialize_results,
    serialize_results,
)


def test_roundtrip_preserves_three_state_and_fields() -> None:
    src = [
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        CheckResult(
            GDBSTUB_ACL_ID,
            CheckStatus.FAIL,
            "blocked",
            fix="open the ACL",
            provider="remote-libvirt",
            failure_category="configuration_error",
        ),
    ]
    out = deserialize_results(serialize_results(src))
    assert [(r.check_id, r.status, r.fix, r.failure_category) for r in out] == [
        (PROVIDER_TLS_ID, CheckStatus.PASS, None, None),
        (GDBSTUB_ACL_ID, CheckStatus.FAIL, "open the ACL", "configuration_error"),
    ]


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


def test_unexpected_check_id_message() -> None:
    payload = '{"results": [{"check_id": "secret_ref", "status": "pass", "detail": "x"}]}'
    with pytest.raises(ResultCodecError, match="unexpected worker-vantage check id"):
        deserialize_results(payload)


def test_invalid_item_message() -> None:
    payload = '{"results": [{"check_id": "provider_tls", "status": "weird", "detail": "x"}]}'
    with pytest.raises(ResultCodecError, match="invalid diagnostics result item"):
        deserialize_results(payload)


def test_non_dict_item_message() -> None:
    with pytest.raises(ResultCodecError) as excinfo:
        deserialize_results('{"results": [3]}')
    assert str(excinfo.value) == "diagnostics result item is not an object"


@pytest.mark.parametrize("raw", [None, "", "not json", "{}", '{"results": 3}', "[]"])
def test_malformed_raises(raw: str | None) -> None:
    with pytest.raises(ResultCodecError):
        deserialize_results(raw)


def test_unexpected_check_id_raises() -> None:
    payload = '{"results": [{"check_id": "secret_ref", "status": "pass", "detail": "x"}]}'
    with pytest.raises(ResultCodecError):
        deserialize_results(payload)


def test_invariant_violation_raises() -> None:
    # fail without a fix violates CheckResult.__post_init__
    payload = '{"results": [{"check_id": "provider_tls", "status": "fail", "detail": "x"}]}'
    with pytest.raises(ResultCodecError):
        deserialize_results(payload)


def test_bad_enum_value_raises() -> None:
    payload = '{"results": [{"check_id": "provider_tls", "status": "weird", "detail": "x"}]}'
    with pytest.raises(ResultCodecError):
        deserialize_results(payload)
