"""RBAC tool-visibility matrix generator behavior + drift guard (#347)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.mcp.exposure import ExposureScope
from kdive.security.authz.rbac import PlatformRole, Role
from scripts import gen_rbac_tool_matrix as gen


def test_every_exposure_scope_has_a_label() -> None:
    # A new ExposureScope without a label would KeyError in render(); pin it as a clear failure.
    assert set(gen._SCOPE_LABEL) == set(ExposureScope)


def test_profiles_have_a_dedicated_column_per_role() -> None:
    # A new role without a matrix column makes its tools render blank across all columns —
    # a clean-but-misleading table the drift guard (doc == render) cannot catch. Assert one
    # column per role by each column's *own* grant, not by scope_satisfied (which role
    # implication would let a single high-privilege column use to mask a missing lower one).
    column_project_roles = {ctx.roles[ctx.projects[0]] for _, ctx in gen._PROFILES if ctx.projects}
    column_platform_roles = {role for _, ctx in gen._PROFILES for role in ctx.platform_roles}
    assert column_project_roles == set(Role)
    assert column_platform_roles == set(PlatformRole)
    assert len(gen._PROFILES) == len(Role) + len(PlatformRole)


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
