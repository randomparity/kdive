"""Unit tests for the shared list-pagination helpers (`kdive.mcp.tools._common`).

Covers the opaque cursor codec (ADR-0192): round-trip, cross-tool replay rejection,
arity and malformed-token rejection, the ``limit + 1`` truncation split, and the
``invalid_cursor`` configuration-error envelope.
"""

from __future__ import annotations

import base64
import json

import pytest

from kdive.mcp.tools._common import (
    InvalidCursor,
    decode_cursor,
    encode_cursor,
    invalid_cursor_error,
    paginate,
)


def test_cursor_round_trips() -> None:
    token = encode_cursor("jobs.list", ["2026-06-20T00:00:00+00:00", "abc"])
    assert decode_cursor("jobs.list", token, arity=2) == ["2026-06-20T00:00:00+00:00", "abc"]


def test_cursor_round_trips_three_parts() -> None:
    token = encode_cursor("images.list", ["fedora", "base", "x86_64"])
    assert decode_cursor("images.list", token, arity=3) == ["fedora", "base", "x86_64"]


def test_cursor_rejects_cross_tool_replay() -> None:
    token = encode_cursor("jobs.list", ["t", "id"])
    with pytest.raises(InvalidCursor):
        decode_cursor("systems.list", token, arity=2)


def test_cursor_rejects_wrong_arity() -> None:
    token = encode_cursor("jobs.list", ["t", "id"])
    with pytest.raises(InvalidCursor):
        decode_cursor("jobs.list", token, arity=3)


@pytest.mark.parametrize(
    "bad",
    [
        "!!!",
        "not-base64-$$$",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode(),
        base64.urlsafe_b64encode(json.dumps({"t": "jobs.list"}).encode()).decode(),
        base64.urlsafe_b64encode(json.dumps({"t": "jobs.list", "k": "x"}).encode()).decode(),
        base64.urlsafe_b64encode(json.dumps({"t": "jobs.list", "k": [1, 2]}).encode()).decode(),
    ],
)
def test_cursor_rejects_malformed(bad: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor("jobs.list", bad, arity=2)


def test_paginate_flags_truncation_on_extra_row() -> None:
    kept, truncated = paginate([1, 2, 3], 2)
    assert kept == [1, 2]
    assert truncated is True


def test_paginate_no_truncation_at_exactly_limit() -> None:
    kept, truncated = paginate([1, 2], 2)
    assert kept == [1, 2]
    assert truncated is False


def test_paginate_empty() -> None:
    kept, truncated = paginate([], 2)
    assert kept == []
    assert truncated is False


def test_invalid_cursor_error_envelope() -> None:
    resp = invalid_cursor_error("jobs")
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    assert resp.data["reason"] == "invalid_cursor"
