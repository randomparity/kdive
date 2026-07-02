"""The served agent index must carry one consolidated pre-provision checklist (#997).

Several choices are provision-bound and expensive to change (a reprovision rebuilds and
reboots): the debug flags, `ssh_credential_ref`, the base image, the shape/disk, and the
kernel config baked into the upload. They used to be scattered, so this guard asserts a single
"decide before you provision" checklist names every irreversible choice in one place. The
assertion reads the **served snapshot** an agent receives over MCP, so it also fails if the
packaged snapshot falls out of sync.
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
    # Every provision-bound irreversible choice must appear in the served index.
    for token in ("images.describe", "disk", "kernel config", "gdbstub", "ssh_credential_ref"):
        assert token in lowered, (
            f"pre-provision checklist does not name the irreversible choice {token!r} (#997)"
        )
