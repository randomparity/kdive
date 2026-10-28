"""ADR-0808: the sender marks which authority rejections prove the request was not admitted."""

from __future__ import annotations

import json

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.authority_sender import _decode_response
from kdive.providers.external_boot_authority.protocol import AuthorityObservationV1


def _rejection(category: str) -> bytes:
    return json.dumps(
        {"status": "error", "category": category}, sort_keys=True, separators=(",", ":")
    ).encode()


def _raised(category: str) -> CategorizedError:
    with pytest.raises(CategorizedError) as raised:
        _decode_response(_rejection(category), AuthorityObservationV1)
    return raised.value


@pytest.mark.parametrize(
    "reason", ["unauthenticated", "superseded", "provider-not-configured", "invalid-request"]
)
def test_a_pre_admission_rejection_is_marked_refused_and_stays_retryable(reason: str) -> None:
    error = _raised(reason)
    assert error.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert error.details == {"admission": "refused", "authority_reason": reason}


def test_a_journal_conflict_is_terminal_for_the_wait_but_not_proven_unadmitted() -> None:
    error = _raised("journal-conflict")
    assert error.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert error.details == {"admission": "unknown", "authority_reason": "journal-conflict"}


def test_a_provider_conflict_keeps_only_its_adr_0714_mark() -> None:
    assert _raised("provider-conflict").details == {"authority_reason": "provider-conflict"}


@pytest.mark.parametrize("reason", ["provider-failure", "invalid-response"])
def test_an_ambiguous_failure_carries_no_admission_mark(reason: str) -> None:
    assert "admission" not in _raised(reason).details


@pytest.mark.parametrize(
    ("reason", "completion"),
    [
        ("remote-module-refused", "refused-before-mutation"),
        ("system-held", "refused-before-mutation"),
    ],
)
def test_the_existing_completion_marks_are_unchanged(reason: str, completion: str) -> None:
    assert _raised(reason).details == {"completion": completion}
