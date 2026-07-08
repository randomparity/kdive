"""The served debug toolset doc must explain the benign ``nokaslr`` console line (#1038).

gdbstub provisioning injects ``nokaslr`` to disable KASLR so symbol breakpoints resolve
against the fetched vmlinux (``services/runs/steps.py``, #711). The token is consumed in
early boot, so the kernel's later unknown-parameter check prints
``Unknown kernel command line parameters "nokaslr", will be passed to user space`` — expected
and harmless. Without this guard a later edit could drop the note (the issue's whole
deliverable) with CI still green.

The assertion reads the **served snapshot** an agent actually gets over MCP — not a
hard-coded ``docs/`` path — so it also fails if the snapshot falls out of sync with canonical
``docs/``.
"""

from __future__ import annotations

from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_CONTENT_DIR = Path(__file__).resolve().parents[3] / "src/kdive/mcp/resources/_content"


def _served_debug_toolset() -> str:
    """Read the packaged ``toolset-debug`` snapshot the way the resource registrar serves it."""
    entry = next(e for e in DOC_RESOURCES if e.name == "toolset-debug")
    return (_CONTENT_DIR / entry.content_file).read_text(encoding="utf-8")


def test_served_debug_toolset_documents_benign_nokaslr_console_line() -> None:
    body = _served_debug_toolset()
    lowered = body.lower()
    assert "nokaslr" in lowered, "served debug toolset does not mention nokaslr (#1038)"
    assert "disable kaslr" in lowered, (
        "served debug toolset does not state nokaslr disables KASLR — the correct, load-bearing "
        "reasoning the issue flagged, not 'noise because KASLR is already off' (#1038)"
    )
    assert "will be passed to user space" in body, (
        "served debug toolset does not quote the benign unknown-parameter console line (#1038)"
    )
