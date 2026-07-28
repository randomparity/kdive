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
import math
import sys
from collections.abc import Mapping

from kdive.cli.errors import exit_code_for_envelope
from kdive.cli.render import emit, flatten_envelope, render, render_record, render_report
from kdive.cli.transport import Session, tool_envelope


def _session_factory() -> Session:
    return Session.from_env()


async def _fetch(name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
    session = _session_factory()
    async with session.client() as client:
        result = await client.call_tool(name, dict(arguments))
    return tool_envelope(result)


def collection_rows(envelope: Mapping[str, object]) -> list[dict[str, object]]:
    """Flatten a collection envelope's ``items`` into rows (shared with the images verbs)."""
    items = envelope.get("items")
    if not isinstance(items, list):
        return []
    return [flatten_envelope(item) for item in items]


async def fetch_collection_envelope(
    name: str, arguments: Mapping[str, object]
) -> Mapping[str, object]:
    """Fetch one collection-shaped read tool and return its whole response envelope."""
    return await _fetch(name, arguments)


def _payload(args: argparse.Namespace, *names: str) -> dict[str, object]:
    payload: dict[str, object] = {}
    for name in names:
        value = getattr(args, name, None)
        if value is not None:
            payload[name] = value
    return payload


async def _list(
    name: str, args: argparse.Namespace, columns: list[str], payload: Mapping[str, object]
) -> int:
    envelope = await _fetch(name, payload)
    emit(envelope, lambda: render(collection_rows(envelope), columns=columns), as_json=args.json)
    return exit_code_for_envelope(envelope)


async def _record(name: str, args: argparse.Namespace, payload: Mapping[str, object]) -> int:
    envelope = await _fetch(name, payload)
    emit(envelope, lambda: render_record(flatten_envelope(envelope)), as_json=args.json)
    return exit_code_for_envelope(envelope)


async def resources_list(args: argparse.Namespace) -> int:
    request = _payload(args, "kind")
    envelope = await _fetch("resources.list", {"request": request} if request else {})
    emit(
        envelope,
        lambda: render(collection_rows(envelope), columns=["id", "kind", "host"]),
        as_json=args.json,
    )
    return exit_code_for_envelope(envelope)


async def resources_get(args: argparse.Namespace) -> int:
    return await _record("resources.describe", args, {"resource_id": args.resource_id})


async def images_get(args: argparse.Namespace) -> int:
    payload = {"image_id": args.image_id, **_payload(args, "target_kernel")}
    return await _record("images.describe", args, payload)


async def allocations_list(args: argparse.Namespace) -> int:
    envelope = await _fetch("allocations.list", {"request": _payload(args, "project")})
    emit(
        envelope,
        lambda: render(collection_rows(envelope), columns=["id", "project", "system", "state"]),
        as_json=args.json,
    )
    return exit_code_for_envelope(envelope)


async def systems_list(args: argparse.Namespace) -> int:
    envelope = await _fetch("systems.list", {"request": _payload(args, "state")})
    emit(
        envelope,
        lambda: render(collection_rows(envelope), columns=["id", "project", "state"]),
        as_json=args.json,
    )
    return exit_code_for_envelope(envelope)


async def systems_get(args: argparse.Namespace) -> int:
    return await _record("systems.get", args, {"system_id": args.system_id})


async def runs_get(args: argparse.Namespace) -> int:
    return await _record("runs.get", args, {"run_id": args.run_id})


async def jobs_list(args: argparse.Namespace) -> int:
    envelope = await _fetch("jobs.list", {"request": {}})
    emit(
        envelope,
        lambda: render(collection_rows(envelope), columns=["id", "kind", "state"]),
        as_json=args.json,
    )
    return exit_code_for_envelope(envelope)


async def _wait(tool: str, args: argparse.Namespace, id_key: str, object_id: str) -> int:
    """Read or poll one record through a ``wait`` tool, rendering the envelope it returns.

    An omitted ``--timeout-s`` sends no ``timeout_s`` key, leaving the tool's own 30-second
    default authoritative rather than restating it here — so a bare ``wait <id>`` waits, and the
    point read stays the explicit ``--timeout-s 0`` (ADR-0470 decision 2, upholding ADR-0468
    decision 2 on the CLI surface).

    A given value is coerced to ``float`` because curated options are declared with no
    ``type=``, so argparse hands over the raw string while both tools declare ``timeout_s`` as a
    JSON ``number``. A value that is not a finite number is a usage error (exit 2). The
    finiteness check is not redundant with the tools' own ``math.isfinite`` guard: ``float()``
    accepts ``inf``/``nan``, and JSON has no encoding for either, so pydantic serializes them
    to ``null`` on the way out — the tool would receive ``null`` for a declared ``number`` and
    the transport would raise before its ``configuration_error`` could ever be returned.
    """
    payload: dict[str, object] = {id_key: object_id}
    raw = getattr(args, "timeout_s", None)
    if raw is not None:
        try:
            timeout = float(raw)
        except ValueError:
            timeout = math.nan
        if not math.isfinite(timeout):
            print(f"error: --timeout-s must be a finite number, not {raw!r}", file=sys.stderr)
            return 2
        payload["timeout_s"] = timeout
    return await _record(tool, args, payload)


async def jobs_wait(args: argparse.Namespace) -> int:
    """Read or poll one job (``jobs.wait``); ``--timeout-s 0`` is the point read."""
    return await _wait("jobs.wait", args, "job_id", args.job_id)


async def allocations_wait(args: argparse.Namespace) -> int:
    """Read or poll one allocation (``allocations.wait``); ``--timeout-s 0`` is the point read."""
    return await _wait("allocations.wait", args, "allocation_id", args.allocation_id)


def _data_list(envelope: Mapping[str, object], key: str) -> list[object]:
    """Return the list a ``data``-shaped read tool puts under ``data[key]``.

    ``secrets.list`` carries its rows in the envelope's ``data`` (not the nested ``items`` a
    collection envelope uses), so it flattens from ``data`` here.
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
    emit(envelope, lambda: render(refs, columns=["ref"]), as_json=args.json)
    return exit_code_for_envelope(envelope)


async def ledger_get(args: argparse.Namespace) -> int:
    """Spend rollup for one project or one investigation (``accounting.usage``).

    The merged tool discriminates on ``target.kind``, so exactly one of ``--project`` /
    ``--investigation-id`` must be given; neither or both is a usage error (exit 2) rather
    than an opaque server-side discriminator failure.
    """
    project = getattr(args, "project", None)
    investigation_id = getattr(args, "investigation_id", None)
    if (project is None) == (investigation_id is None):
        print("error: give exactly one of --project or --investigation-id", file=sys.stderr)
        return 2
    target: dict[str, object] = (
        {"kind": "project", "project": project}
        if project is not None
        else {"kind": "investigation", "investigation_id": investigation_id}
    )
    return await _record("accounting.usage", args, {"target": target})


async def inventory_show(args: argparse.Namespace) -> int:
    """Cross-project inventory summary, optionally narrowed by ``--project``.

    ``inventory.list`` takes its filters inside a ``request`` wrapper, like the other list
    tools, so ``--project`` goes there rather than at the top level of the payload.
    """
    request = {"request": _payload(args, "project")}
    return await _list("inventory.list", args, ["key", "backend", "status"], request)


_GRANTED_SET_SCOPE = "granted-set"
_REPORT_SCOPES = (_GRANTED_SET_SCOPE, "all-projects")
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
    emit(
        envelope,
        lambda: render_report(
            collection_rows(envelope),
            _totals(envelope),
            columns=_REPORT_COLUMNS,
            total_columns=_REPORT_TOTAL_COLUMNS,
        ),
        as_json=args.json,
    )
    return exit_code_for_envelope(envelope)


async def ledger_report(args: argparse.Namespace) -> int:
    """Accounting rollup over your granted projects or every project (``accounting.report``).

    ``--scope granted-set`` rolls up the caller's own projects and accepts ``--projects``;
    ``--scope all-projects`` rolls up the whole platform and is denied server-side without
    a ``platform_auditor`` token. Both send the scope in the discriminated ``request``, so
    the CLI never picks a scope on the caller's behalf.
    """
    scope = args.scope
    if scope not in _REPORT_SCOPES:
        print(f"error: --scope must be one of {', '.join(_REPORT_SCOPES)}", file=sys.stderr)
        return 2
    names = _projects_arg(args)
    if names == []:
        print("error: --projects was given but listed no project names", file=sys.stderr)
        return 2
    if names is not None and scope != _GRANTED_SET_SCOPE:
        print(f"error: --projects is only valid with --scope {_GRANTED_SET_SCOPE}", file=sys.stderr)
        return 2
    request: dict[str, object] = {"scope": scope, **_payload(args, "group_by")}
    if names is not None:
        request["projects"] = names
    request.update(_window_payload(args))
    return await _report("accounting.report", args, {"request": request})
