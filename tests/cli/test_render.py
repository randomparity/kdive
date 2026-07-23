"""``render``/``render_record``/``render_report`` emit the default human table.

``--json`` is no longer a projection here: every curated verb's ``--json`` prints the whole
server envelope via :func:`render_envelope` (ADR-0421 §6), routed through :func:`emit`. The
table renderers below are the default (non-``--json``) path only and take no ``as_json`` flag.
"""

from __future__ import annotations

import json

from kdive.cli.render import (
    emit,
    flatten_envelope,
    render,
    render_envelope,
    render_record,
    render_report,
)

ROWS = [{"id": "r1", "kind": "local-libvirt"}, {"id": "r2", "kind": "remote-libvirt"}]


def test_table_mode_has_header_and_rows(capsys) -> None:
    render(ROWS, columns=["id", "kind"])
    out = capsys.readouterr().out
    assert "id" in out and "r1" in out and "remote-libvirt" in out


def test_table_mode_columns_are_aligned(capsys) -> None:
    render(ROWS, columns=["id", "kind"])
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
    render(rows, columns=["id", "kind"])
    lines = capsys.readouterr().out.splitlines()
    # Header column is left-justified: "id" sits at the start, padding follows it.
    assert lines[0].startswith("id ")
    # The narrow "x" data cell is left-justified within the column width.
    assert lines[3].startswith("x ")
    assert not lines[3].startswith(" ")


def test_empty_rows_table_still_prints_header(capsys) -> None:
    render([], columns=["id", "kind"])
    out = capsys.readouterr().out.strip()
    assert out == "id    kind" or ("id" in out and "kind" in out)
    # Exactly the header line, no data rows.
    assert len(out.splitlines()) == 1


def test_missing_key_renders_blank_cell(capsys) -> None:
    render([{"id": "r1"}], columns=["id", "kind"])
    lines = capsys.readouterr().out.splitlines()
    # The data row keeps the column slot but leaves the missing cell blank.
    assert lines[1].startswith("r1")
    assert lines[1].rstrip() == "r1"


def test_none_value_renders_as_empty(capsys) -> None:
    render([{"id": "r1", "kind": None}], columns=["id", "kind"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].rstrip() == "r1"


def test_render_record_keyvalue(capsys) -> None:
    render_record({"id": "r1", "kind": "local-libvirt"})
    out = capsys.readouterr().out
    assert "id" in out and "r1" in out and "kind" in out


def test_render_record_empty_record(capsys) -> None:
    render_record({})
    assert capsys.readouterr().out == ""


def test_render_record_none_value_renders_blank(capsys) -> None:
    render_record({"id": "r1", "host": None})
    out = capsys.readouterr().out
    assert "host" in out
    assert "None" not in out


def test_render_record_lines_have_no_trailing_whitespace(capsys) -> None:
    # A ``None`` value renders blank, and the trailing gap/pad is stripped from the line.
    render_record({"id": "r1", "host": None})
    lines = capsys.readouterr().out.splitlines()
    for line in lines:
        assert line == line.rstrip(), f"unexpected trailing whitespace: {line!r}"
    # The blank-valued line is just the (left-justified) key with no trailing gap.
    assert lines[1] == "host"


def test_render_record_keys_are_left_justified(capsys) -> None:
    # Keys are padded to the widest key on the RIGHT so values line up in a column.
    render_record({"id": "r1", "hostname": "h"})
    lines = capsys.readouterr().out.splitlines()
    # "id" is the short key: left-justified means it starts the line, padding follows.
    assert lines[0].startswith("id ")
    assert not lines[0].startswith(" id")
    # Both values start at the same column offset (aligned).
    assert lines[0].index("r1") == lines[1].rindex("h")


_REPORT_COLS = ["project", "reserved"]
_REPORT_TCOLS = ["scope", "total_reserved"]


def test_render_report_table_has_rows_then_totals_footer(capsys) -> None:
    rows = [{"project": "p", "reserved": "1.0"}]
    totals = {"scope": "all-projects", "total_reserved": "1.0"}
    render_report(rows, totals, columns=_REPORT_COLS, total_columns=_REPORT_TCOLS)
    lines = capsys.readouterr().out.splitlines()
    assert "project" in lines[0] and any("p" in line for line in lines)  # row table
    assert "" in lines  # blank separator line
    assert any("scope" in line and "all-projects" in line for line in lines)  # totals footer


def test_render_report_empty_rows_still_prints_header_and_totals(capsys) -> None:
    render_report([], {"scope": "all-projects"}, columns=_REPORT_COLS, total_columns=["scope"])
    out = capsys.readouterr().out
    assert "project" in out and "scope" in out and "all-projects" in out


def test_render_report_footer_projects_onto_total_columns(capsys) -> None:
    # A totals key outside total_columns never reaches the footer; a missing one renders blank.
    totals = {"scope": "all-projects", "extra": "drop-me"}
    render_report([], totals, columns=_REPORT_COLS, total_columns=_REPORT_TCOLS)
    out = capsys.readouterr().out
    assert "all-projects" in out
    assert "drop-me" not in out
    assert "total_reserved" in out  # the missing total keeps its (blank) footer slot


# --- emit: the single --json branch shared by every curated verb (ADR-0421 §6) ---


def test_emit_json_prints_whole_envelope_and_skips_table(capsys) -> None:
    envelope = {
        "object_id": "r1",
        "status": "ok",
        "data": {"kind": "k"},
        "items": [],
        "suggested_next_actions": ["jobs.wait"],
    }

    def _table() -> None:  # pragma: no cover - must not run in --json mode
        raise AssertionError("table renderer must not run when as_json=True")

    emit(envelope, _table, as_json=True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == envelope
    assert parsed["suggested_next_actions"] == ["jobs.wait"]


def test_emit_table_runs_the_callback_and_prints_no_json(capsys) -> None:
    called: list[bool] = []
    emit({"object_id": "r1"}, lambda: called.append(True), as_json=False)
    assert called == [True]
    assert capsys.readouterr().out == ""


# --- flatten_envelope (the id/state/data projection shared with the read/mutation verbs) ---


def _item(object_id: str, status: str, data: dict) -> dict:
    return {"object_id": object_id, "status": status, "data": data, "items": []}


def _collection(items: list[dict], **extra: object) -> dict:
    base = {"object_id": "col", "status": "ok", "data": {"count": len(items)}, "items": items}
    return {**base, **extra}


def test_flatten_envelope_keeps_id_state_and_data(capsys) -> None:
    row = flatten_envelope(_item("r1", "ok", {"kind": "k", "host": "h"}))
    assert row == {"id": "r1", "state": "ok", "kind": "k", "host": "h"}


def test_flatten_envelope_non_mapping_is_empty_row() -> None:
    # A degraded/untyped item flattens to an empty row rather than raising.
    assert flatten_envelope("not-a-mapping") == {}
    assert flatten_envelope(None) == {}


# --- render_envelope: the whole-envelope --json path for every verb (ADR-0421 §6) ---


def test_render_envelope_json_emits_whole_unprojected_envelope(capsys) -> None:
    # The key property: --json keeps the navigation contract fields, not a projection.
    envelope = _item("r1", "ok", {"kind": "k"})
    envelope["suggested_next_actions"] = ["jobs.wait", "jobs.cancel"]
    envelope["refs"] = {"result": "s3://x"}
    envelope["error_category"] = None
    render_envelope(envelope, as_json=True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == envelope
    assert parsed["suggested_next_actions"] == ["jobs.wait", "jobs.cancel"]


def test_render_envelope_json_is_pretty_printed_with_two_space_indent(capsys) -> None:
    render_envelope({"id": "r1"}, as_json=True)
    assert capsys.readouterr().out == '{\n  "id": "r1"\n}\n'


def test_render_envelope_json_serializes_non_json_values_via_str(capsys) -> None:
    from pathlib import PurePosixPath

    render_envelope({"path": PurePosixPath("/a/b")}, as_json=True)
    assert json.loads(capsys.readouterr().out) == {"path": "/a/b"}


def test_render_envelope_json_collection_keeps_items_and_next_actions(capsys) -> None:
    coll = _collection(
        [_item("r1", "ok", {"kind": "k"})],
        suggested_next_actions=["allocations.release"],
    )
    render_envelope(coll, as_json=True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["items"][0]["object_id"] == "r1"
    assert parsed["suggested_next_actions"] == ["allocations.release"]


def test_render_envelope_collection_tables_over_union_of_keys(capsys) -> None:
    # Heterogeneous item data keys: the table's columns are the UNION across all rows,
    # not any single item's keys, and never a declared list.
    coll = _collection(
        [
            _item("r1", "ok", {"kind": "k"}),
            _item("r2", "ok", {"host": "h", "kind": "k2"}),
        ]
    )
    render_envelope(coll, as_json=False)
    lines = capsys.readouterr().out.splitlines()
    header = lines[0]
    # id/state come first (from flatten), then first-seen data keys: kind, then host.
    assert header.split() == ["id", "state", "kind", "host"]
    # The row missing "kind" keeps the slot blank; the row missing "host" likewise.
    assert "r1" in lines[1] and "k" in lines[1]
    assert "r2" in lines[2] and "h" in lines[2] and "k2" in lines[2]


def test_render_envelope_collection_columns_are_deterministic_first_seen(capsys) -> None:
    coll = _collection(
        [
            _item("a", "ok", {"b": 1, "a": 2}),
            _item("c", "ok", {"c": 3, "a": 4}),
        ]
    )
    render_envelope(coll, as_json=False)
    header = capsys.readouterr().out.splitlines()[0]
    # First-seen order across the union: id, state, b, a, c.
    assert header.split() == ["id", "state", "b", "a", "c"]


def test_render_envelope_single_renders_as_record(capsys) -> None:
    # Empty items -> flatten the one envelope and render it as a key/value record.
    render_envelope(_item("r1", "ok", {"kind": "k", "host": "h"}), as_json=False)
    lines = capsys.readouterr().out.splitlines()
    joined = "\n".join(lines)
    assert "id" in joined and "r1" in joined
    assert "state" in joined and "ok" in joined
    assert "kind" in joined and "k" in joined
    # A record is one key per line, not a single header+row table.
    assert any(line.startswith("id") for line in lines)


def test_render_envelope_empty_collection_uses_record_path(capsys) -> None:
    # An empty item list is not a table; it falls to the single-record path (spec).
    render_envelope(_collection([]), as_json=False)
    out = capsys.readouterr().out
    assert "id" in out and "col" in out and "count" in out
