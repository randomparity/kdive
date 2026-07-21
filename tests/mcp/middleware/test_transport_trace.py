"""Tests for the opt-in ASGI transport-trace middleware (ADR-0417)."""

from __future__ import annotations

import pytest

from kdive import config
from kdive.mcp.middleware.transport_trace import mcp_trace_enabled


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("0", False),
        ("false", False),
        ("off", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("YES", True),
        ("On", True),
    ],
)
def test_mcp_trace_enabled_resolves_truthy_set(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv("KDIVE_MCP_TRACE", raising=False)
    else:
        monkeypatch.setenv("KDIVE_MCP_TRACE", raw)
    config.load()
    assert mcp_trace_enabled() is expected
