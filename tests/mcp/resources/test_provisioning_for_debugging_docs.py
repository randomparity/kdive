"""The served agent index must carry the provisioning-for-debugging guidance (#955).

The completeness guard ties each toolset doc to its live tools and the snapshot guard ties
the served bytes to canonical ``docs/`` — but neither asserts that the provision-time
debug/live-introspection guidance is present. Without this guard, a later edit could drop
the section (the issue's whole deliverable) with CI still green.

The assertion is behavioral: it checks the section heading and the three provision-bound
knobs an agent must set at provision (``gdbstub``, ``preserve_on_crash``,
``ssh_credential_ref``) are named in the **served snapshot** an agent actually reads over
MCP — not a hard-coded doc path — so it also fails if the snapshot falls out of sync.
"""

from __future__ import annotations

from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_CONTENT_DIR = Path(__file__).resolve().parents[3] / "src/kdive/mcp/resources/_content"


def _served_agent_index() -> str:
    """Read the packaged ``agent-index`` snapshot the way the resource registrar serves it."""
    entry = next(e for e in DOC_RESOURCES if e.name == "agent-index")
    return (_CONTENT_DIR / entry.content_file).read_text(encoding="utf-8")


def test_agent_index_names_provision_bound_debug_knobs() -> None:
    body = _served_agent_index()
    lowered = body.lower()
    assert "provisioning for debugging" in lowered, (
        "served agent-index is missing the provisioning-for-debugging section (#955)"
    )
    for knob in ("gdbstub", "preserve_on_crash", "ssh_credential_ref"):
        assert knob in body, f"served agent-index does not name the provision-bound knob {knob!r}"
