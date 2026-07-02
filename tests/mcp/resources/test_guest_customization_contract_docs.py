"""The served agent docs must state the guest-customization contract (#992).

An agent that does not know it has root in the guest — and that the guest package manager
is its own to install tracers/compilers/stress tools with — assumes a capability is missing
instead of installing the tool it needs (`apt install trace-cmd`). This is the most common
observed failure mode, so the contract is a deliverable in its own right and must not be
silently dropped by a later doc edit with CI still green.

The assertions read the **served snapshots** an agent actually receives over MCP (not the
`docs/` source), so they also fail if the packaged snapshot falls out of sync.
"""

from __future__ import annotations

from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_CONTENT_DIR = Path(__file__).resolve().parents[3] / "src/kdive/mcp/resources/_content"


def _served(name: str) -> str:
    """Read the packaged snapshot for doc resource ``name`` the way the registrar serves it."""
    entry = next(e for e in DOC_RESOURCES if e.name == name)
    return (_CONTENT_DIR / entry.content_file).read_text(encoding="utf-8")


def test_agent_index_states_guest_is_yours_as_root() -> None:
    body = _served("agent-index")
    lowered = body.lower()
    assert "authorize_ssh_key" in body, (
        "served agent-index does not name systems.authorize_ssh_key as the root-SSH grant (#992)"
    )
    assert "package manager" in lowered, (
        "served agent-index does not tell the agent the guest package manager is its own (#992)"
    )
    assert "disk" in lowered, "served agent-index does not mention guest disk headroom (#992)"


def test_systems_toolset_states_guest_is_yours_as_root() -> None:
    body = _served("toolset-systems")
    lowered = body.lower()
    assert "authorize_ssh_key" in body
    assert "package manager" in lowered, (
        "served systems toolset does not tell the agent the guest package manager is its own (#992)"
    )
