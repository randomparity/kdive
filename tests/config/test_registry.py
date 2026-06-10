"""The Setting descriptor and the snapshot-resolving Registry (ADR-0087)."""

from __future__ import annotations

import pytest

from kdive.config import Registry, Setting
from kdive.domain.errors import CategorizedError, ErrorCategory


def _int(raw: str) -> int:
    return int(raw)


def test_get_returns_parsed_value_from_snapshot() -> None:
    s = Setting(name="KDIVE_HTTP_PORT", parse=_int, default="8000", group="http")
    reg = Registry([s])
    reg.load({"KDIVE_HTTP_PORT": "9001"})
    assert reg.get(s) == 9001


def test_get_returns_parsed_default_when_absent() -> None:
    s = Setting(name="KDIVE_HTTP_PORT", parse=_int, default="8000", group="http")
    reg = Registry([s])
    reg.load({})
    assert reg.get(s) == 8000


def test_get_returns_none_when_absent_and_no_default() -> None:
    s = Setting(name="KDIVE_HTTP_PORT", parse=_int, group="http")
    reg = Registry([s])
    reg.load({})
    assert reg.get(s) is None


def test_get_raises_configuration_error_on_unparseable_value() -> None:
    s = Setting(name="KDIVE_HTTP_PORT", parse=_int, default="8000", group="http")
    reg = Registry([s])
    reg.load({"KDIVE_HTTP_PORT": "not-a-number"})
    with pytest.raises(CategorizedError) as ei:
        reg.get(s)
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details["variable"] == "KDIVE_HTTP_PORT"


def test_load_keeps_only_kdive_keys() -> None:
    s = Setting(name="KDIVE_HTTP_PORT", parse=_int, default="8000", group="http")
    reg = Registry([s])
    reg.load({"KDIVE_HTTP_PORT": "9001", "PATH": "/usr/bin"})
    reg.load({"KDIVE_HTTP_PORT": "9002"})  # the second snapshot fully replaces the first
    assert reg.get(s) == 9002


def test_unknown_process_in_setting_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown processes"):
        Setting(name="KDIVE_X", parse=str, processes=frozenset({"frobnicate"}))


def test_duplicate_setting_name_is_rejected() -> None:
    a = Setting(name="KDIVE_DUP", parse=str)
    b = Setting(name="KDIVE_DUP", parse=str)
    with pytest.raises(ValueError, match="duplicate setting"):
        Registry([a, b])
