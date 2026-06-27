"""Curated read verbs: call one read-only MCP tool, flatten its envelope, render it.

Each verb builds an arguments payload from its parsed ``argparse`` namespace, calls the
mapped read tool through an authenticated session, and renders the result. List verbs
flatten the collection envelope's ``items`` into rows (``id`` from ``object_id``, ``state``
from ``status``, the rest from each item's ``data``) and call :func:`render`. Single-record
verbs flatten the one envelope the same way and call :func:`render_record` (ADR-0089).

``_session_factory`` is the seam the tests replace with a fake session.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping

from kdive.cli.errors import exit_code_for_envelope
from kdive.cli.render import render, render_record, render_report
from kdive.cli.transport import Session, tool_envelope


def _session_factory() -> Session:
    return Session.from_env()


async def _fetch(name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
    session = _session_factory()
    async with session.client() as client:
        result = await client.call_tool(name, dict(arguments))
    return tool_envelope(result)


async def fetch_read_envelope(name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
    return await _fetch(name, arguments)


def _flatten(envelope: object) -> dict[str, object]:
    """Flatten one envelope into a row: ``id``/``state`` plus the envelope's ``data``.

    Accepts ``object`` because the items of a collection envelope arrive untyped from the
    wire; a non-mapping (e.g. a degraded row) flattens to an empty row rather than raising.
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


def _rows(envelope: Mapping[str, object]) -> list[dict[str, object]]:
    items = envelope.get("items")
    if not isinstance(items, list):
        return []
    return [_flatten(item) for item in items]


def flatten_collection_rows(envelope: Mapping[str, object]) -> list[dict[str, object]]:
    return _rows(envelope)


def _payload(args: argparse.Namespace, *names: str) -> dict[str, object]:
    payload: dict[str, object] = {}
    for name in names:
        value = getattr(args, name, None)
        if value is not None:
            payload[name] = value
    return payload


async def _list(name: str, args: argparse.Namespace, columns: list[str], *params: str) -> int:
    envelope = await _fetch(name, _payload(args, *params))
    render(_rows(envelope), columns=columns, as_json=args.json)
    return exit_code_for_envelope(envelope)


async def _record(name: str, args: argparse.Namespace, payload: Mapping[str, object]) -> int:
    envelope = await _fetch(name, payload)
    render_record(_flatten(envelope), as_json=args.json)
    return exit_code_for_envelope(envelope)


async def resources_list(args: argparse.Namespace) -> int:
    return await _list("resources.list", args, ["id", "kind", "host"], "kind")


async def resources_describe(args: argparse.Namespace) -> int:
    return await _record("resources.describe", args, {"resource_id": args.resource_id})


async def images_describe(args: argparse.Namespace) -> int:
    payload = {"image_id": args.image_id, **_payload(args, "target_kernel")}
    return await _record("images.describe", args, payload)


async def allocations_list(args: argparse.Namespace) -> int:
    return await _list("allocations.list", args, ["id", "project", "system", "state"], "project")


async def allocations_get(args: argparse.Namespace) -> int:
    return await _record("allocations.get", args, {"allocation_id": args.allocation_id})


async def systems_list(args: argparse.Namespace) -> int:
    envelope = await _fetch("systems.list", {"request": _payload(args, "state")})
    render(_rows(envelope), columns=["id", "project", "state"], as_json=args.json)
    return exit_code_for_envelope(envelope)


async def systems_show(args: argparse.Namespace) -> int:
    return await _record("systems.get", args, {"system_id": args.system_id})


async def runs_show(args: argparse.Namespace) -> int:
    return await _record("runs.get", args, {"run_id": args.run_id})


async def jobs_list(args: argparse.Namespace) -> int:
    return await _list("jobs.list", args, ["id", "kind", "state"])


async def jobs_get(args: argparse.Namespace) -> int:
    return await _record("jobs.get", args, {"job_id": args.job_id})


def _data_list(envelope: Mapping[str, object], key: str) -> list[object]:
    """Return the list a ``data``-shaped read tool puts under ``data[key]``.

    ``secrets.list``/``fixtures.list`` carry their rows in the envelope's ``data`` (not the
    nested ``items`` a collection envelope uses), so they flatten from ``data`` here.
    """
    raw = envelope.get("data")
    if not isinstance(raw, Mapping):
        return []
    data: Mapping[str, object] = {str(k): v for k, v in raw.items()}
    rows = data.get(key)
    return list(rows) if isinstance(rows, list) else []


async def secrets_list(args: argparse.Namespace) -> int:
    """List secret-reference *presence* (keys only; never values). Platform operator-gated."""
    envelope = await _fetch("secrets.list", {})
    refs = [{"ref": str(ref)} for ref in _data_list(envelope, "secrets")]
    render(refs, columns=["ref"], as_json=args.json)
    return exit_code_for_envelope(envelope)


async def fixtures_list(args: argparse.Namespace) -> int:
    """List rootfs fixture catalog entries (provider, name, arch). Requires a valid token."""
    envelope = await _fetch("fixtures.list", {})
    rows = [
        {str(k): v for k, v in row.items()}
        for row in _data_list(envelope, "fixtures")
        if isinstance(row, Mapping)
    ]
    render(rows, columns=["provider", "name", "arch"], as_json=args.json)
    return exit_code_for_envelope(envelope)


async def ledger_show(args: argparse.Namespace) -> int:
    return await _record("accounting.usage_project", args, {"project": args.project})


async def inventory_show(args: argparse.Namespace) -> int:
    return await _list("inventory.list", args, ["key", "backend", "status"], "project")


_REPORT_COLUMNS = ["project", "principal", "reserved", "reconciled", "variance"]
_REPORT_TOTAL_COLUMNS = [
    "scope",
    "group_by",
    "project_count",
    "total_project",
    "total_principal",
    "total_reserved",
    "total_reconciled",
    "total_variance",
]


def _window_payload(args: argparse.Namespace) -> dict[str, object]:
    """Assemble ``{"window": [since, until]}`` from ``--since``/``--until``, or ``{}``.

    Sends no ``window`` key when both bounds are absent (server reports all time). When only
    one bound is given the other half of the pair is ``None`` (a half-open window). Values
    pass through verbatim; the tool's parser owns ISO-8601/timezone validation.
    """
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    if since is None and until is None:
        return {}
    return {"window": [since, until]}


def _projects_arg(args: argparse.Namespace) -> list[str] | None:
    """Comma-split ``--projects`` into a name list, or ``None`` when the flag is absent.

    Whitespace is trimmed and empty tokens dropped. A given-but-all-empty value yields an
    empty list, which the caller rejects as a usage error rather than sending ``projects=[]``.
    """
    raw = getattr(args, "projects", None)
    if raw is None:
        return None
    return [name.strip() for name in raw.split(",") if name.strip()]


def _totals(envelope: Mapping[str, object]) -> dict[str, object]:
    data = envelope.get("data")
    return {str(k): v for k, v in data.items()} if isinstance(data, Mapping) else {}


async def _report(name: str, args: argparse.Namespace, payload: Mapping[str, object]) -> int:
    envelope = await _fetch(name, payload)
    render_report(
        _rows(envelope),
        _totals(envelope),
        columns=_REPORT_COLUMNS,
        total_columns=_REPORT_TOTAL_COLUMNS,
        as_json=args.json,
    )
    return exit_code_for_envelope(envelope)


async def ledger_report_all(args: argparse.Namespace) -> int:
    """Platform-wide accounting rollup (``accounting.report_all_projects``; auditor-gated)."""
    payload = _payload(args, "group_by")
    payload.update(_window_payload(args))
    return await _report("accounting.report_all_projects", args, payload)


async def ledger_report_granted(args: argparse.Namespace) -> int:
    """Granted-project accounting rollup (``accounting.report_granted_set``)."""
    names = _projects_arg(args)
    if names == []:
        print("error: --projects was given but listed no project names", file=sys.stderr)
        return 2
    payload = _payload(args, "group_by")
    if names is not None:
        payload["projects"] = names
    payload.update(_window_payload(args))
    return await _report("accounting.report_granted_set", args, payload)
