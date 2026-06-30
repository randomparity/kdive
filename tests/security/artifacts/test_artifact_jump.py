"""Unit tests for the byte-space literal jump matcher (#939)."""

from __future__ import annotations

from kdive.security.artifacts.artifact_jump import jump_find, resolve_anchor

BODY = b"line one\nBUG: panic here\nline three\ntail BUG: again\nlast\n"


def test_forward_first_hit_from_start() -> None:
    hit = jump_find(BODY, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=24 * 1024)
    assert hit is not None
    assert hit.match_offset == BODY.index(b"BUG:")
    assert hit.match_line == 2
    assert b"BUG: panic here" in hit.content
    assert hit.next_offset == BODY.index(b"line three")


def test_forward_paging_enumerates_then_exhausts() -> None:
    first = jump_find(BODY, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=4096)
    assert first is not None and first.next_offset is not None
    second = jump_find(
        BODY, terms=("BUG:",), direction="forward", byte_offset=first.next_offset, max_bytes=4096
    )
    assert second is not None
    assert second.match_offset == BODY.index(b"tail BUG:") + len("tail ")
    assert second.next_offset is not None
    third = jump_find(
        BODY, terms=("BUG:",), direction="forward", byte_offset=second.next_offset, max_bytes=4096
    )
    assert third is None


def test_backward_default_offset_starts_from_end() -> None:
    hit = jump_find(BODY, terms=("BUG:",), direction="backward", byte_offset=0, max_bytes=4096)
    assert hit is not None
    assert hit.match_offset == BODY.rindex(b"BUG:")  # the LAST match
    assert hit.next_offset is not None and hit.next_offset < hit.match_offset


def test_backward_negative_offset_also_end() -> None:
    hit = jump_find(BODY, terms=("BUG:",), direction="backward", byte_offset=-1, max_bytes=4096)
    assert hit is not None and hit.match_offset == BODY.rindex(b"BUG:")


def test_backward_paging_walks_up_then_exhausts() -> None:
    last = jump_find(BODY, terms=("BUG:",), direction="backward", byte_offset=0, max_bytes=4096)
    assert last is not None and last.next_offset is not None
    prev = jump_find(
        BODY, terms=("BUG:",), direction="backward", byte_offset=last.next_offset, max_bytes=4096
    )
    assert prev is not None and prev.match_offset == BODY.index(b"BUG: panic")
    assert prev.next_offset is not None  # line one still precedes the match line
    # the next backward call searches line one only, which has no match
    exhausted = jump_find(
        BODY, terms=("BUG:",), direction="backward", byte_offset=prev.next_offset, max_bytes=4096
    )
    assert exhausted is None


def test_or_terms_jump_to_nearest_forward() -> None:
    hit = jump_find(
        BODY, terms=("absent", "three"), direction="forward", byte_offset=0, max_bytes=4096
    )
    assert hit is not None and hit.match_offset == BODY.index(b"three")


def test_no_match_returns_none() -> None:
    assert (
        jump_find(BODY, terms=("nope",), direction="forward", byte_offset=0, max_bytes=4096) is None
    )
    assert (
        jump_find(BODY, terms=("nope",), direction="backward", byte_offset=0, max_bytes=4096)
        is None
    )


def test_match_at_offset_zero() -> None:
    body = b"BUG: at start\nmore\n"
    hit = jump_find(body, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=4096)
    assert hit is not None
    assert hit.match_offset == 0 and hit.window_start == 0 and hit.match_line == 1


def test_match_at_eof_no_trailing_newline() -> None:
    body = b"first\nlast line BUG:"  # no trailing newline
    hit = jump_find(body, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=4096)
    assert hit is not None
    assert b"BUG:" in hit.content
    assert hit.next_offset is None  # nothing after the final line


def test_long_line_anchors_window_at_match() -> None:
    body = b"X" * 30000 + b"BUG:" + b"Y" * 100  # single line longer than the cap
    hit = jump_find(body, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=24 * 1024)
    assert hit is not None
    assert b"BUG:" in hit.content  # term must be in-window despite the long line
    assert hit.window_start == body.index(b"BUG:")


def test_long_line_backward_anchors_window_at_match() -> None:
    body = b"BUG:" + b"Z" * 30000  # match near the start of a very long single line
    hit = jump_find(body, terms=("BUG:",), direction="backward", byte_offset=0, max_bytes=24 * 1024)
    assert hit is not None
    assert b"BUG:" in hit.content
    assert hit.window_start == 0


def test_byte_space_no_unicode_oversplit() -> None:
    # U+2028 (line separator) must NOT act as a line boundary; only \n does.
    body = "a b BUG: x\nnext\n".encode()
    hit = jump_find(body, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=4096)
    assert hit is not None and hit.match_line == 1  # whole first physical (\n-delimited) line


def test_resolve_anchor_edges() -> None:
    assert resolve_anchor(100, direction="forward", byte_offset=0) == 0
    assert resolve_anchor(100, direction="forward", byte_offset=-5) == 0
    assert resolve_anchor(100, direction="forward", byte_offset=40) == 40
    assert resolve_anchor(100, direction="backward", byte_offset=0) == 100
    assert resolve_anchor(100, direction="backward", byte_offset=-1) == 100
    assert resolve_anchor(100, direction="backward", byte_offset=40) == 40
    assert resolve_anchor(100, direction="backward", byte_offset=999) == 100
