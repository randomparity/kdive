"""Tests for the shared client-label validator (ADR-0264, #867)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.labels import LABEL_MAX_LEN, validate_label


def test_none_returns_none() -> None:
    assert validate_label(None) is None


def test_strips_surrounding_whitespace_preserving_interior_space() -> None:
    assert validate_label("  my run  ") == "my run"


@pytest.mark.parametrize("value", ["", "   ", "\t\n "])
def test_empty_after_strip_rejected(value: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        validate_label(value)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "invalid_label"


def test_max_length_boundary_accepted() -> None:
    label = "a" * LABEL_MAX_LEN
    assert validate_label(label) == label


def test_over_length_rejected() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        validate_label("a" * (LABEL_MAX_LEN + 1))
    assert excinfo.value.details["reason"] == "invalid_label"


@pytest.mark.parametrize(
    "value",
    [
        "x\x00y",  # NUL (Cc)
        "x\ny",  # newline (Cc)
        "x\ty",  # tab (Cc)
        "a​b",  # zero-width space (Cf)
        "a‮b",  # right-to-left override (Cf)
        "a b",  # non-breaking space (Zs)
        "a\x85b",  # NEL, a C1 control (Cc)
    ],
)
def test_non_printable_characters_rejected(value: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        validate_label(value)
    assert excinfo.value.details["reason"] == "invalid_label"


@pytest.mark.parametrize("value", ["café-run", "run #42", "kdump-repro-A"])
def test_printable_unicode_and_ascii_accepted(value: str) -> None:
    assert validate_label(value) == value


def test_error_does_not_echo_the_rejected_value() -> None:
    rejected = "x\nidentifiable-payload-9000"
    with pytest.raises(CategorizedError) as excinfo:
        validate_label(rejected)
    assert rejected not in str(excinfo.value)
    assert rejected not in str(excinfo.value.details)
