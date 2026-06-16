"""Shared build-config validation rules (ADR-0122)."""

from __future__ import annotations

import pytest

from kdive.build_configs.rules import (
    exceeds_build_config_cap,
    validate_build_config_content,
    validate_build_config_name,
)


@pytest.mark.parametrize("name", ["kdump", "a", "k1", "kdump-debug", "kdump_debug", "a" * 64])
def test_valid_names_pass(name: str) -> None:
    assert validate_build_config_name(name) == name


@pytest.mark.parametrize("name", ["", "Kdump", "-kdump", "kdump!", "a" * 65, "kd/ump", "kd ump"])
def test_invalid_names_raise(name: str) -> None:
    with pytest.raises(ValueError):
        validate_build_config_name(name)


def test_nonempty_content_passes() -> None:
    assert validate_build_config_content("CONFIG_KEXEC=y\n") == "CONFIG_KEXEC=y\n"


def test_empty_content_raises() -> None:
    with pytest.raises(ValueError):
        validate_build_config_content("")


def test_cap_predicate() -> None:
    assert exceeds_build_config_cap(b"x" * 11, 10) is True
    assert exceeds_build_config_cap(b"x" * 10, 10) is False
    assert exceeds_build_config_cap(b"", 10) is False
