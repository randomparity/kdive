"""Prove the read-only operator MCP tool cells over HTTP (#2812, ADR-0722).

``live_stack``-marked. One parameter per contract cell of the eight read-only operator tools
owner group 2812 keeps, framed by :func:`~tests.integration.live_stack.tool_cells.run_tool_cell`
like the #2811 cells. A functional cell compares the tool's answer with the evidence database
(read-only session), the configured ``systems.toml``, the checkout's ``HEAD``, or calls the cell
made itself under a subject and agent session no other cell uses. Every cell works in a fresh
``cov-<hex>`` project whose snapshot must not change; ``inventory.list`` also takes one
allocation in the funded ``KDIVE_PROJECT`` and releases it. ``docs/operating/runbooks/
live-testing.md`` covers the precondition and the run.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import subprocess
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pytest
from psycopg.rows import dict_row

import kdive.config as config
from kdive.config.cli_settings import CLI_CLIENT_ID
from kdive.config.core_settings import SECRETS_ROOT
from kdive.diagnostics.checks import SECRET_REF_ID
from kdive.diagnostics.contracts import WORKER_UNAVAILABLE_DETAIL
from kdive.diagnostics.contributions.multiarch_gdb import (
    DEPMOD_TOOLCHAIN_ID,
    GUEST_ARCH_ACCEL_ID,
    MULTIARCH_GDB_ID,
    PSERIES_FADUMP_ID,
)
from kdive.domain.errors import ErrorCategory
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.path import systems_toml_path
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.security.audit import args_digest
from kdive.security.secrets.secrets import read_secret_file
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Exposure,
    Functional,
    Grants,
    HttpCaller,
    Rejection,
    boundary_of,
    one,
    project_state,
    prove_functional,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

TOOLS = (
    "audit.query",
    "inventory.list",
    "ops.diagnostics",
    "ops.export_cost_classes",
    "ops.export_systems_toml",
    "ops.jobs_list",
    "ops.tool_trail",
    "secrets.list",
)
_AUDITOR_TOOLS = frozenset({"inventory.list", "ops.tool_trail"})
_LOCAL_WORKER_CHECKS = {
    MULTIARCH_GDB_ID,
    PSERIES_FADUMP_ID,
    GUEST_ARCH_ACCEL_ID,
    DEPMOD_TOOLCHAIN_ID,
}
_DENIED = ErrorCategory.AUTHORIZATION_DENIED.value
_SIZING = {"vcpus": 1, "memory_gb": 1, "disk_gb": 1}
_SETTLE_ATTEMPTS = 3
_PAGED_JOBS = 5
Rows = list[dict[str, Any]]


async def _rows(
    db_url: str, query: LiteralString, params: tuple[object, ...] | Mapping[str, object] = ()
) -> Rows:
    """``query``'s rows from a read-only session on the evidence database."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return list(await cursor.fetchall())


def _fresh() -> str:
    return f"cov-{secrets.token_hex(4)}"


def _viewer(project: str) -> Grants:
    return Grants(f"{project}-viewer", (project,), {project: "viewer"})


def _platform(project: str, role: str) -> Grants:
    return Grants(f"{project}-{role}", (project,), {project: "viewer"}, (role,))


def _functional_grants(tool: str, project: str) -> Grants:
    if tool == "audit.query":
        # Project admin for the project form, auditor for the all-projects form.
        return Grants(f"{project}-admin", (project,), {project: "admin"}, ("platform_auditor",))
    if tool in _AUDITOR_TOOLS:
        return _platform(project, "platform_auditor")
    return _platform(project, "platform_operator")


def _when(value: object) -> datetime:
    """A served ISO-8601 timestamp as a datetime, so session time zones cannot differ."""
    assert isinstance(value, str), f"timestamp {value!r} is not a string"
    return datetime.fromisoformat(value)


def _text(value: object) -> str:
    return "" if value is None else str(value)


async def _pages(
    caller: HttpCaller, tool: str, request: Mapping[str, object], token: str, *, most: int = 200
) -> list[ToolResponse]:
    """``tool``'s items for ``request``, one per page; at most ``most`` pages when below 200."""
    items: list[ToolResponse] = []
    cursor: object = None
    for page in range(min(most, 200)):
        args = {**request, "limit": 1, **({"cursor": cursor} if cursor else {})}
        env = one(await caller.call(tool, {"request": args}, token, discover=page == 0))
        items.extend(env.items)
        cursor = env.data.get("next_cursor")
        if not cursor:
            return items
    if most < 200:
        return items
    raise AssertionError(f"{tool} paged past 200 pages")


async def _settled[T](
    observe: Callable[[], Awaitable[T]], read: Callable[[], Awaitable[Rows]]
) -> tuple[T, Rows]:
    """``observe()`` bracketed by two equal database reads, retried while background work moves."""
    for _ in range(_SETTLE_ATTEMPTS):
        before = await read()
        observed = await observe()
        if await read() == before:
            return observed, before
    raise AssertionError(f"the database changed across {_SETTLE_ATTEMPTS} observations")


_AUDIT_ROWS: LiteralString = (
    "SELECT ts, principal, agent_session, project, tool, object_kind, object_id, transition "
    "FROM audit_log WHERE principal = %s ORDER BY ts DESC, id DESC"
)


def _audit_view(data: Mapping[str, object]) -> dict[str, object]:
    return {**data, "ts": _when(data["ts"])}


def _audit_row(row: Mapping[str, object]) -> dict[str, object]:
    return {k: (row[k] if k == "ts" else _text(row[k])) for k in row}


async def _audit(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    project, other = grants.projects[0], _fresh()
    actor = HttpCaller("direct", caller.base_url, caller.issuer)
    member = Grants(f"{project}-member", (project, other), {project: "viewer", other: "viewer"})
    member_token = actor.token(member)
    # A viewer reading a project's audit trail is a rank-below denial, which the denial-audit
    # boundary records: the known attributed operations this cell reads back.
    for target, limit in ((project, 1), (project, 2), (other, 1)):
        request = {"scope": "project", "project": target, "limit": limit}
        denied = await actor.call("audit.query", {"request": request}, member_token)
        assert isinstance(denied, ToolResponse) and denied.error_category == _DENIED, denied
    rows = [_audit_row(r) for r in await _rows(db_url, _AUDIT_ROWS, (member.subject,))]
    assert [r["project"] for r in rows] == [other, project, project], rows
    assert {r["transition"] for r in rows} == {"denied"} and {r["tool"] for r in rows} == {
        "audit.query"
    }
    token = caller.token(grants)
    scoped = await _pages(
        caller,
        "audit.query",
        {"scope": "project", "project": project, "principal": member.subject},
        token,
    )
    assert [_audit_view(i.data) for i in scoped] == [r for r in rows if r["project"] == project]
    everywhere = await _pages(
        caller, "audit.query", {"scope": "all-projects", "principal": member.subject}, token
    )
    assert [_audit_view(i.data) for i in everywhere] == rows
    return {"principal_rows": len(rows), "project_rows": len(scoped), "projects": 2}


def _funded_project() -> str:
    """The project the bring-up onboarded with budget and quota (``KDIVE_PROJECT``)."""
    project = os.environ.get("KDIVE_PROJECT")
    assert project, "KDIVE_PROJECT is unset; source examples/local-libvirt/env.sh"
    return project


_ALLOCATION: LiteralString = "SELECT * FROM allocations WHERE id = %s"


async def _release(caller: HttpCaller, token: str, db_url: str, allocation_id: str) -> str | None:
    """Release ``allocation_id``; what kept it from reaching ``released``, if anything."""
    result = await caller.call("allocations.release", {"allocation_id": allocation_id}, token)
    state = (await _rows(db_url, _ALLOCATION, (allocation_id,)))[0]["state"]
    if isinstance(result, ToolResponse) and result.status == "released" and state == "released":
        return None
    return f"release of {allocation_id} answered {getattr(result, 'detail', result)}, {state}"


@asynccontextmanager
async def _granted(caller: HttpCaller, db_url: str) -> AsyncIterator[dict[str, Any]]:
    """A granted allocation in the funded project, released and checked on exit."""
    funded = _funded_project()
    operator = HttpCaller("direct", caller.base_url, caller.issuer)
    token = operator.token(Grants(f"{funded}-{_fresh()}", (funded,), {funded: "contributor"}))
    env = one(await operator.call("allocations.request", {"project": funded, **_SIZING}, token))
    assert env.status == "granted", f"allocations.request answered {env.status}"
    try:
        yield (await _rows(db_url, _ALLOCATION, (env.object_id,)))[0]
    except BaseException:
        await _release(operator, token, db_url, env.object_id)
        raise
    problem = await _release(operator, token, db_url, env.object_id)
    assert problem is None, problem


_ALLOCATIONS: LiteralString = (
    "SELECT id, resource_id, project, principal, state, lease_expiry FROM allocations "
    "WHERE (%(project)s::text IS NULL OR project = %(project)s) "
    "AND (%(resource)s::uuid IS NULL OR resource_id = %(resource)s) "
    "ORDER BY created_at DESC, id DESC LIMIT 200"
)
_SYSTEMS: LiteralString = (
    "SELECT s.id, s.allocation_id, a.resource_id, r.kind AS resource_kind, s.project, "
    "s.principal, s.state, s.domain_name FROM systems s "
    "JOIN allocations a ON a.id = s.allocation_id JOIN resources r ON r.id = a.resource_id "
    "WHERE (%(project)s::text IS NULL OR s.project = %(project)s) "
    "AND (%(resource)s::uuid IS NULL OR a.resource_id = %(resource)s) "
    "ORDER BY s.created_at DESC, s.id DESC LIMIT 200"
)


def _inventory_view(data: Mapping[str, object]) -> dict[str, object]:
    expiry = data.get("lease_expiry")
    return {**data, "lease_expiry": _when(expiry)} if expiry is not None else dict(data)


def _inventory_row(kind: str, row: Mapping[str, object]) -> dict[str, object]:
    view: dict[str, object] = {
        k: (None if row[k] is None else str(row[k])) for k in row if k != "lease_expiry"
    }
    if kind == "allocation":
        view["lease_expiry"] = row["lease_expiry"]
    return {"kind": kind, **view}


async def _inventory_expected(db_url: str, filters: Mapping[str, object]) -> list[object]:
    allocations = await _rows(db_url, _ALLOCATIONS, filters)
    systems = await _rows(db_url, _SYSTEMS, filters)
    return [
        *(_inventory_row("allocation", r) for r in allocations),
        *(_inventory_row("system", r) for r in systems),
    ]


async def _inventory(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    token = caller.token(grants)
    funded = _funded_project()
    assert funded not in grants.projects, "the auditor must read a project it does not hold"
    async with _granted(caller, db_url) as allocation:
        allocation_id, resource = str(allocation["id"]), str(allocation["resource_id"])
        observed: dict[str, object] = {}
        for request, filters in (
            ({"project": funded}, {"project": funded, "resource": None}),
            ({"resource_id": resource}, {"project": None, "resource": resource}),
        ):
            args = {"request": {**request, "limit": 200}}
            env = one(await caller.call("inventory.list", args, token, discover=True))
            listed = [_inventory_view(item.data) for item in env.items]
            assert listed == await _inventory_expected(db_url, filters), f"{request} differs"
            held = [i for i in listed if i["kind"] == "allocation" and i["id"] == allocation_id]
            assert held and held[0]["state"] == "granted", f"{request} omits the allocation"
            observed[next(iter(request))] = len(listed)
        page = one(await caller.call("inventory.list", {"request": {"limit": 1}}, token))
        everything = await _inventory_expected(db_url, {"project": None, "resource": None})
        kinds = [cast(dict[str, object], row)["kind"] for row in everything]
        assert page.data["allocation_count"] == 1
        more = kinds.count("allocation") > 1 or kinds.count("system") > 1
        assert page.data["truncated"] == more, f"truncated is not {more}"
    return {**observed, "allocation": allocation_id, "owned": [allocation_id]}


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


async def _diagnose(caller: HttpCaller, token: str) -> ToolResponse:
    env = one(await caller.call("ops.diagnostics", {}, token, discover=True))
    checks = {str(item.data["check"]): item.data for item in env.items}
    remote = sorted(k for k, v in checks.items() if v.get("provider") == "remote-libvirt")
    if remote:
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"remote-libvirt diagnostics {remote} are configured; the cells cover local lanes",
        )
    return env


async def _diagnostics(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    token = caller.token(grants)
    env = await _diagnose(caller, token)
    checks = {str(item.data["check"]): item.data for item in env.items}
    assert set(checks) == {SECRET_REF_ID, *_LOCAL_WORKER_CHECKS}, sorted(checks)
    assert checks[SECRET_REF_ID]["status"] == "pass", checks[SECRET_REF_ID]
    assert all(checks[c]["detail"] != WORKER_UNAVAILABLE_DETAIL for c in _LOCAL_WORKER_CHECKS)
    statuses = [str(v["status"]) for v in checks.values()]
    assert env.data["has_failure"] == ("fail" in statuses)
    assert env.data["has_error"] == ("error" in statuses)
    version = cast(dict[str, object], env.data["service_version"])
    assert _head().startswith(str(version["commit"])), f"served commit {version['commit']}"
    # No local contribution supplies the probe-guest seam: the deliberately unavailable one.
    egress = one(await caller.call("ops.diagnostics", {"with_egress": True}, token))
    assert [i.data["check"] for i in egress.items] == ["diagnostics"], egress.items
    item = egress.items[0].data
    assert item["status"] == "error" and "could not be assembled" in str(item["detail"])
    return {"checks": dict(sorted((k, v["status"]) for k, v in checks.items())), "egress": "error"}


_COST_CLASSES: LiteralString = (
    "SELECT cost_class, coeff FROM cost_class_coefficients ORDER BY cost_class"
)


async def _cost_classes(db_url: str) -> list[dict[str, str]]:
    return [
        {"name": r["cost_class"], "coeff": str(r["coeff"])}
        for r in await _rows(db_url, _COST_CLASSES)
    ]


async def _export_cost(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    env = one(await caller.call("ops.export_cost_classes", {}, caller.token(grants), discover=True))
    exported = tomllib.loads(str(env.data["toml"])).get("cost_class", [])
    assert exported == await _cost_classes(db_url), "export differs from the coefficient table"
    return {"cost_classes": len(exported)}


async def _export_systems(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    # Never `persist`: the server writes through its own writeback setting, unseen from here.
    env = one(await caller.call("ops.export_systems_toml", {}, caller.token(grants), discover=True))
    exported = tomllib.loads(str(env.data["toml"]))
    assert exported.get("cost_class", []) == await _cost_classes(db_url)
    declared = load_inventory_optional(systems_toml_path())
    assert declared is not None, "the lane's systems.toml is absent; export KDIVE_SYSTEMS_TOML"
    for section, names in (
        ("local_libvirt", {h.name for h in declared.local_libvirt}),
        ("image", {i.name for i in declared.image}),
    ):
        listed = {entry["name"] for entry in exported.get(section, [])}
        assert names <= listed, f"{section} {sorted(names - listed)} declared but not exported"
    return {"declared_hosts": len(declared.local_libvirt), "declared_images": len(declared.image)}


_JOBS: LiteralString = (
    "SELECT id, kind, state, authorizing->>'project' AS project, attempt, worker_id FROM jobs "
    "WHERE (%(states)s::text[] IS NULL OR state = ANY(%(states)s)) "
    "ORDER BY created_at DESC, id DESC LIMIT %(limit)s"
)
_DEPTH: LiteralString = "SELECT state, count(*) AS n FROM jobs GROUP BY state ORDER BY state"


def _job_view(item: ToolResponse) -> dict[str, object]:
    return {"id": item.object_id, **item.data}


def _job_row(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "state": row["state"],
        "project": row["project"],
        "attempt": str(row["attempt"]),
        "worker_id": row["worker_id"] or "",
    }


async def _jobs(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    token = caller.token(grants)
    await _diagnose(caller, token)
    known = (
        await _rows(
            db_url,
            "SELECT id, state FROM jobs WHERE kind = 'diagnostics_worker_check' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
        )
    )[0]

    async def database() -> Rows:
        rows = await _rows(db_url, _JOBS, {"states": None, "limit": 200})
        return [*await _rows(db_url, _DEPTH), *rows]

    async def listing() -> ToolResponse:
        return one(
            await caller.call("ops.jobs_list", {"request": {"limit": 200}}, token, discover=True)
        )

    env, rows = await _settled(listing, database)
    depth = {f"depth_{r['state']}": r["n"] for r in rows if "n" in r}
    assert {k: v for k, v in env.data.items() if k.startswith("depth_")} == depth
    expected = [_job_row(r) for r in rows if "n" not in r]
    assert [_job_view(i) for i in env.items] == expected, "job rows differ from the database"
    mine = [r for r in expected if r["id"] == str(known["id"])]
    assert mine and mine[0]["project"] not in grants.projects, "the known job is not cross-project"
    state = str(known["state"])

    async def state_rows() -> Rows:
        return await _rows(db_url, _JOBS, {"states": [state], "limit": _PAGED_JOBS})

    async def paged() -> list[ToolResponse]:
        return await _pages(caller, "ops.jobs_list", {"states": [state]}, token, most=_PAGED_JOBS)

    pages, rows = await _settled(paged, state_rows)
    assert [_job_view(i) for i in pages] == [_job_row(r) for r in rows], f"{state} pages differ"
    return {"jobs": len(expected), "known_job": mine[0]["kind"], "paged": len(pages)}


_TRAIL: LiteralString = (
    "SELECT ts, principal, agent_session, project, tool, outcome, actor, client_id, args_digest "
    "FROM tool_invocation WHERE agent_session = %s ORDER BY ts DESC, id DESC"
)


async def _trail(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    actor = HttpCaller("direct", caller.base_url, caller.issuer)
    known = _viewer(f"{grants.projects[0]}-{secrets.token_hex(2)}")
    token = actor.token(known)
    one(await actor.call("projects.list", {}, token))
    one(await actor.call("session.whoami", {}, token))
    denied = await actor.call("inventory.list", {}, token)
    assert isinstance(denied, ToolResponse) and denied.error_category == _DENIED, denied
    session = f"{known.subject}-sess"
    rows = [
        {k: (row[k] if k == "ts" else _text(row[k])) for k in row}
        for row in await _rows(db_url, _TRAIL, (session,))
    ]
    assert [(r["tool"], r["outcome"]) for r in rows] == [
        ("inventory.list", "denied"),
        ("session.whoami", "ok"),
        ("projects.list", "ok"),
    ], rows
    assert {(r["principal"], r["actor"], r["client_id"], r["args_digest"]) for r in rows} == {
        (known.subject, "operator-cli", config.require(CLI_CLIENT_ID), args_digest({}))
    }
    pages = await _pages(caller, "ops.tool_trail", {"agent_session": session}, caller.token(grants))
    assert [{**i.data, "ts": _when(i.data["ts"])} for i in pages] == rows
    return {"calls": len(rows), "paged": len(pages)}


def _lane_secrets() -> set[str]:
    """The lane's secret setting values, and the content of each that names a secret file."""
    root = Path(config.require(SECRETS_ROOT))
    found: set[str] = set()
    for setting in config.all_settings():
        value = config.get(setting) if setting.secret else None
        if not value:
            continue
        found.add(value)
        # A value that is no reference under the root is itself the secret.
        with contextlib.suppress(OSError, ValueError):
            found.add(read_secret_file(root, value))
    return {v for v in found if v.strip()}


async def _secrets(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    env = one(await caller.call("secrets.list", {}, caller.token(grants), discover=True))
    labels = cast(list[str], env.data["secrets"])
    assert labels == sorted(set(labels)), f"labels are not sorted and unique: {labels}"
    # A string scope comes only from a remote-libvirt artifact channel, absent on local lanes.
    assert set(labels) <= {"<process-global>", "<scoped>"}, f"unexpected labels {labels}"
    served = env.model_dump_json()
    assert not [v for v in _lane_secrets() if v in served], "a configured secret was served"
    raise ScenarioStop(
        Outcome.BLOCKED,
        f"secrets.list served {labels} with no secret leaked, but presence has no positive "
        "control: no local-lane call makes the server register a secret",
    )


_FUNCTIONAL: dict[str, Callable[..., Awaitable[dict[str, object]]]] = {
    "audit.query": _audit,
    "inventory.list": _inventory,
    "ops.diagnostics": _diagnostics,
    "ops.export_cost_classes": _export_cost,
    "ops.export_systems_toml": _export_systems,
    "ops.jobs_list": _jobs,
    "ops.tool_trail": _trail,
    "secrets.list": _secrets,
}


def _valid(tool: str, project: str) -> dict[str, object]:
    """Arguments that change nothing for the cell's issued-token control call."""
    if tool == "audit.query":
        return {"request": {"scope": "project", "project": project}}
    return {}


def _invalid(tool: str, project: str) -> dict[str, object]:
    """Schema-invalid arguments: a mistyped field or an unknown discriminator."""
    table: dict[str, dict[str, object]] = {
        "audit.query": {"request": {"scope": "galaxy"}},
        "ops.diagnostics": {"with_egress": "maybe"},
        "ops.export_systems_toml": {"persist": "maybe"},
    }
    return table.get(tool, {"request": {"limit": "many"}})


def _rejection_grants(tool: str, boundary: Boundary, project: str) -> Grants:
    if boundary == "validation":
        # tools.invoke reports field_errors only for a tool the token can see (ADR-0722 §3).
        return _functional_grants(tool, project)
    if boundary == "project-isolation":
        other = _fresh()
        return Grants(f"{other}-admin", (other,), {other: "admin"})
    if boundary == "authorization" and tool != "audit.query" and tool not in _AUDITOR_TOOLS:
        return _platform(project, "platform_auditor")
    return _viewer(project)


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    cell = run.cell
    caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
    project = _fresh()
    snapshot = partial(project_state, db_url, project)
    if cell.kind == "functional":
        body = cast(Functional, partial(_FUNCTIONAL[cell.operation], db_url=db_url))
        grants = _functional_grants(cell.operation, project)
        await prove_functional(run, caller, grants, body, snapshot)
        return
    boundary = boundary_of(cell)
    args = _invalid if boundary == "validation" else _valid
    rejection = Rejection(
        args(cell.operation, project), _rejection_grants(cell.operation, boundary, project)
    )
    await prove_rejection(run, caller, boundary, rejection, snapshot)


@pytest.mark.parametrize("cell", tool_cells(TOOLS), ids=lambda cell: cell.id)
def test_operator_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a read-only operator tool."""
    run_tool_cell(cell, _scenario)
