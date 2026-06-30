"""Tests for the client-attested external source-provenance helper (ADR-0274, #893)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_provenance import (
    PROVENANCE_FIELD_MAX_LEN,
    external_source_provenance,
)


def test_both_none_returns_none() -> None:
    assert external_source_provenance(None, None) is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n "])
def test_both_blank_after_strip_returns_none(blank: str) -> None:
    assert external_source_provenance(blank, blank) is None


def test_label_only_records_label_and_discriminator() -> None:
    assert external_source_provenance("my-fix worktree", None) == {
        "client_attested": True,
        "label": "my-fix worktree",
    }


def test_source_ref_only_records_source_ref_and_discriminator() -> None:
    assert external_source_provenance(None, "v6.9-rc1+patch") == {
        "client_attested": True,
        "source_ref": "v6.9-rc1+patch",
    }


def test_both_fields_recorded_with_discriminator() -> None:
    assert external_source_provenance("linux-6.9", "abc1234") == {
        "client_attested": True,
        "label": "linux-6.9",
        "source_ref": "abc1234",
    }


def test_surrounding_whitespace_stripped_in_stored_values() -> None:
    result = external_source_provenance("  linux-6.9  ", "  abc1234  ")
    assert result == {
        "client_attested": True,
        "label": "linux-6.9",
        "source_ref": "abc1234",
    }


def test_one_blank_one_present_records_only_the_present_field() -> None:
    assert external_source_provenance("   ", "abc1234") == {
        "client_attested": True,
        "source_ref": "abc1234",
    }


def test_max_length_boundary_accepted() -> None:
    value = "a" * PROVENANCE_FIELD_MAX_LEN
    assert external_source_provenance(value, None) == {
        "client_attested": True,
        "label": value,
    }


@pytest.mark.parametrize("field_index", [0, 1])
def test_over_length_rejected_naming_the_field(field_index: int) -> None:
    over = "a" * (PROVENANCE_FIELD_MAX_LEN + 1)
    args = [None, None]
    args[field_index] = over
    with pytest.raises(CategorizedError) as excinfo:
        external_source_provenance(args[0], args[1])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "invalid_source_provenance"
    assert excinfo.value.details["field"] == ("source_label", "source_ref")[field_index]


@pytest.mark.parametrize(
    "value",
    [
        "x\x00y",  # NUL (Cc)
        "x\ny",  # newline (Cc)
        "x\ty",  # tab (Cc)
        "a​b",  # zero-width space (Cf)
        "a‮b",  # right-to-left override (Cf)
        "a\x85b",  # NEL, a C1 control (Cc)
    ],
)
def test_non_printable_rejected(value: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        external_source_provenance(value, None)
    assert excinfo.value.details["reason"] == "invalid_source_provenance"
    assert excinfo.value.details["field"] == "source_label"


def test_error_does_not_echo_the_rejected_value() -> None:
    rejected = "x\nidentifiable-payload-9000"
    with pytest.raises(CategorizedError) as excinfo:
        external_source_provenance(None, rejected)
    assert rejected not in str(excinfo.value)
    assert rejected not in str(excinfo.value.details)
