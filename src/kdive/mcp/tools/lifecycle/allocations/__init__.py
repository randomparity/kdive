"""Allocation lifecycle MCP tool handlers."""

from kdive.mcp.auth import current_context
from kdive.mcp.tools.lifecycle.allocations.common import (
    _allocation_next_actions,
    _envelope_for_allocation,
    _queue_position,
    allocation_next_actions,
    envelope_for_allocation,
    queue_position,
)
from kdive.mcp.tools.lifecycle.allocations.lifecycle import (
    ReleaseOutcome,
    RenewOutcome,
    _release_response,
    _renew_response,
    release_allocation,
    renew_allocation,
)
from kdive.mcp.tools.lifecycle.allocations.registrar import register
from kdive.mcp.tools.lifecycle.allocations.request import request_allocation
from kdive.mcp.tools.lifecycle.allocations.view import (
    get_allocation,
    list_allocations,
    wait_allocation,
)

__all__ = [
    "_allocation_next_actions",
    "_envelope_for_allocation",
    "_queue_position",
    "_release_response",
    "_renew_response",
    "ReleaseOutcome",
    "RenewOutcome",
    "allocation_next_actions",
    "current_context",
    "envelope_for_allocation",
    "get_allocation",
    "list_allocations",
    "queue_position",
    "register",
    "release_allocation",
    "renew_allocation",
    "request_allocation",
    "wait_allocation",
]
