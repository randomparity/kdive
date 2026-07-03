"""The served debug/introspect guides must route by-name struct-field reads to drgn (#991).

`debug.resolve_symbol` resolves a symbol's *address* only; the gdbstub path evaluates no
member/array/type expressions. The supported way to read `some_struct->field[3].member` by
name on a live guest is the drgn path (`introspect.script`). That routing is the whole
deliverable of #991 — the completeness and snapshot guards tie each doc to its live tools and
to canonical `docs/`, but neither asserts the routing prose exists, so a later edit could drop
it with CI still green.

The assertions read the **served snapshots** an agent actually reads over MCP (not the
canonical doc path), so they also fail if a snapshot falls out of sync.
"""

from __future__ import annotations

from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_CONTENT_DIR = Path(__file__).resolve().parents[3] / "src/kdive/mcp/resources/_content"

# The issue's acceptance shape — the exact expression an agent must have a documented way to
# read by name. Anchoring the guard on it ties the doc to the #991 acceptance criterion.
_ACCEPTANCE_SHAPE = "some_struct->field[3].member"


def _served_snapshot(name: str) -> str:
    """Read a packaged toolset snapshot the way the resource registrar serves it."""
    entry = next(e for e in DOC_RESOURCES if e.name == name)
    return (_CONTENT_DIR / entry.content_file).read_text(encoding="utf-8")


def test_debug_guide_routes_field_reads_to_drgn() -> None:
    body = _served_snapshot("toolset-debug")
    lowered = body.lower()
    assert "introspect.script" in body, (
        "served debug guide does not route member/array reads to introspect.script (#991)"
    )
    assert "struct field" in lowered, (
        "served debug guide does not tell the agent the gdbstub path cannot read a struct "
        "field by name (#991)"
    )


def test_introspect_guide_documents_by_name_field_read() -> None:
    body = _served_snapshot("toolset-introspect")
    assert _ACCEPTANCE_SHAPE in body, (
        f"served introspect guide does not document reading {_ACCEPTANCE_SHAPE!r} by name (#991)"
    )
    assert "introspect.script" in body, (
        "served introspect guide does not name introspect.script as the by-name field-read path"
    )
