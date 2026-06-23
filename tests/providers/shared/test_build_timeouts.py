"""Pin the shared provider build subprocess timeout."""

from __future__ import annotations

from kdive.providers.shared import build_timeouts


def test_slow_build_tool_timeout_is_thirty_minutes() -> None:
    assert build_timeouts.SLOW_BUILD_TOOL_TIMEOUT_S == 30 * 60
    assert build_timeouts.SLOW_BUILD_TOOL_TIMEOUT_S == 1800
