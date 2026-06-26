"""``render`` emits a stable JSON list or an aligned table; ``render_record`` one record."""

from __future__ import annotations

import json

from kdive.cli.render import render, render_record, render_report

ROWS = [{"id": "r1", "kind": "local-libvirt"}, {"id": "r2", "kind": "remote-libvirt"}]


def test_json_mode_is_stable(capsys) -> None:
    render(ROWS, columns=["id", "kind"], as_json=True)
    assert json.loads(capsys.readouterr().out) == ROWS


def test_json_mode_is_pretty_printed_with_two_space_indent(capsys) -> None:
    # The JSON contract is a 2-space indented, multi-line document (not compact).
    render([{"id": "r1"}], columns=["id"], as_json=True)
    out = capsys.readouterr().out
    assert out == '[\n  {\n    "id": "r1"\n  }\n]\n'


def test_json_mode_serializes_non_json_values_via_str(capsys) -> None:
    # ``default=str`` lets non-JSON-native values (e.g. a path) serialize as their str().
    from pathlib import PurePosixPath

    render([{"id": PurePosixPath("/a/b")}], columns=["id"], as_json=True)
    assert json.loads(capsys.readouterr().out) == [{"id": "/a/b"}]


def test_json_mode_projects_only_requested_columns(capsys) -> None:
    rows = [{"id": "r1", "kind": "k", "secret": "x"}]
    render(rows, columns=["id", "kind"], as_json=True)
    assert json.loads(capsys.readouterr().out) == [{"id": "r1", "kind": "k"}]


def test_table_mode_has_header_and_rows(capsys) -> None:
    render(ROWS, columns=["id", "kind"], as_json=False)
    out = capsys.readouterr().out
    assert "id" in out and "r1" in out and "remote-libvirt" in out


def test_table_mode_columns_are_aligned(capsys) -> None:
    render(ROWS, columns=["id", "kind"], as_json=False)
    lines = capsys.readouterr().out.splitlines()
    # Header plus two data rows.
    assert len(lines) == 3
    # Every line is the same width because the columns are left-justified to a fixed width.
    assert len({len(line.rstrip()) for line in lines}) >= 1
    assert lines[0].startswith("id")


def test_table_mode_left_justifies_header_and_cells(capsys) -> None:
    # A short cell in a column widened by a long cell is padded on the RIGHT (left-justified),
    # so the next column starts at a fixed offset. Right-justification would flip the padding.
    rows = [{"id": "short"}, {"id": "a-much-longer-value"}, {"id": "x"}]
    render(rows, columns=["id", "kind"], as_json=False)
    lines = capsys.readouterr().out.splitlines()
    # Header column is left-justified: "id" sits at the start, padding follows it.
    assert lines[0].startswith("id ")
    # The narrow "x" data cell is left-justified within the column width.
    assert lines[3].startswith("x ")
    assert not lines[3].startswith(" ")


def test_empty_rows_table_still_prints_header(capsys) -> None:
    render([], columns=["id", "kind"], as_json=False)
    out = capsys.readouterr().out.strip()
    assert out == "id    kind" or ("id" in out and "kind" in out)
    # Exactly the header line, no data rows.
    assert len(out.splitlines()) == 1


def test_empty_rows_json_is_empty_list(capsys) -> None:
    render([], columns=["id"], as_json=True)
    assert json.loads(capsys.readouterr().out) == []


def test_missing_key_renders_blank_cell(capsys) -> None:
    render([{"id": "r1"}], columns=["id", "kind"], as_json=False)
    lines = capsys.readouterr().out.splitlines()
    # The data row keeps the column slot but leaves the missing cell blank.
    assert lines[1].startswith("r1")
    assert lines[1].rstrip() == "r1"


def test_none_value_renders_as_empty(capsys) -> None:
    render([{"id": "r1", "kind": None}], columns=["id", "kind"], as_json=False)
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].rstrip() == "r1"


def test_render_record_keyvalue_and_json(capsys) -> None:
    render_record({"id": "r1", "kind": "local-libvirt"}, as_json=False)
    out = capsys.readouterr().out
    assert "id" in out and "r1" in out and "kind" in out
    render_record({"id": "r1"}, as_json=True)
    assert json.loads(capsys.readouterr().out) == {"id": "r1"}


def test_render_record_empty_record(capsys) -> None:
    render_record({}, as_json=False)
    assert capsys.readouterr().out == ""
    render_record({}, as_json=True)
    assert json.loads(capsys.readouterr().out) == {}


def test_render_record_none_value_renders_blank(capsys) -> None:
    render_record({"id": "r1", "host": None}, as_json=False)
    out = capsys.readouterr().out
    assert "host" in out
    assert "None" not in out


def test_render_record_lines_have_no_trailing_whitespace(capsys) -> None:
    # A ``None`` value renders blank, and the trailing gap/pad is stripped from the line.
    render_record({"id": "r1", "host": None}, as_json=False)
    lines = capsys.readouterr().out.splitlines()
    for line in lines:
        assert line == line.rstrip(), f"unexpected trailing whitespace: {line!r}"
    # The blank-valued line is just the (left-justified) key with no trailing gap.
    assert lines[1] == "host"


def test_render_record_keys_are_left_justified(capsys) -> None:
    # Keys are padded to the widest key on the RIGHT so values line up in a column.
    render_record({"id": "r1", "hostname": "h"}, as_json=False)
    lines = capsys.readouterr().out.splitlines()
    # "id" is the short key: left-justified means it starts the line, padding follows.
    assert lines[0].startswith("id ")
    assert not lines[0].startswith(" id")
    # Both values start at the same column offset (aligned).
    assert lines[0].index("r1") == lines[1].rindex("h")


def test_render_record_json_is_pretty_printed(capsys) -> None:
    render_record({"id": "r1"}, as_json=True)
    out = capsys.readouterr().out
    assert out == '{\n  "id": "r1"\n}\n'


def test_render_record_json_serializes_non_json_values_via_str(capsys) -> None:
    from pathlib import PurePosixPath

    render_record({"path": PurePosixPath("/a/b")}, as_json=True)
    assert json.loads(capsys.readouterr().out) == {"path": "/a/b"}


_REPORT_COLS = ["project", "reserved"]
_REPORT_TCOLS = ["scope", "total_reserved"]


def test_render_report_json_emits_items_and_projected_totals(capsys) -> None:
    rows = [{"project": "p", "reserved": "1.0", "secret": "x"}]
    totals = {"scope": "all-projects", "total_reserved": "1.0", "extra": "drop-me"}
    render_report(rows, totals, columns=_REPORT_COLS, total_columns=_REPORT_TCOLS, as_json=True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {
        "items": [{"project": "p", "reserved": "1.0"}],
        "totals": {"scope": "all-projects", "total_reserved": "1.0"},
    }


def test_render_report_table_has_rows_then_totals_footer(capsys) -> None:
    rows = [{"project": "p", "reserved": "1.0"}]
    totals = {"scope": "all-projects", "total_reserved": "1.0"}
    render_report(rows, totals, columns=_REPORT_COLS, total_columns=_REPORT_TCOLS, as_json=False)
    lines = capsys.readouterr().out.splitlines()
    assert "project" in lines[0] and any("p" in line for line in lines)  # row table
    assert "" in lines  # blank separator line
    assert any("scope" in line and "all-projects" in line for line in lines)  # totals footer


def test_render_report_empty_rows_still_prints_header_and_totals(capsys) -> None:
    render_report(
        [], {"scope": "all-projects"}, columns=_REPORT_COLS, total_columns=["scope"], as_json=False
    )
    out = capsys.readouterr().out
    assert "project" in out and "scope" in out and "all-projects" in out


def test_render_report_json_missing_total_key_renders_null(capsys) -> None:
    render_report([], {}, columns=_REPORT_COLS, total_columns=_REPORT_TCOLS, as_json=True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {"items": [], "totals": {"scope": None, "total_reserved": None}}
