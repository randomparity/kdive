"""The served agent index must carry one consolidated pre-provision checklist (#997).

The checklist distinguishes capacity sizing, provision-time profile choices, and kernel
build configuration so an agent can plan before spending its lease on setup. The assertion
reads the served snapshot and protects discoverability of those choices.
"""

from __future__ import annotations

from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_CONTENT_DIR = Path(__file__).resolve().parents[3] / "src/kdive/mcp/resources/_content"


def _served_agent_index() -> str:
    entry = next(e for e in DOC_RESOURCES if e.name == "agent-index")
    return (_CONTENT_DIR / entry.content_file).read_text(encoding="utf-8")


def test_agent_index_has_consolidated_pre_provision_checklist() -> None:
    body = _served_agent_index()
    lowered = body.lower()
    assert "decide before you provision" in lowered, (
        "served agent-index has no consolidated pre-provision checklist (#997)"
    )
    # Keep the planning topics discoverable without treating each as provision-bound.
    for token in ("images.describe", "disk", "kernel config", "gdbstub"):
        assert token in lowered, (
            f"pre-provision checklist does not name the planning topic {token!r} (#997)"
        )
