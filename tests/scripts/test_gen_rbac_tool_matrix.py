"""RBAC tool-visibility matrix generator behavior + drift guard (#347)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import gen_rbac_tool_matrix as gen


def test_render_hides_admin_only_tool_from_lower_roles() -> None:
    block = gen.render()
    teardown = next(line for line in block.splitlines() if line.startswith("| `systems.teardown`"))

    # Columns: Tool, Scope, Viewer, Contributor, Operator, Admin, Plat-Op, Plat-Aud, Plat-Adm
    cells = [cell.strip() for cell in teardown.split("|")[1:-1]]
    viewer, contributor, operator, admin = cells[2], cells[3], cells[4], cells[5]
    assert (viewer, contributor, operator) == ("", "", "")
    assert admin == "✓"


def test_render_marks_public_tool_visible_to_every_profile() -> None:
    block = gen.render()
    whoami = next(line for line in block.splitlines() if line.startswith("| `session.whoami`"))

    role_cells = [cell.strip() for cell in whoami.split("|")[1:-1]][2:]
    assert role_cells == ["✓"] * len(gen._PROFILES)
    assert "(public)" in whoami


def test_splice_replaces_only_the_marked_region() -> None:
    doc = f"head\n{gen.BEGIN_MARKER}\nOLD\n{gen.END_MARKER}\ntail\n"

    result = gen.splice(doc, "NEW")

    assert result == f"head\n{gen.BEGIN_MARKER}\nNEW\n{gen.END_MARKER}\ntail\n"


def test_splice_rejects_missing_markers() -> None:
    with pytest.raises(SystemExit):
        gen.splice("no markers here", "NEW")


def test_check_detects_drift(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(f"{gen.BEGIN_MARKER}\nstale\n{gen.END_MARKER}\n", encoding="utf-8")

    assert gen.check(doc) == 1


def test_write_makes_check_pass(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(f"intro\n{gen.BEGIN_MARKER}\n{gen.END_MARKER}\nend\n", encoding="utf-8")

    gen.write(doc)

    assert gen.check(doc) == 0


def test_committed_doc_is_in_sync(capsys: pytest.CaptureFixture[str]) -> None:
    assert gen.main(["--check"]) == 0
    assert "rbac-tool-matrix: in sync." in capsys.readouterr().out
