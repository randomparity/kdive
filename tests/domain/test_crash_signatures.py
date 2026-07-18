"""Crash-signature preset map and matching coverage (ADR-0266, #865)."""

from __future__ import annotations

import pytest

from kdive.domain.lifecycle.crash_signatures import (
    CONSOLE_CRASH_KIND,
    CONSOLE_CRASH_KINDS,
    CRASH_SIGNATURE_PRESETS,
)
from kdive.security.artifacts.artifact_search import parse_literal_terms, search_text


def test_console_crash_kinds_is_console_crash_plus_presets() -> None:
    assert CONSOLE_CRASH_KIND == "console_crash"
    expected = frozenset({"console_crash", "oops", "panic", "hung_task", "ubsan"})
    assert expected == CONSOLE_CRASH_KINDS
    assert CONSOLE_CRASH_KIND not in CRASH_SIGNATURE_PRESETS
    assert set(CRASH_SIGNATURE_PRESETS) == {"oops", "panic", "hung_task", "ubsan"}


@pytest.mark.parametrize("preset", sorted(CRASH_SIGNATURE_PRESETS))
def test_every_preset_pattern_is_a_valid_literal_pattern(preset: str) -> None:
    # A malformed preset entry (empty term, NUL, >16 terms, >256 chars) would silently
    # break boot matching; parse_literal_terms is the same gate the model validator relies on.
    terms = parse_literal_terms(CRASH_SIGNATURE_PRESETS[preset])
    assert terms
    assert len(terms) <= 16
    assert len(CRASH_SIGNATURE_PRESETS[preset]) <= 256


def _matches(preset: str, console: str) -> bool:
    result = search_text(
        console.encode("utf-8"),
        pattern=CRASH_SIGNATURE_PRESETS[preset],
        before_lines=0,
        after_lines=0,
        max_matches=1,
    )
    return result.match_count > 0


def test_panic_preset_matches_panic_console_line() -> None:
    assert _matches("panic", "Kernel panic - not syncing: Attempted to kill init!")
    assert not _matches("panic", "everything is fine\nsystemd: started\n")


@pytest.mark.parametrize(
    "console_line",
    [
        "BUG: unable to handle page fault for address: ffff8881",  # v5.0+
        "BUG: unable to handle kernel paging request at 00000010",  # EL8 / pre-v5.0
        "BUG: kernel NULL pointer dereference, address: 0000000000000000",
        "Oops: 0000 [#1] SMP PTI",
        "Internal error: Oops: 96000004 [#1] SMP",  # arm64
        "kernel BUG at mm/slub.c:4567!",
    ],
)
def test_oops_preset_matches_oops_variants(console_line: str) -> None:
    assert _matches("oops", f"some prior line\n{console_line}\nfollowing line\n")


def test_oops_preset_ignores_unrelated_console() -> None:
    assert not _matches("oops", "loaded module\nstarting services\n")


@pytest.mark.parametrize(
    "console_line",
    [
        "INFO: task khungtaskd:42 blocked for more than 120 seconds.",
        "INFO: task dd:456 blocked in I/O wait for more than 122 seconds.",
        '"echo 0 > /proc/sys/kernel/hung_task_timeout_secs" disables this message.',
    ],
)
def test_hung_task_preset_matches_hung_task_variants(console_line: str) -> None:
    assert _matches("hung_task", f"prior\n{console_line}\nafter\n")


@pytest.mark.parametrize(
    "console_line",
    [
        "UBSAN: shift-out-of-bounds in kernel/foo.c:12:34",
        "UBSAN: array-index-out-of-bounds in drivers/bar.c:5:6",
        "UBSAN: signed-integer-overflow in net/baz.c:7:8",
    ],
)
def test_ubsan_preset_matches_ubsan_report_variants(console_line: str) -> None:
    assert _matches("ubsan", f"prior\n{console_line}\nafter\n")


def test_ubsan_preset_ignores_unrelated_console() -> None:
    assert not _matches("ubsan", "loaded module\nstarting services\n")
