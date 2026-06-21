"""Shared profile schema helper tests."""

from __future__ import annotations

import pytest

from kdive.profiles._schema import reject_coerced_schema_version


def test_reject_coerced_schema_version_accepts_plain_int() -> None:
    assert reject_coerced_schema_version(1) == 1


@pytest.mark.parametrize("value", ["1", 1.0, True, None])
def test_reject_coerced_schema_version_rejects_non_integer_values(value: object) -> None:
    with pytest.raises(ValueError, match="schema_version must be an integer"):
        reject_coerced_schema_version(value)


def test_reject_coerced_schema_version_error_message_is_exact() -> None:
    # The message is surfaced to the caller verbatim; pin it so a padded/garbled message is
    # caught (a substring match alone would not be).
    with pytest.raises(ValueError) as exc:
        reject_coerced_schema_version("1")
    assert str(exc.value) == "schema_version must be an integer"
