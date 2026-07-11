"""Debug session tool registration package."""

from __future__ import annotations

from kdive.mcp.tools.debug.sessions.lifecycle import (
    DebugSessionHandlers,
)
from kdive.mcp.tools.debug.sessions.registrar import register

__all__ = [
    "DebugSessionHandlers",
    "register",
]
