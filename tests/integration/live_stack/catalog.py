"""Catalog carrier support: independent SQL evidence and owned availability fixtures."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, LiteralString, cast

import psycopg
from psycopg.rows import dict_row

from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.scenario import ScenarioStop
from tests.integration.live_stack.tool_cells import Grants, HttpCaller, one

Rows = list[dict[str, Any]]


_OCCUPYING = ["granted", "active", "releasing"]


_REQUESTED = "requested"


async def _rows(db_url: str, query: LiteralString, params: tuple[object, ...] = ()) -> Rows:
    """``query``'s rows from a read-only session on the evidence database."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return list(await cursor.fetchall())


Removal = Callable[[], Awaitable[list[str]]]


async def _attempt(remove: Removal) -> list[str]:
    try:
        return await remove()
    except Exception as exc:  # noqa: BLE001 - reported as a cleanup problem, never raised over the cell
        return [repr(exc)]


@asynccontextmanager
async def _cleaning(remove: Removal, what: str) -> AsyncIterator[None]:
    """Run ``remove`` on exit; a failed body keeps its error and gains the cleanup problems."""
    try:
        yield
    except BaseException as exc:
        problems = await _attempt(remove)
        if problems:
            exc.add_note(f"cleanup of {what} also failed: {problems}")
        raise
    problems = await _attempt(remove)
    assert not problems, f"cleanup of {what} failed: {problems}"


_SHAPES: LiteralString = (
    "SELECT name, vcpus, memory_mb, disk_gb, pcie_match FROM system_shapes ORDER BY name"
)


async def _visible_resources(db_url: str, projects: tuple[str, ...]) -> Rows:
    """The ``resources`` rows any of ``projects`` may see: global, owned or allow-listed."""
    rows = await _rows(db_url, "SELECT * FROM resources ORDER BY created_at, id")
    return [
        row
        for row in rows
        if row["owner_project"] is None
        or row["owner_project"] in projects
        or set(row["affinity_allowlist"] or []) & set(projects)
    ]


def _cap(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def _availability_expected(db_url: str, projects: tuple[str, ...]) -> dict[str, object]:
    """Per-host availability and queue depth derived from the database alone."""
    occupancy = {
        str(r["resource_id"]): int(r["n"])
        for r in await _rows(
            db_url,
            "SELECT resource_id, count(*) AS n FROM allocations "
            "WHERE resource_id IS NOT NULL AND state = ANY(%s) GROUP BY resource_id",
            (_OCCUPYING,),
        )
    }
    groups = await _rows(
        db_url,
        "SELECT requested_kind, count(*) AS n FROM allocations WHERE state = %s "
        "GROUP BY requested_kind",
        (_REQUESTED,),
    )
    queued = {
        "total": sum(int(row["n"]) for row in groups),
        "by_kind": {
            str(row["requested_kind"]): int(row["n"])
            for row in groups
            if row["requested_kind"] is not None
        },
        "by_id": sum(int(row["n"]) for row in groups if row["requested_kind"] is None),
    }
    shapes = await _rows(db_url, _SHAPES)
    hosts: dict[str, dict[str, object]] = {}
    for row in await _visible_resources(db_url, projects):
        caps = cast(dict[str, object], row["capabilities"])
        cap, vcpus, memory = (
            _cap(caps.get(k)) for k in ("concurrent_allocation_cap", "vcpus", "memory_mb")
        )
        schedulable = (
            cap is not None
            and vcpus is not None
            and memory is not None
            and row["status"] == "available"
            and not row["cordoned"]
        )
        in_use = occupancy.get(str(row["id"]), 0)
        headroom = max(cap - in_use, 0) if cap is not None else 0
        fits = {
            str(s["name"])
            for s in shapes
            if schedulable
            and headroom >= 1
            and s["pcie_match"] is None
            and s["vcpus"] <= cast(int, vcpus)
            and s["memory_mb"] <= cast(int, memory)
        }
        hosts[str(row["id"])] = {
            "schedulable": schedulable,
            "cap": cap if cap is not None else 0,
            "in_use": in_use,
            "headroom": headroom,
            "fits": fits,
            "vcpus": vcpus,
            "memory_mb": memory,
        }
    pcie = {str(s["name"]) for s in shapes if s["pcie_match"] is not None}
    return {"hosts": hosts, "queued": queued, "pcie_shapes": pcie}


def _funded_project() -> str:
    """The project the bring-up onboarded with budget and quota (``KDIVE_PROJECT``)."""
    project = os.environ.get("KDIVE_PROJECT")
    assert project, "KDIVE_PROJECT is unset; source examples/local-libvirt/env.sh"
    return project


async def _allocation(db_url: str, allocation_id: str) -> dict[str, Any]:
    rows = await _rows(db_url, "SELECT * FROM allocations WHERE id = %s", (allocation_id,))
    assert len(rows) == 1, f"allocation {allocation_id} has {len(rows)} rows"
    return rows[0]


async def _release(caller: HttpCaller, token: str, db_url: str, allocation_id: str) -> list[str]:
    """Release ``allocation_id``; what kept it from reaching ``released``."""
    result = await caller.call("allocations.release", {"allocation_id": allocation_id}, token)
    state = (await _allocation(db_url, allocation_id))["state"]
    if isinstance(result, ToolResponse) and result.status == "released" and state == "released":
        return []
    return [f"release of {allocation_id} answered {getattr(result, 'detail', result)}, {state}"]


async def _compare_availability(
    caller: HttpCaller, token: str, db_url: str, projects: tuple[str, ...]
) -> dict[str, object]:
    """One ``resources.availability`` read compared with the database; the expectation."""
    env = one(await caller.call("resources.availability", {}, token, discover=True))
    expected = await _availability_expected(db_url, projects)
    hosts = cast(dict[str, dict[str, Any]], expected["hosts"])
    pcie = cast(set[str], expected["pcie_shapes"])
    fields = ("schedulable", "cap", "in_use", "headroom")
    observed = {
        item.object_id: {
            **{k: item.data.get(k) for k in fields},
            "fits": set(cast(list[str], item.data.get("fits"))) - pcie,
        }
        for item in env.items
    }
    wanted = {rid: {**{k: h[k] for k in fields}, "fits": h["fits"]} for rid, h in hosts.items()}
    assert observed == wanted, f"availability {observed} != database {wanted}"
    queue = cast(dict[str, object], env.data.get("queue_depth"))
    assert queue == expected["queued"], f"queue depth {queue} != {expected['queued']}"
    union = set().union(*(h["fits"] for h in hosts.values()))
    assert set(cast(list[str], env.data.get("fits_now"))) - pcie == union
    return expected


async def _queue_host(db_url: str, project: str) -> str:
    """One empty cap-one local host makes both queued selectors deterministic."""
    resources = await _visible_resources(db_url, (project,))
    eligible = [
        r
        for r in resources
        if r["kind"] == "local-libvirt" and r["status"] == "available" and not r["cordoned"]
    ]
    occupied = await _rows(
        db_url, "SELECT * FROM allocations WHERE state = ANY(%s)", (_OCCUPYING + [_REQUESTED],)
    )
    if len(eligible) != 1 or occupied:
        raise ScenarioStop(
            Outcome.BLOCKED, "queue proof needs one available local host and idle fleet"
        )
    caps = eligible[0]["capabilities"]
    if (
        caps.get("concurrent_allocation_cap") != 1
        or caps.get("vcpus", 0) < 1
        or caps.get("memory_mb", 0) < 1024
        or caps.get("disk_gb", 0) < 1
    ):
        raise ScenarioStop(
            Outcome.BLOCKED, "queue proof needs cap=1 and room for 1 vCPU/1 GiB/1 GB"
        )
    return str(eligible[0]["id"])


@asynccontextmanager
async def _queued_allocations(
    caller: HttpCaller, db_url: str, host: str
) -> AsyncIterator[list[str]]:
    """Hold one grant and both queue selector forms; withdraw before freeing capacity."""
    project = _funded_project()
    quota_rows = await _rows(db_url, "SELECT * FROM quotas WHERE project = %s", (project,))
    fields = ("max_concurrent_allocations", "max_concurrent_systems", "max_pending_allocations")
    if not quota_rows or quota_rows[0][fields[0]] < 1:
        raise ScenarioStop(Outcome.BLOCKED, "queue proof needs an existing funded grant quota")
    original = {"project": project, **{k: quota_rows[0][k] for k in fields}}
    subject = f"cov-queue-{secrets.token_hex(8)}"
    operator = HttpCaller("direct", caller.base_url, caller.issuer)
    token = operator.token(Grants(subject, (project,), {project: "admin"}))
    indeterminate = False
    owned_ids: list[str] = []

    async def owned() -> Rows:
        return await _rows(
            db_url,
            "SELECT * FROM allocations WHERE project = %s AND principal = %s",
            (project, subject),
        )

    async def quota(values: dict[str, Any]) -> list[str]:
        one(await operator.call("accounting.set_quota", values, token))
        rows = await _rows(db_url, "SELECT * FROM quotas WHERE project = %s", (project,))
        assert rows and {k: rows[0][k] for k in fields} == {k: values[k] for k in fields}
        return []

    async def remove() -> list[str]:
        problems: list[str] = []
        try:
            rows = await owned()
            owned_ids[:] = sorted(set(owned_ids) | {str(r["id"]) for r in rows})
            for row in rows:
                if row["state"] == _REQUESTED:
                    problems.extend(
                        await _attempt(partial(_release, operator, token, db_url, str(row["id"])))
                    )
            rows = await owned()
            pending = [str(r["id"]) for r in rows if r["state"] == _REQUESTED]
            if indeterminate or pending:
                problems.append(
                    f"indeterminate={indeterminate}; pending={pending}; retain blockers"
                )
            else:
                for row in rows:
                    if row["state"] == "granted":
                        problems.extend(
                            await _attempt(
                                partial(_release, operator, token, db_url, str(row["id"]))
                            )
                        )
                remaining = [str(r["id"]) for r in await owned() if r["state"] != "released"]
                if remaining:
                    problems.append(f"unreleased owned allocations: {remaining}")
        except Exception as exc:  # noqa: BLE001 - quota restoration must still be attempted
            problems.append(repr(exc))
        problems.extend(await _attempt(partial(quota, original)))
        if problems:
            problems.append(
                f"reconcile project={project} subject={subject} known IDs={owned_ids}; "
                f"withdraw queued IDs before releasing blockers, restore caps={original}"
            )
        return problems

    async with _cleaning(remove, f"queue fixture {subject}"):
        await quota({**original, "max_pending_allocations": max(original[fields[2]], 2)})
        selectors = [
            {"mode": "id", "resource_id": host},
            {"mode": "kind", "kind": "local-libvirt"},
            {"mode": "id", "resource_id": host},
        ]
        for index, selector in enumerate(selectors):
            indeterminate = True
            result = await operator.call(
                "allocations.request",
                {
                    "project": project,
                    "vcpus": 1,
                    "memory_gb": 1,
                    "disk_gb": 1,
                    "resource": selector,
                    "on_capacity": "queue" if index else "deny",
                },
                token,
            )
            indeterminate = False
            env = one(result)
            owned_ids.append(env.object_id)
            expected = _REQUESTED if index else "granted"
            assert env.status == expected, f"allocation {env.object_id}: {env.status} != {expected}"
            row = await _allocation(db_url, env.object_id)
            assert row["state"] == expected
            if index:
                assert row["requested_kind"] == selector.get("kind")
                assert str(row["requested_resource_id"]) == str(selector.get("resource_id"))
        yield owned_ids


async def _availability(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    """Prove occupancy and both positive queue dimensions, then their restoration."""
    token = caller.token(grants)
    idle = await _compare_availability(caller, token, db_url, grants.projects)
    host = await _queue_host(db_url, _funded_project())
    async with _queued_allocations(caller, db_url, host) as owned:
        held = await _compare_availability(caller, token, db_url, grants.projects)
        before = cast(dict[str, dict[str, Any]], idle["hosts"])[host]
        during = cast(dict[str, dict[str, Any]], held["hosts"])[host]
        assert during["in_use"] == before["in_use"] + 1, "the grant did not occupy its host"
        assert held["queued"] == {"total": 2, "by_kind": {"local-libvirt": 1}, "by_id": 1}
    after = await _compare_availability(caller, token, db_url, grants.projects)
    assert after["hosts"] == idle["hosts"], "the released allocation did not free its host"
    assert after["queued"] == {"total": 0, "by_kind": {}, "by_id": 0}
    return {
        "hosts": len(cast(dict[str, object], idle["hosts"])),
        "held_host": host,
        "queued": held["queued"],
        "quota_restored": True,
        "owned": owned,
    }
