"""Control worker-plane registration."""

from __future__ import annotations

from kdive.mcp.tools.control import (
    force_crash_handler,
    power_handler,
    register_handlers,
)

__all__ = ["force_crash_handler", "power_handler", "register_handlers"]
