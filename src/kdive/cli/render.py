"""Operator-facing output: a plain aligned table by default, stable JSON with ``--json``.

The curated read verbs (``kdivectl resources list`` etc.) project each row onto a fixed
column set and hand it here. JSON mode emits the same projected columns so scripts get a
stable contract; table mode left-justifies each column to its widest cell. ``None`` and
missing cells render as the empty string so a column slot is never dropped (ADR-0089).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

_GAP = "  "


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def render(rows: Sequence[Mapping[str, object]], *, columns: Sequence[str], as_json: bool) -> None:
    """Project rows onto ``columns`` and render them as stable JSON or an aligned table."""
    projected = [{c: row.get(c) for c in columns} for row in rows]
    if as_json:
        print(json.dumps(projected, indent=2, default=str))
        return
    widths = {c: len(c) for c in columns}
    for row in projected:
        for column in columns:
            widths[column] = max(widths[column], len(_cell(row[column])))
    print(_GAP.join(column.ljust(widths[column]) for column in columns))
    for row in projected:
        print(_GAP.join(_cell(row[column]).ljust(widths[column]) for column in columns))


def render_record(record: Mapping[str, object], *, as_json: bool) -> None:
    """Render a single record as aligned key/value lines, or as stable JSON.

    The single-record verbs (``describe``/``get``/``show``) return one record, not a row
    list. ``None`` values render as the empty string, matching :func:`render`.
    """
    if as_json:
        print(json.dumps(dict(record), indent=2, default=str))
        return
    width = max((len(key) for key in record), default=0)
    for key, value in record.items():
        print(f"{key.ljust(width)}{_GAP}{_cell(value)}".rstrip())


def render_report(
    rows: Sequence[Mapping[str, object]],
    totals: Mapping[str, object],
    *,
    columns: Sequence[str],
    total_columns: Sequence[str],
    as_json: bool,
) -> None:
    """Render report rows with a totals footer or as ``{"items": ..., "totals": ...}``.

    Both halves are projected onto their declared key sets so the scriptable contract is
    stable against server-side envelope additions: a ``totals`` key not in ``total_columns``
    never reaches the output, and a missing one renders blank (table) or ``null`` (JSON),
    matching :func:`render` / :func:`render_record`.
    """
    projected_totals = {c: totals.get(c) for c in total_columns}
    if as_json:
        projected_rows = [{c: row.get(c) for c in columns} for row in rows]
        document = {"items": projected_rows, "totals": projected_totals}
        print(json.dumps(document, indent=2, default=str))
        return
    render(rows, columns=columns, as_json=False)
    print()
    render_record(projected_totals, as_json=False)
