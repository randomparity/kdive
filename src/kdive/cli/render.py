"""Operator-facing output: a plain aligned table by default, the server envelope with ``--json``.

Every curated verb renders a human-facing table by default and, with ``--json``, prints the
server response envelope verbatim via :func:`render_envelope` — the one thing ``--json`` means
across the whole surface (ADR-0421 §6). :func:`emit` is the shared branch each verb calls.

The table renderers here (:func:`render`, :func:`render_record`, :func:`render_report`) are the
default (non-``--json``) path only: each projects its rows onto a fixed column set and left-
justifies each column to its widest cell. ``None`` and missing cells render as the empty string
so a column slot is never dropped (ADR-0089).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence

_GAP = "  "


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def emit(envelope: Mapping[str, object], table: Callable[[], None], *, as_json: bool) -> None:
    """Print the whole envelope as JSON on ``--json``, else run the curated ``table`` renderer.

    This is the single definition of what ``--json`` means for a curated verb: the server
    response envelope verbatim (ADR-0421 §6), not a hand-picked column projection. ``table`` is
    a zero-argument callable that renders the default human table when ``--json`` is not set.
    """
    if as_json:
        render_envelope(envelope, as_json=True)
        return
    table()


def render(rows: Sequence[Mapping[str, object]], *, columns: Sequence[str]) -> None:
    """Project rows onto ``columns`` and render them as an aligned table (default output)."""
    projected = [{c: row.get(c) for c in columns} for row in rows]
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
    """Render a response envelope, the shared ``--json`` path for every verb (epic #1442).

    * ``as_json=True`` prints the WHOLE envelope unprojected, so the agent-navigation contract
      (``suggested_next_actions``, ``refs``, ``error_category``, nested ``items``) survives. This
      is what ``--json`` emits on every curated and generated verb (ADR-0421 §6).
    * The table path (``as_json=False``, used by the *generated* verbs whose columns nobody
      chose) flattens a collection's ``items`` via :func:`flatten_envelope` and tables them over
      the *union* of all row keys in stable first-seen order; a single envelope (empty ``items``)
      renders as a record.

    Curated verbs supply their own fixed-column table via :func:`render` / :func:`render_report`;
    they reach this function only through :func:`emit`'s ``--json`` branch.
    """
    if as_json:
        print(json.dumps(dict(envelope), indent=2, default=str))
        return
    items = envelope.get("items")
    if isinstance(items, list) and items:
        rows = [flatten_envelope(item) for item in items]
        render(rows, columns=_union_columns(rows))
        return
    render_record(flatten_envelope(envelope))


def render_record(record: Mapping[str, object]) -> None:
    """Render a single record as aligned key/value lines (default output).

    The single-record ``get`` verbs return one record, not a row list. ``None`` values
    render as the empty string, matching :func:`render`.
    """
    width = max((len(key) for key in record), default=0)
    for key, value in record.items():
        print(f"{key.ljust(width)}{_GAP}{_cell(value)}".rstrip())


def render_report(
    rows: Sequence[Mapping[str, object]],
    totals: Mapping[str, object],
    *,
    columns: Sequence[str],
    total_columns: Sequence[str],
) -> None:
    """Render report rows as an aligned table with a projected totals footer (default output).

    Both halves are projected onto their declared key sets: a ``totals`` key not in
    ``total_columns`` never reaches the footer, and a missing one renders blank, matching
    :func:`render` / :func:`render_record`.
    """
    projected_totals = {c: totals.get(c) for c in total_columns}
    render(rows, columns=columns)
    print()
    render_record(projected_totals)
