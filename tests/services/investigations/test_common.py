"""Direct unit tests for the shared Investigation service helpers."""

from __future__ import annotations

from typing import cast

import pytest

from kdive.services.investigations.common import (
    DESCRIPTION_MAX,
    SUMMARY_MAX,
    TITLE_MAX,
    ExternalRefInput,
    ExternalRefKey,
    InvestigationErrorReason,
    InvestigationServiceError,
    invalid_external_refs_error,
    invalid_text_error,
    natural_key,
    parse_external_refs,
    require_summary,
    validate_text,
)


def test_invalid_text_error_carries_bounded_detail() -> None:
    err = invalid_text_error("inv-1")
    assert err.object_id == "inv-1"
    assert err.reason is InvestigationErrorReason.INVALID_TEXT
    assert err.detail == (
        f"title must be 1..{TITLE_MAX} chars and description at most {DESCRIPTION_MAX} chars"
    )


def test_require_summary_accepts_bounded_text() -> None:
    assert require_summary("inv-1", "found it") == "found it"
    assert require_summary("inv-1", "x" * SUMMARY_MAX) == "x" * SUMMARY_MAX


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_require_summary_blank_names_missing_field_with_detail(blank: str) -> None:
    with pytest.raises(InvestigationServiceError) as err:
        require_summary("inv-1", blank)
    assert err.value.object_id == "inv-1"
    assert err.value.reason is InvestigationErrorReason.MISSING_REQUIRED_FIELD
    assert err.value.detail == "closing an investigation requires a non-empty summary of the work"


def test_require_summary_oversized_names_invalid_text_with_detail() -> None:
    with pytest.raises(InvestigationServiceError) as err:
        require_summary("inv-1", "x" * (SUMMARY_MAX + 1))
    assert err.value.object_id == "inv-1"
    assert err.value.reason is InvestigationErrorReason.INVALID_TEXT
    assert err.value.detail == f"summary must be at most {SUMMARY_MAX} chars"


def test_natural_key_returns_tracker_and_id_for_valid_ref() -> None:
    assert natural_key({"tracker": "bz", "id": "7"}) == ("bz", "7")


@pytest.mark.parametrize(
    "ref",
    [
        {},
        {"tracker": "bz"},
        {"id": "7"},
        {"tracker": "", "id": "7"},
        {"tracker": "bz", "id": ""},
        {"tracker": 5, "id": "7"},
        {"tracker": "bz", "id": 5},
    ],
)
def test_natural_key_rejects_malformed_refs(ref: dict[str, object]) -> None:
    assert natural_key(cast(ExternalRefKey, ref)) is None


def test_parse_external_refs_none_is_empty() -> None:
    assert parse_external_refs(None) == []


def test_parse_external_refs_parses_and_dedupes_by_key() -> None:
    raw = cast(
        "list[ExternalRefInput]",
        [
            {"tracker": "bz", "id": "7", "url": "https://bz/7"},
            {"tracker": "bz", "id": "7", "url": "https://bz/7-dup"},  # same key, last wins
            {"tracker": "jira", "id": "K-1", "url": "https://j/1"},
        ],
    )
    parsed = parse_external_refs(raw)
    assert [(r.tracker, r.id) for r in parsed] == [("bz", "7"), ("jira", "K-1")]
    assert next(r for r in parsed if r.tracker == "bz").url == "https://bz/7-dup"


def test_validate_text_accepts_boundary_lengths() -> None:
    # Exactly at the inclusive upper bounds must validate (an exclusive-bound mutant would reject).
    assert validate_text("x" * TITLE_MAX, None) is True
    assert validate_text("t", "x" * DESCRIPTION_MAX) is True
    assert validate_text("x", None) is True  # minimum length 1


def test_validate_text_rejects_out_of_bounds() -> None:
    assert validate_text("", None) is False
    assert validate_text("x" * (TITLE_MAX + 1), None) is False
    assert validate_text("t", "x" * (DESCRIPTION_MAX + 1)) is False


def test_invalid_external_refs_error_detail_depends_on_key_only() -> None:
    full = invalid_external_refs_error("inv-1")
    key_only = invalid_external_refs_error("inv-1", key_only=True)
    assert full.detail == "ref must carry a tracker, id, and url"
    assert key_only.detail == "ref key must carry a non-empty tracker and id"
    assert full.reason is InvestigationErrorReason.INVALID_EXTERNAL_REF
