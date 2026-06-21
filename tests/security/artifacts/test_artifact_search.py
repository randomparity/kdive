from __future__ import annotations

import pytest

from kdive.security.artifacts.artifact_search import (
    MAX_MATCHES_JSON_CHARS,
    MAX_PATTERN_CHARS,
    MAX_TERMS,
    ArtifactSearchInputError,
    parse_literal_terms,
    search_text,
)


def test_parse_literal_terms_splits_or_terms() -> None:
    assert parse_literal_terms("__d_lookup|Oops") == ("__d_lookup", "Oops")


def test_parse_literal_terms_accepts_max_length_pattern() -> None:
    pattern = "a" * MAX_PATTERN_CHARS
    assert parse_literal_terms(pattern) == (pattern,)


def test_parse_literal_terms_rejects_over_max_length() -> None:
    with pytest.raises(ArtifactSearchInputError, match=r"^pattern must be 1-256 characters$"):
        parse_literal_terms("a" * (MAX_PATTERN_CHARS + 1))


def test_parse_literal_terms_rejects_empty() -> None:
    with pytest.raises(ArtifactSearchInputError, match=r"^pattern must be 1-256 characters$"):
        parse_literal_terms("")


def test_parse_literal_terms_rejects_non_str() -> None:
    with pytest.raises(ArtifactSearchInputError, match=r"^pattern must be 1-256 characters$"):
        parse_literal_terms(b"abc")  # ty: ignore[invalid-argument-type]


def test_parse_literal_terms_rejects_nul() -> None:
    with pytest.raises(ArtifactSearchInputError, match=r"^pattern must not contain NUL$"):
        parse_literal_terms("bad\x00term")


def test_parse_literal_terms_rejects_empty_term() -> None:
    with pytest.raises(ArtifactSearchInputError, match=r"^pattern contains an empty term$"):
        parse_literal_terms("a||b")


def test_parse_literal_terms_accepts_max_terms() -> None:
    pattern = "|".join(f"t{i}" for i in range(MAX_TERMS))
    assert len(parse_literal_terms(pattern)) == MAX_TERMS


def test_parse_literal_terms_rejects_too_many_terms() -> None:
    with pytest.raises(ArtifactSearchInputError, match=r"^pattern has too many terms$"):
        parse_literal_terms("|".join(f"t{i}" for i in range(MAX_TERMS + 1)))


def test_search_text_returns_bounded_context() -> None:
    data = b"line one\npanic start\nRIP: __d_lookup+0x1\nnext line\n"
    result = search_text(
        data,
        pattern="__d_lookup|Oops",
        before_lines=1,
        after_lines=1,
        max_matches=5,
    )
    assert result.match_count == 1
    assert result.truncated is False
    assert result.matches[0]["line"] == 3
    assert result.matches[0]["before"] == ["panic start"]
    assert result.matches[0]["after"] == ["next line"]


def test_search_text_default_context_window() -> None:
    # Defaults: before_lines=2, after_lines=4. A match in the middle picks up exactly that many.
    lines = [f"l{i}" for i in range(20)]
    lines[10] = "NEEDLE here"
    data = ("\n".join(lines)).encode()
    result = search_text(data, pattern="NEEDLE")
    match = result.matches[0]
    assert match["before"] == ["l8", "l9"]
    assert match["after"] == ["l11", "l12", "l13", "l14"]


def test_search_text_window_clamps_at_file_edges() -> None:
    # Match on the first line: no before context; after stops at the last line.
    data = b"NEEDLE\nl1\nl2"
    result = search_text(data, pattern="NEEDLE", before_lines=3, after_lines=10)
    match = result.matches[0]
    assert match["before"] == []
    assert match["after"] == ["l1", "l2"]


def test_search_text_default_max_matches_is_20() -> None:
    data = ("\n".join("NEEDLE" for _ in range(25))).encode()
    result = search_text(data, pattern="NEEDLE", before_lines=0, after_lines=0)
    assert result.match_count == 20
    assert result.truncated is True


def test_search_text_stops_exactly_at_max_matches() -> None:
    data = ("\n".join("NEEDLE" for _ in range(5))).encode()
    result = search_text(data, pattern="NEEDLE", before_lines=0, after_lines=0, max_matches=3)
    assert result.match_count == 3
    assert result.truncated is True


def test_search_text_not_truncated_when_under_max() -> None:
    data = ("\n".join("NEEDLE" for _ in range(3))).encode()
    result = search_text(data, pattern="NEEDLE", before_lines=0, after_lines=0, max_matches=5)
    assert result.match_count == 3
    assert result.truncated is False


@pytest.mark.parametrize(
    ("kwargs", "label"),
    [
        ({"before_lines": 11}, "before_lines"),
        ({"before_lines": -1}, "before_lines"),
        ({"after_lines": 21}, "after_lines"),
        ({"after_lines": -1}, "after_lines"),
        ({"max_matches": 51}, "max_matches"),
        ({"max_matches": 0}, "max_matches"),
    ],
)
def test_search_text_rejects_out_of_range_bounds(kwargs: dict[str, int], label: str) -> None:
    with pytest.raises(ArtifactSearchInputError, match=f"{label} out of range"):
        search_text(b"NEEDLE", pattern="NEEDLE", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"before_lines": 10},
        {"before_lines": 0},
        {"after_lines": 20},
        {"after_lines": 0},
        {"max_matches": 50},
        {"max_matches": 1},
    ],
)
def test_search_text_accepts_boundary_bounds(kwargs: dict[str, int]) -> None:
    result = search_text(b"NEEDLE", pattern="NEEDLE", **kwargs)
    assert result.match_count == 1


def test_search_text_replaces_invalid_utf8_bytes() -> None:
    # Invalid UTF-8 must be replaced (errors="replace"), not raise — the line is still searchable.
    data = b"prefix \xff\xfe NEEDLE suffix"
    result = search_text(data, pattern="NEEDLE", before_lines=0, after_lines=0)
    assert result.match_count == 1
    assert "�" in result.matches[0]["text"]


def test_search_text_does_not_clip_at_exact_limit() -> None:
    # A line of exactly MAX_LINE_CHARS (512) is returned verbatim — the clip is len > limit.
    line = "N" + "a" * 505 + "NEEDLE"  # 512 chars, contains the pattern
    assert len(line) == 512
    result = search_text(line.encode(), pattern="NEEDLE", before_lines=0, after_lines=0)
    assert result.matches[0]["text"] == line


def test_search_text_clips_long_lines_and_total_json() -> None:
    data = ("x" * 900 + " NEEDLE\n").encode()
    result = search_text(data, pattern="NEEDLE", before_lines=0, after_lines=0, max_matches=1)
    assert len(result.matches[0]["text"]) <= 512 + len("...[clipped]")


def test_search_text_before_window_starts_at_zero_not_one() -> None:
    # Match at index 2 with before_lines=2: the window starts at index 0, so BOTH preceding
    # lines are captured (a clamp to index 1 would drop the first).
    data = b"first\nsecond\nNEEDLE\nafter"
    result = search_text(data, pattern="NEEDLE", before_lines=2, after_lines=0)
    assert result.matches[0]["before"] == ["first", "second"]


def test_search_text_before_window_clamps_negative_start_to_zero() -> None:
    # Match near the top of a file longer than before_lines. start = max(0, idx - before_lines)
    # must clamp to 0 so the single preceding line is captured. Dropping the max() yields a
    # negative start (idx - before_lines = 1 - 10 = -9), and lines[-9:1] on a 20-line file is
    # [] rather than ["l0"] — the negative index wraps to the tail, past the stop index.
    lines = [f"l{i}" for i in range(20)]
    lines[1] = "NEEDLE here"
    data = ("\n".join(lines)).encode()
    result = search_text(data, pattern="NEEDLE", before_lines=10, after_lines=0)
    assert result.matches[0]["before"] == ["l0"]


def test_search_text_truncates_when_json_budget_exceeded() -> None:
    # The truncation cut-off is decided by re-serializing the running match list with the SAME
    # compact, non-escaping json.dumps args the dataclass uses (separators=(",",":"),
    # ensure_ascii=False). Pin the exact match_count at the budget boundary so that any change to
    # those serializer args shifts the observed count and is caught.
    #
    # The payload is sized so the JSON budget (not max_matches=50) is the limiter and the cut-off
    # lands at exactly 35 matches. The boundary is razor-thin: a match object carries four keys
    # (line/text/before/after), so a key-separator change adds only four chars per match, while an
    # item-separator change adds one char per array element. The before/after window is therefore
    # tuned (before=0, after=9) so BOTH the key-separator-only and the item-separator-only widening
    # drop exactly the 35th match -> 34. (An earlier 42-match payload with a wide context window was
    # equivalent for the key separator: the four extra chars/match never crossed the boundary.)
    line = "NEEDLE" + "é" * 174
    data = ("\n".join([line] * 4000)).encode("utf-8")
    result = search_text(data, pattern="NEEDLE", before_lines=0, after_lines=9, max_matches=50)
    assert result.truncated is True
    # Exact count pins the cut-off. Any serializer widening drops the 35th match:
    #   * item-separator ","->", " -> 34
    #   * key-separator ":"->": " -> 34
    #   * ensure_ascii=True escapes each "é" to "é" (six chars), blowing the estimate -> ~6
    assert result.match_count == 35
    assert len(result.matches_json()) <= MAX_MATCHES_JSON_CHARS
