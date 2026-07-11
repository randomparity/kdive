"""Compatibility exports for debug session tool registration."""

from __future__ import annotations

from kdive.mcp.tools.debug.sessions.lifecycle import (
    _GDBSTUB,
    DebugSessionHandlers,
    _AttachRequest,
    _insert_session_locked,
)
from kdive.mcp.tools.debug.sessions.registrar import register

__all__ = [
    "DebugSessionHandlers",
    "_AttachRequest",
    "_GDBSTUB",
    "_insert_session_locked",
    "register",
]
