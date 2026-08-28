"""KDIVE's MCP surface, and the protocol range it declares (ADR-0537).

The negotiated protocol range is a property of the pinned ``mcp`` dependency, not of KDIVE
source: KDIVE never serialises a JSON-RPC envelope itself, so the wire layer — and with it
version negotiation — is delegated in full. Declaring the range here makes a dependency bump
that moves either end a reviewable edit rather than a silent consequence of ``uv lock``.

Both ends are declared because ``mcp`` advertises a *set*, not a scalar:
``mcp/server/session.py`` answers ``initialize`` with the client's requested version whenever
it appears in ``SUPPORTED_PROTOCOL_VERSIONS``, falling back to ``LATEST_PROTOCOL_VERSION``
otherwise. Asserting only the ceiling is blind in the direction that breaks clients — a
release that drops an older supported revision leaves the ceiling untouched.

``scripts/guards/check_mcp_spec_version.py`` asserts both against the installed library on every PR.
"""

from __future__ import annotations

MCP_PROTOCOL_VERSION = "2025-11-25"
"""The newest protocol revision KDIVE negotiates — ``mcp.types.LATEST_PROTOCOL_VERSION``."""

MCP_SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
"""Every revision KDIVE accepts — ``mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS``."""
