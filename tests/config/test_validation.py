"""Per-process startup validation with the conditional ``required_when`` contract."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from kdive.config import Registry, Setting
from kdive.config.core_settings import (
    DATABASE_URL,
    OIDC_AUDIENCE,
    OIDC_ISSUER,
    OIDC_JWKS_URI,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_REGION,
    SETTINGS,
    _always,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


def _str(raw: str) -> str:
    return raw


def _uri_set(env: Mapping[str, str]) -> bool:
    return bool(env.get("KDIVE_TEST_PROVIDER_URI"))


URI = Setting(
    name="KDIVE_TEST_PROVIDER_URI",
    parse=_str,
    group="test-provider",
    processes=frozenset({"worker", "reconciler"}),
)
CA = Setting(
    name="KDIVE_TEST_PROVIDER_CA_CERT_REF",
    parse=_str,
    secret=True,
    group="test-provider",
    processes=frozenset({"worker", "reconciler"}),
    required_when=_uri_set,
    suggest="set the CA cert secret ref",
)


def test_required_when_false_does_not_require_optional_provider_setting() -> None:
    reg = Registry([URI, CA])
    reg.load({})  # remote-libvirt not enabled
    reg.validate("worker")  # must not raise


def test_required_when_true_requires_the_setting() -> None:
    reg = Registry([URI, CA])
    reg.load({"KDIVE_TEST_PROVIDER_URI": "test://host/system"})
    with pytest.raises(CategorizedError) as ei:
        reg.validate("worker")
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "KDIVE_TEST_PROVIDER_CA_CERT_REF" in str(ei.value)
    assert ei.value.details["missing"] == ["KDIVE_TEST_PROVIDER_CA_CERT_REF"]


def test_validate_only_checks_settings_for_the_role() -> None:
    reg = Registry([URI, CA])
    reg.load({"KDIVE_TEST_PROVIDER_URI": "test://host/system"})
    reg.validate("server")  # server does not consume these → no raise


def test_validate_surfaces_a_malformed_value_for_the_role() -> None:
    def _int(raw: str) -> int:
        return int(raw)

    port = Setting(
        name="KDIVE_HTTP_PORT", parse=_int, group="http", processes=frozenset({"server"})
    )
    reg = Registry([port])
    reg.load({"KDIVE_HTTP_PORT": "not-a-number"})
    with pytest.raises(CategorizedError) as ei:
        reg.validate("server")
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details["variable"] == "KDIVE_HTTP_PORT"


_S3_SETTINGS = [S3_ENDPOINT_URL, S3_BUCKET, S3_REGION]


@pytest.mark.parametrize("process", ["server", "worker", "reconciler"])
def test_s3_settings_required_when_absent(process: str) -> None:
    reg = Registry(_S3_SETTINGS)
    reg.load({})  # no KDIVE_S3_* configured (region has a default)
    with pytest.raises(CategorizedError) as ei:
        reg.validate(process)
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details["missing"] == [S3_ENDPOINT_URL.name, S3_BUCKET.name]


def test_s3_endpoint_empty_string_is_rejected() -> None:
    reg = Registry(_S3_SETTINGS)
    reg.load({S3_ENDPOINT_URL.name: "", S3_BUCKET.name: "kdive"})
    with pytest.raises(CategorizedError) as ei:
        reg.validate("server")
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details["variable"] == S3_ENDPOINT_URL.name


def test_s3_bucket_whitespace_only_is_rejected() -> None:
    reg = Registry(_S3_SETTINGS)
    reg.load({S3_ENDPOINT_URL.name: "http://minio:9000", S3_BUCKET.name: "   "})
    with pytest.raises(CategorizedError) as ei:
        reg.validate("worker")
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details["variable"] == S3_BUCKET.name


# The always-required string settings that share the S3 empty-vs-absent gap (#1137):
# a present-but-blank value must fail at config.validate(), not later at pool/token time.
_ALWAYS_REQUIRED_STRINGS = [DATABASE_URL, OIDC_JWKS_URI, OIDC_ISSUER, OIDC_AUDIENCE]


@pytest.mark.parametrize("setting", _ALWAYS_REQUIRED_STRINGS, ids=lambda s: s.name)
def test_always_required_string_absent_is_rejected(setting: Setting[str]) -> None:
    reg = Registry([setting])
    reg.load({})
    with pytest.raises(CategorizedError) as ei:
        reg.validate("server")
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details["missing"] == [setting.name]


@pytest.mark.parametrize("setting", _ALWAYS_REQUIRED_STRINGS, ids=lambda s: s.name)
@pytest.mark.parametrize("blank", ["", "   "])
def test_always_required_string_blank_is_rejected(setting: Setting[str], blank: str) -> None:
    reg = Registry([setting])
    reg.load({setting.name: blank})
    with pytest.raises(CategorizedError) as ei:
        reg.validate("server")
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details["variable"] == setting.name


@pytest.mark.parametrize(
    "setting", [s for s in SETTINGS if s.required_when is _always], ids=lambda s: s.name
)
@pytest.mark.parametrize("blank", ["", "   "])
def test_always_required_settings_reject_blank(setting: Setting[str], blank: str) -> None:
    """Invariant guard: every always-required setting's parser must reject a blank value.

    A future ``required_when=_always`` string setting declared with a blank-accepting
    parser would silently reintroduce the empty-vs-absent gap #1133/#1137 closed. This
    asserts the invariant directly (parser rejects blank) rather than the ``_nonempty``
    identity, so it holds for any equivalently strict parser.
    """
    with pytest.raises(ValueError):
        setting.parse(blank)
