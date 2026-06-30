"""Byte-space literal jump matcher for ``artifacts.get`` (#939).

Locates a literal ``|``-OR term over the whole fetched artifact body and returns one
direction-anchored window plus a strictly-advancing continuation cursor. Matching is on raw
bytes (UTF-8-encoded terms) with line boundaries on ``\\n`` only, so the byte-offset cursor
``artifacts.get`` already uses stays exact and Unicode line separators do not over-split.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

JumpDirection = Literal["forward", "backward"]


@dataclass(frozen=True, slots=True)
class JumpHit:
    """One located match and its returned context window."""

    match_offset: int
    match_line: int
    window_start: int
    content: bytes
    next_offset: int | None


def resolve_anchor(size: int, *, direction: JumpDirection, byte_offset: int) -> int:
    """Resolve the search anchor to the direction's natural edge when unset/degenerate.

    Forward keeps the existing ``artifacts.get`` meaning (``0``/negative = from the start).
    Backward treats an omitted/``0``/negative ``byte_offset`` as end-of-artifact (a strict
    backward search from byte 0 is degenerate), and clamps a positive value to ``size``.
    """
    if direction == "forward":
        return max(byte_offset, 0)
    if byte_offset <= 0:
        return size
    return min(byte_offset, size)


def _first_match_at_or_after(body: bytes, terms_b: tuple[bytes, ...], start: int) -> int | None:
    best: int | None = None
    for term in terms_b:
        i = body.find(term, start)
        if i != -1 and (best is None or i < best):
            best = i
    return best


def _last_match_at_or_before(body: bytes, terms_b: tuple[bytes, ...], end: int) -> int | None:
    best: int | None = None
    for term in terms_b:
        i = body.rfind(term, 0, end + 1)
        if i != -1 and (best is None or i > best):
            best = i
    return best


def _line_bounds(body: bytes, offset: int) -> tuple[int, int]:
    """Return ``(line_start, line_end)`` for the ``\\n``-delimited line containing ``offset``."""
    line_start = body.rfind(b"\n", 0, offset) + 1
    nl_after = body.find(b"\n", offset)
    line_end = nl_after if nl_after != -1 else len(body)
    return line_start, line_end


def jump_find(
    body: bytes,
    *,
    terms: tuple[str, ...],
    direction: JumpDirection,
    byte_offset: int,
    max_bytes: int,
) -> JumpHit | None:
    """Locate the next/previous literal match and return its anchored window, or ``None``.

    The match is found over the entire ``body``; only the returned ``content`` window is bounded
    by ``max_bytes``. Paging is line-granular: ``next_offset`` skips past (forward) or before
    (backward) the matched line, so it strictly advances and re-supplying it enumerates matches
    in order. ``None`` means no match exists in ``direction`` from the resolved anchor.
    """
    terms_b = tuple(term.encode("utf-8") for term in terms)
    size = len(body)
    anchor = resolve_anchor(size, direction=direction, byte_offset=byte_offset)
    if direction == "forward":
        match = _first_match_at_or_after(body, terms_b, anchor)
    else:
        match = _last_match_at_or_before(body, terms_b, anchor)
    if match is None:
        return None
    line_start, line_end = _line_bounds(body, match)
    match_line = body.count(b"\n", 0, match) + 1
    if direction == "forward":
        window_start = line_start if (match - line_start) < max_bytes else match
        window_end = min(size, window_start + max_bytes)
        next_offset = line_end + 1 if line_end < size else None
    else:
        window_end = line_end
        window_start = max(0, window_end - max_bytes)
        if match < window_start:  # a long line pushed the match out of the window
            window_start = match
            window_end = min(size, match + max_bytes)
        next_offset = line_start - 1 if line_start > 0 else None
    return JumpHit(
        match_offset=match,
        match_line=match_line,
        window_start=window_start,
        content=body[window_start:window_end],
        next_offset=next_offset,
    )
