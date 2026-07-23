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


def flatten_envelope(envelope: object) -> dict[str, object]:
    """Flatten one response envelope into a row: ``id``/``state`` plus the envelope's ``data``.

    ``id`` comes from ``object_id`` and ``state`` from ``status``; every ``data`` key is lifted
    to a top-level cell. Accepts ``object`` because the items of a collection envelope arrive
    untyped from the wire; a non-mapping (e.g. a degraded row) flattens to an empty row rather
    than raising. This is the shared projection the curated read/mutation verbs also use.
    """
    if not isinstance(envelope, Mapping):
        return {}
    fields: Mapping[str, object] = {str(k): v for k, v in envelope.items()}
    row: dict[str, object] = {"id": fields.get("object_id"), "state": fields.get("status")}
    data = fields.get("data")
    if isinstance(data, Mapping):
        for key, value in data.items():
            row[str(key)] = value
    return row


def _union_columns(rows: Sequence[Mapping[str, object]]) -> list[str]:
    """Return every key across ``rows`` in stable first-seen order (no declared column set)."""
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def render_envelope(envelope: Mapping[str, object], *, as_json: bool) -> None:
    """Render a response envelope with no hand-picked column set (epic #1442 R11).

    This is the column-agnostic renderer for *generated* verbs, whose columns nobody chose:

    * ``as_json=True`` prints the WHOLE envelope unprojected, so the agent-navigation contract
      (``suggested_next_actions``, ``refs``, ``error_category``, nested ``items``) survives.
    * A collection envelope (non-empty ``items``) flattens each item via
      :func:`flatten_envelope` and tables them over the *union* of all row keys, computed in
      stable first-seen order rather than declared.
    * A single envelope (empty ``items``) flattens the one envelope and renders it as a record.

    Leaves the curated verbs' fixed-column :func:`render` / :func:`render_report` path untouched.
    """
    if as_json:
        print(json.dumps(dict(envelope), indent=2, default=str))
        return
    items = envelope.get("items")
    if isinstance(items, list) and items:
        rows = [flatten_envelope(item) for item in items]
        render(rows, columns=_union_columns(rows), as_json=False)
        return
    render_record(flatten_envelope(envelope), as_json=False)


def render_record(record: Mapping[str, object], *, as_json: bool) -> None:
    """Render a single record as aligned key/value lines, or as stable JSON.

    The single-record ``get`` verbs return one record, not a row list. ``None`` values
    render as the empty string, matching :func:`render`.
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
