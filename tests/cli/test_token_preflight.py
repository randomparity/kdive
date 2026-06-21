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


@pytest.mark.parametrize(
    ("claims", "expected_body_len"),
    [
        # A 35-char (len % 4 == 3) body: the correct pad count is 1. A wrong modulus
        # (e.g. `% 4` -> `% 5`) under-pads this length, so base64.urlsafe_b64decode raises
        # binascii.Error, _decode_exp returns None, and the token is wrongly refused. This
        # is the body length that pins the modulus in the padding line.
        ({"exp": 10_000_000, "s": ""}, 35),
        # A 20-char (len % 4 == 0) body needs zero padding: the padding line must add an
        # empty string, exercising the no-padding branch.
        ({"exp": 100_000}, 20),
    ],
)
def test_accepts_token_across_body_padding_lengths(
    claims: dict[str, object], expected_body_len: int
) -> None:
    # _jwt_with_claims strips '=' padding, so the body fed to _decode_exp is unpadded; the
    # source's `body += "=" * (-len(body) % 4)` line is the only thing that restores it.
    # base64.urlsafe_b64decode rejects an under-padded body (binascii.Error), so a mutant
    # that corrupts the pad count makes _decode_exp return None for one of these body
    # lengths and the token is wrongly refused. The exp is far past now so a decodable body
    # is accepted.
    token = _jwt_with_claims(claims)
    assert len(token.split(".")[1]) == expected_body_len
    ensure_token_valid(token, now=0, margin_s=30)


def test_default_margin_is_thirty_seconds() -> None:
    # Called without margin_s, the default 30s applies: exp-now == 30 is refused (<=),
    # exp-now == 31 is accepted. This pins the default value, not just an explicit arg.
    with pytest.raises(TokenExpiringError):
        ensure_token_valid(_jwt_with_exp(1030), now=1000)
    ensure_token_valid(_jwt_with_exp(1031), now=1000)
