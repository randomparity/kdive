"""Tests for the diagnostic-SysRq capture core (ADR-0285, #925)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from kdive.jobs.handlers.control.diagnostic_sysrq import CaptureResult, capture_console_delta


def _scripted_reader(frames: list[bytes]) -> Callable[[], Awaitable[bytes]]:
    """Return an async console reader that yields ``frames`` in order, then repeats the last."""
    state = {"i": 0}

    async def _read() -> bytes:
        i = min(state["i"], len(frames) - 1)
        state["i"] += 1
        return frames[i]

    return _read


async def _noop_sleep(_seconds: float) -> None:
    return None


async def _capture(
    frames: list[bytes], *, seam_overlap: int = 0, max_polls: int = 8, settle_polls: int = 2
) -> tuple[CaptureResult, list[str]]:
    injected: list[str] = []

    async def _inject() -> None:
        injected.append("inject")

    result = await capture_console_delta(
        _scripted_reader(frames),
        _inject,
        _noop_sleep,
        seam_overlap=seam_overlap,
        poll_interval=0.0,
        max_polls=max_polls,
        settle_polls=settle_polls,
    )
    return result, injected


def test_growth_then_stable_returns_delta_and_injects_once() -> None:
    frames = [b"", b"AAA", b"AAAdump", b"AAAdump", b"AAAdump"]
    result, injected = asyncio.run(_capture(frames))
    assert result.exit_reason == "stabilized"
    assert result.raw == b"AAAdump"
    assert injected == ["inject"]


def test_still_growing_at_bound_reports_hit_bound() -> None:
    frames = [b"", b"A", b"AA", b"AAA", b"AAAA"]
    result, _ = asyncio.run(_capture(frames, max_polls=3))
    assert result.exit_reason == "hit_bound"
    assert result.raw == b"AAA"


def test_no_growth_reports_no_output_with_empty_delta() -> None:
    frames = [b"xxx", b"xxx", b"xxx"]
    result, _ = asyncio.run(_capture(frames))
    assert result.exit_reason == "no_output"
    assert result.raw == b""


def test_seam_overlap_keeps_a_secret_straddling_the_mark_contiguous() -> None:
    before = b"log line SEC"  # mark falls inside the "SECRET=abc" token
    grown = b"log line SECRET=abc dump\n"
    result, _ = asyncio.run(_capture([before, grown, grown, grown], seam_overlap=8))
    assert result.exit_reason == "stabilized"
    # The pre-mark overlap is included so the whole secret token is contiguous for redaction.
    assert b"SECRET=abc" in result.raw
    assert result.raw == grown[len(before) - 8 :]


def test_disabled_marker_in_growth_reports_disabled() -> None:
    disabled = b"sysrq: This sysrq operation is disabled.\n"
    frames = [b"boot\n", b"boot\n" + disabled, b"boot\n" + disabled, b"boot\n" + disabled]
    result, injected = asyncio.run(_capture(frames))
    assert result.exit_reason == "disabled"
    assert injected == ["inject"]


def test_disabled_marker_present_before_mark_does_not_report_disabled() -> None:
    # The marker sits in the retained boot log (before the injection mark); a fresh, real
    # dump grows after it. Only the post-mark growth is inspected, so this is not `disabled`.
    before = b"boot\nsysrq: This sysrq operation is disabled.\n"
    grown = before + b"SysRq : Show Memory\n dump\n"
    result, _ = asyncio.run(_capture([before, grown, grown, grown], seam_overlap=64))
    assert result.exit_reason == "stabilized"
    # The overlap still carries the old marker into the stored slice, but it did not fail.
    assert b"This sysrq operation is disabled." in result.raw
