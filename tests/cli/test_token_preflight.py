"""The fail-closed token-``exp`` preflight refuses a token too close to (or past) expiry.

A destructive break-glass verb runs this preflight before its single MCP call so a
near-expired token is refused up front rather than risking a mid-operation 401. Fail-closed
means a missing/unparsable ``exp`` is treated as expiring, and the boundary (``exp - now``
exactly equal to the margin) is refused (ADR-0089).
"""

from __future__ import annotations

import base64
import json

import pytest

from kdive.cli.commands.mutations import TokenExpiringError, ensure_token_valid


def _jwt_with_claims(claims: dict[str, object]) -> str:
    """Build a structurally-valid unsigned JWT carrying ``claims`` in its body segment."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"x.{body}.y"


def _jwt_with_exp(exp_epoch: int) -> str:
    return _jwt_with_claims({"exp": exp_epoch})


def test_refuses_token_expiring_within_margin() -> None:
    with pytest.raises(TokenExpiringError):
        ensure_token_valid(_jwt_with_exp(1000), now=995, margin_s=30)


def test_accepts_token_with_headroom() -> None:
    ensure_token_valid(_jwt_with_exp(10_000), now=1000, margin_s=30)


def test_refuses_exactly_at_margin_boundary() -> None:
    # exp - now == margin is refused (<=): fail closed on the boundary.
    with pytest.raises(TokenExpiringError):
        ensure_token_valid(_jwt_with_exp(1030), now=1000, margin_s=30)


def test_accepts_one_second_past_margin() -> None:
    ensure_token_valid(_jwt_with_exp(1031), now=1000, margin_s=30)


def test_refuses_already_expired_token() -> None:
    with pytest.raises(TokenExpiringError):
        ensure_token_valid(_jwt_with_exp(500), now=1000, margin_s=30)


def test_refuses_token_missing_exp_claim() -> None:
    with pytest.raises(TokenExpiringError):
        ensure_token_valid(_jwt_with_claims({"sub": "u"}), now=1000, margin_s=30)


def test_refuses_non_numeric_exp() -> None:
    with pytest.raises(TokenExpiringError):
        ensure_token_valid(_jwt_with_claims({"exp": "soon"}), now=1000, margin_s=30)


def test_refuses_malformed_jwt_without_segments() -> None:
    with pytest.raises(TokenExpiringError):
        ensure_token_valid("not-a-jwt", now=1000, margin_s=30)


def test_refuses_jwt_with_undecodable_body() -> None:
    with pytest.raises(TokenExpiringError):
        ensure_token_valid("x.!!!not-base64!!!.y", now=1000, margin_s=30)


def test_refuses_empty_token() -> None:
    with pytest.raises(TokenExpiringError):
        ensure_token_valid("", now=1000, margin_s=30)


def test_error_does_not_leak_the_token() -> None:
    token = _jwt_with_exp(500)
    with pytest.raises(TokenExpiringError) as excinfo:
        ensure_token_valid(token, now=1000, margin_s=30)
    assert token not in str(excinfo.value)


def test_error_message_directs_to_relogin() -> None:
    with pytest.raises(TokenExpiringError) as excinfo:
        ensure_token_valid(_jwt_with_exp(500), now=1000, margin_s=30)
    assert str(excinfo.value) == (
        "token missing exp or expiring soon; run `kdivectl login` and retry"
    )


def test_accepts_two_segment_token() -> None:
    # A header.body token (no signature segment) is still parsable: the structural guard
    # rejects only fewer than two segments, so a two-part token with headroom is accepted.
    body = base64.urlsafe_b64encode(json.dumps({"exp": 10_000}).encode()).rstrip(b"=").decode()
    two_segment = f"x.{body}"
    ensure_token_valid(two_segment, now=1000, margin_s=30)


def test_default_margin_is_thirty_seconds() -> None:
    # Called without margin_s, the default 30s applies: exp-now == 30 is refused (<=),
    # exp-now == 31 is accepted. This pins the default value, not just an explicit arg.
    with pytest.raises(TokenExpiringError):
        ensure_token_valid(_jwt_with_exp(1030), now=1000)
    ensure_token_valid(_jwt_with_exp(1031), now=1000)
