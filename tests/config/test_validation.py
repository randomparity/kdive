"""Per-process startup validation with the conditional ``required_when`` contract."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from kdive.config import Registry, Setting
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
