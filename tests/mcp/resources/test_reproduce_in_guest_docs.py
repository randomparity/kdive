"""The served agent index must document the reproduce-and-capture loop (#994).

The typical session used to jump from boot straight to observe/debug with no reproduce stage,
yet the reproduce-and-capture loop is where most investigation time goes. The load-bearing
gotcha — a panic drops the SSH channel, so the serial-console sidecar is the durable record —
must be present so an agent does not treat a lost SSH session as lost evidence. This guard
keeps the stage and the gotcha from being dropped by a later edit.

The assertions read the **served snapshot** an agent receives over MCP, so they also fail if
the packaged snapshot falls out of sync with the source doc.
"""

from __future__ import annotations

from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_CONTENT_DIR = Path(__file__).resolve().parents[3] / "src/kdive/mcp/resources/_content"


def _served_agent_index() -> str:
    entry = next(e for e in DOC_RESOURCES if e.name == "agent-index")
    return (_CONTENT_DIR / entry.content_file).read_text(encoding="utf-8")


def test_agent_index_has_reproduce_stage() -> None:
    lowered = _served_agent_index().lower()
    assert "reproduce" in lowered, "served agent-index has no reproduce stage (#994)"
    assert "scp" in lowered, (
        "served agent-index reproduce stage does not cover getting the reproducer in-guest (#994)"
    )


def test_agent_index_states_panic_drops_ssh() -> None:
    lowered = _served_agent_index().lower()
    assert "panic drops your ssh" in lowered or (
        "panic" in lowered and "ssh" in lowered and "console" in lowered
    ), "served agent-index does not state the panic-drops-SSH / console-is-durable gotcha (#994)"
