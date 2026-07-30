"""The shared upload-window expiry predicate (ADR-0512, #1555).

Both finalize lanes — ``runs.complete_build`` in the service layer and
``investigations.complete_rootfs_upload`` in the MCP tool layer — used to write their own
comparison of a :class:`~kdive.artifacts.upload_manifest.ManifestStamp`'s two fields, spelled as
exact logical negations of each other. ADR-0512 replaced both with
:attr:`~kdive.artifacts.upload_manifest.ManifestStamp.expired`.

These tests pin the predicate's semantics directly, because the two lanes now agree *by
construction* and a lane-level test can no longer detect a change to the rule itself. The
boundary case is the one that matters: ``deadline == server_time`` is **open**, which is what both
lanes did before the hoist, and it is exactly where a silent behaviour change would hide — the
old spellings were ``deadline < server_time`` and ``deadline >= server_time``, and getting the
new single operator off by one strictness step flips only that instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kdive.artifacts.upload_manifest import UPLOAD_WINDOW_EXPIRED, ManifestStamp

_CLOCK = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def _stamp(offset: timedelta) -> ManifestStamp:
    """A stamp whose deadline sits ``offset`` from the fixed reference clock."""
    return ManifestStamp(server_time=_CLOCK, deadline=_CLOCK + offset)


def test_deadline_equal_to_the_clock_is_open() -> None:
    """``deadline == server_time`` is not expired — the pre-hoist behaviour of both lanes.

    A window whose deadline is exactly now has not lapsed; the reaper's own ``deadline >= now()``
    arm agrees. Flipping this to ``<=`` would reject a finalize the reaper would not have
    collected, on one instant, and no lane-level test would see it.
    """
    assert _stamp(timedelta(0)).expired is False


def test_deadline_before_the_clock_is_expired() -> None:
    assert _stamp(timedelta(seconds=-1)).expired is True


def test_deadline_after_the_clock_is_open() -> None:
    assert _stamp(timedelta(seconds=1)).expired is False


def test_the_boundary_is_sharp_at_microsecond_resolution() -> None:
    """One microsecond either side of the clock decides it — ``timestamptz`` resolution.

    Both sides are asserted together so a predicate that ignored the sign of the difference, or
    that compared truncated seconds, fails here rather than passing three coarser cases.
    """
    assert _stamp(timedelta(microseconds=-1)).expired is True
    assert _stamp(timedelta(microseconds=1)).expired is False


def test_expired_reads_both_fields_of_the_stamp() -> None:
    """Moving the clock alone flips the verdict, so the predicate cannot be reading a constant."""
    hour = timedelta(hours=1)
    assert ManifestStamp(server_time=_CLOCK - hour, deadline=_CLOCK).expired is False
    assert ManifestStamp(server_time=_CLOCK + hour, deadline=_CLOCK).expired is True


def test_reason_code_is_the_wire_literal() -> None:
    """The constant's *value* is the agent-visible reason string, unchanged by the move.

    ADR-0512 relocated ``UPLOAD_WINDOW_EXPIRED`` out of ``services/runs/complete_build.py`` so
    both lanes could name it. The move is wire-invisible only while the literal is preserved, and
    both lanes' behavior tests assert this exact string.
    """
    assert UPLOAD_WINDOW_EXPIRED == "upload_window_expired"
