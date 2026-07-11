"""MCP adapters for Investigation metadata services."""

from __future__ import annotations

from kdive.services.investigations.metadata import (
    link_external_ref,
    set_investigation,
    unlink_external_ref,
)

__all__ = ["link_external_ref", "set_investigation", "unlink_external_ref"]
