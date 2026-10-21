"""Real Run/System/Job service cells (#3097); synthetic builds prove validation only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import sys
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from typing import Any, LiteralString, cast

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import FILTERED_LIST_BOUNDARIES
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.authority_lane import (
    on_authority_system,
)
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.service_cells import (
    _ABSENT_ID,
    _BUILD_ID,
    _BUILD_PROFILE,
    _built_run,
    _check_upload,
    _contributor,
    _declaration,
    _fixture,
    _Investigation,
    _investigation,
    _live_state,
    _member,
    _put,
    _rows,
    _s3,
    _stored,
    _stranger,
    _synthetic_build,
    _unbound_run,
    _versions,
    _viewer,
)
from tests.integration.live_stack.spine import build_and_upload_kernel, ok, scalar
from tests.integration.live_stack.tool_cells import (
    Boundary,
    FilteredListControl,
    Grants,
    HttpCaller,
    Rejection,
    boundary_of,
    caller_for,
    http_caller,
    one,
    prove_functional,
    prove_rejection,
)

TOOLS = (
    "runs.create",
    "runs.get",
    "runs.list",
    "runs.set",
    "runs.complete_build",
    "runs.bind",
    "systems.get",
    "systems.list",
    "jobs.list",
    "jobs.wait",
    "jobs.cancel",
)


_READERS = frozenset(
    {"runs.get", "runs.list", "systems.get", "systems.list", "jobs.list", "jobs.wait"}
)


_LISTS = frozenset({"runs.list", "systems.list", "jobs.list"})


_NEEDS_SYSTEM = frozenset(
    {
        "runs.create",
        "runs.bind",
        "runs.list",
        "systems.get",
        "systems.list",
        "jobs.list",
        "jobs.wait",
        "jobs.cancel",
    }
)


_RETENTION: dict[str, list[dict[str, object]]] = {}


_RUN: LiteralString = "SELECT * FROM runs WHERE id = %s"


_SYSTEM: LiteralString = "SELECT * FROM systems WHERE id = %s"


_JOB: LiteralString = "SELECT * FROM jobs WHERE id = %s"


_STEPS: LiteralString = "SELECT step, state, result FROM run_steps WHERE run_id = %s ORDER BY step"


async def _row(db: str, query: LiteralString, key: str) -> dict[str, Any]:
    rows = await _rows(db, query, (key,))
    assert len(rows) == 1, "the independent owner record is absent or duplicated"
    return rows[0]


async def _call(c: HttpCaller, grants: Grants, tool: str, **args: object) -> ToolResponse:
    result = one(await c.call(tool, args, c.token(grants), discover=True))
    assert result.error_category is None, f"{tool} failed: {result.error_category}"
    return result


def _assert_run(env: ToolResponse, row: Mapping[str, Any]) -> None:
    assert env.object_id == str(row["id"])
    assert env.status == row["state"]
    for name in ("project", "target_kind", "label", "outcome_note", "build_ref"):
        assert env.data.get(name) == row[name], f"run {name} disagrees with durable record"
    for name in ("investigation_id", "system_id"):
        expected = str(row[name]) if row[name] is not None else None
        assert env.data.get(name) == expected, f"run {name} disagrees with durable record"
    for name, ref in (("kernel_ref", "kernel"), ("debuginfo_ref", "debuginfo")):
        assert env.refs.get(ref) == row[name]


def _assert_note(before: Mapping[str, Any], after: Mapping[str, Any], note: str | None) -> None:
    assert after["outcome_note"] == note, "note was not changed or cleared"
    assert {k: v for k, v in before.items() if k not in {"outcome_note", "updated_at"}} == {
        k: v for k, v in after.items() if k not in {"outcome_note", "updated_at"}
    }, "annotation changed protected Run fields"


def _assert_page(actual: list[str], expected: list[str]) -> None:
    assert len(actual) == len(set(actual)), "page repeated an item"
    assert actual == expected, "filtered page differs from independently selected records"


def _assert_unclaimed(job: Mapping[str, Any], state: str) -> None:
    assert job["state"] == state, "cancellation job has the wrong durable state"
    assert job["attempt"] == 0 and job["worker_id"] is None, "job was claimed; see #3241"
    assert job["lease_expires_at"] is None and job["heartbeat_at"] is None, (
        "job acquired a worker lease"
    )


def _assert_retained(before: object, after: object) -> None:
    assert after == before, (
        "queued cancellation changed domain/storage/System/build or authority work"
    )


async def _pages(c: HttpCaller, grants: Grants, tool: str, request: dict[str, object]) -> list[str]:
    ids: list[str] = []
    cursors: set[str] = set()
    for _ in range(100):
        env = await _call(c, grants, tool, request={**request, "limit": 1})
        ids.extend(item.object_id for item in env.items)
        cursor = env.data["next_cursor"]
        assert bool(cursor) == env.data["truncated"], "noncanonical paging metadata"
        if cursor is None:
            return ids
        assert isinstance(cursor, str) and cursor not in cursors, "page repeated its cursor"
        cursors.add(cursor)
        request = {**request, "cursor": cursor}
    raise AssertionError("list exceeded the bounded fixture's 100 pages")


async def _create(inv: _Investigation, c: HttpCaller, g: Grants, system: str | None = None) -> str:
    args: dict[str, object] = {
        "investigation_id": inv.id,
        "build_profile": _BUILD_PROFILE,
        "label": "cov-run",
    }
    args |= {"system_id": system} if system else {"target_kind": "local-libvirt"}
    env = await _call(c, g, "runs.create", **args)
    inv.runs.append(env.object_id)
    assert env.status == "created" and env.data["investigation_id"] == inv.id
    assert env.data["system_id"] == system and env.data["target_kind"] == "local-libvirt"
    return env.object_id


async def _stage(inv: _Investigation, db: str) -> tuple[str, set[str]]:
    run_id = await _unbound_run(inv)
    content = dict(zip(("kernel", "vmlinux"), _synthetic_build(), strict=True))
    uploaded = one(
        await inv.caller.call(
            "artifacts.create_run_upload",
            {
                "run_id": run_id,
                "artifacts": [_declaration(n, b) for n, b in content.items()],
            },
            inv.token,
        )
    )
    keys = {item.object_id for item in uploaded.items}
    for item in uploaded.items:
        await _put(item, content[str(item.data["name"])])
        stored, _metadata = await asyncio.to_thread(_stored, item.object_id)
        expected = content[str(item.data["name"])]
        _assert_digest(stored, expected)
        await _check_upload(db, item, expected, owner_kind="runs", owner_id=run_id)
    return run_id, keys


async def _complete(inv: _Investigation, c: HttpCaller, g: Grants, db: str) -> str:
    run_id, keys = await _stage(inv, db)
    env = await _call(c, g, "runs.complete_build", run_id=run_id, build_id=_BUILD_ID)
    row = await _row(db, _RUN, run_id)
    assert env.status == row["state"] == "succeeded"
    inv.runs.remove(run_id)
    steps = await _rows(db, _STEPS, (run_id,))
    assert [s["step"] for s in steps] == ["build"] and steps[0]["state"] == "succeeded"
    result = steps[0]["result"]
    assert result["build_id"] == _BUILD_ID
    assert env.data["build_ref"] == result["build_ref"]
    assert env.refs["kernel"] == row["kernel_ref"] and env.refs["vmlinux"] == row["debuginfo_ref"]
    assert row["build_ref"] == result["build_ref"]
    generation = await _rows(
        db,
        "SELECT * FROM investigation_builds WHERE investigation_id = %s AND build_ref = %s",
        (inv.id, result["build_ref"]),
    )
    assert len(generation) == 1, "the exact adopted generation is absent or duplicated"
    _assert_build_versions(result, generation[0])
    assert row["kernel_ref"] in keys and row["debuginfo_ref"] in keys
    artifacts = await _rows(
        db,
        "SELECT object_key, owner_kind, owner_id FROM artifacts WHERE object_key = ANY(%s)",
        (list(keys),),
    )
    assert {a["object_key"] for a in artifacts} == keys
    assert all(
        a["owner_kind"] == "investigations" and str(a["owner_id"]) == inv.id for a in artifacts
    )
    return run_id


async def _run_body(
    c: HttpCaller, g: Grants, *, tool: str, db: str, system: Any | None = None
) -> dict[str, object]:
    project = g.projects[0]
    async with _owned(c, project, db) as inv:
        if tool == "runs.create":
            assert system is not None
            ids = [await _create(inv, c, g), await _create(inv, c, g, system.system_id)]
            for run_id, target in zip(ids, (None, system.system_id), strict=True):
                row = await _row(db, _RUN, run_id)
                assert row["project"] == project and str(row["investigation_id"]) == inv.id
                assert row["state"] == "created" and row["label"] == "cov-run"
                assert row["target_kind"] == "local-libvirt"
                assert (str(row["system_id"]) if row["system_id"] else None) == target
            assert (await _row(db, _SYSTEM, system.system_id))["allocation_id"] is not None
        elif tool == "runs.complete_build":
            ids = [await _complete(inv, c, g, db)]
        else:
            run_id, _ = await _built_run(inv)
            ids = [run_id]
            before = await _row(db, _RUN, run_id)
            if tool == "runs.get":
                env = await _call(c, g, tool, run_id=run_id)
                _assert_run(env, before)
                assert env.data["steps"] == {
                    "build": "succeeded",
                    "install": "pending",
                    "boot": "pending",
                }
                assert [r["step"] for r in await _rows(db, _STEPS, (run_id,))] == ["build"]
            elif tool == "runs.set":
                for supplied, expected in (("  cov outcome  ", "cov outcome"), ("  ", None)):
                    env = await _call(c, g, tool, run_id=run_id, outcome_note=supplied)
                    after = await _row(db, _RUN, run_id)
                    _assert_note(before, after, expected)
                    assert (
                        env.data["outcome_note"] == expected
                        and env.data["run_state"] == before["state"]
                    )
            elif tool == "runs.bind":
                assert system is not None
                env = await _call(c, g, tool, run_id=run_id, system_id=system.system_id)
                after = await _row(db, _RUN, run_id)
                assert env.status == "bound" and str(after["system_id"]) == system.system_id
                assert {
                    k: v
                    for k, v in before.items()
                    if k not in {"system_id", "allocation_id", "updated_at"}
                } == {
                    k: v
                    for k, v in after.items()
                    if k not in {"system_id", "allocation_id", "updated_at"}
                }
                owner = await _row(db, _SYSTEM, system.system_id)
                assert owner["state"] == "ready" and owner["allocation_id"] is not None
            else:
                raise AssertionError(f"no functional Run body for {tool}")
        return {"owned": [inv.id, *ids], "relationships": True, "tool": tool}


async def _list_body(
    c: HttpCaller, g: Grants, *, tool: str, db: str, system: Any
) -> dict[str, object]:
    project = g.projects[0]
    async with _owned(c, project, db) as inv, _owned(c, project, db) as other:
        if tool == "runs.list":
            await _create(inv, inv.caller, _contributor(project), system.system_id)
            await _unbound_run(inv)
            await _unbound_run(inv)
            excluded = await _unbound_run(other)
            await _call(other.caller, _contributor(project), "runs.cancel", run_id=excluded)
            other.runs.remove(excluded)
            rows = await _rows(
                db,
                "SELECT * FROM runs WHERE project = %s ORDER BY created_at DESC, id DESC",
                (project,),
            )
            filters = (
                {},
                {"project": project},
                {"system_id": system.system_id},
                {"investigation_id": inv.id},
                {"state": "created"},
                {"state": "canceled"},
                {"project": project + "-absent"},
                {"system_id": _ABSENT_ID},
                {
                    "project": project,
                    "system_id": system.system_id,
                    "investigation_id": inv.id,
                    "state": "created",
                },
            )
        elif tool == "systems.list":
            rows = await _rows(
                db,
                "SELECT * FROM systems WHERE project = %s ORDER BY created_at DESC, id DESC",
                (project,),
            )
            current = next(r for r in rows if str(r["id"]) == system.system_id)
            assert len(rows) >= 2 and any(r["state"] == "torn_down" for r in rows), (
                "missing independent historical System control"
            )
            filters = (
                {},
                {"project": project},
                {"project": project + "-absent"},
                {"allocation_id": str(current["allocation_id"])},
                {"state": "ready"},
                {"state": "torn_down"},
                {"shape": current["shape"] or "__custom__"},
                {"shape": "cov-absent-shape"},
                {
                    "project": project,
                    "allocation_id": str(current["allocation_id"]),
                    "state": "ready",
                    "shape": current["shape"] or "__custom__",
                },
            )
        else:
            await _queued_install(
                c, _contributor(project), db, system, inv, cancel_caller=inv.caller
            )
            rows = await _rows(
                db,
                "SELECT j.*, r.investigation_id AS inv FROM jobs j LEFT JOIN runs r "
                "ON r.id::text = j.payload->>'run_id' WHERE j.authorizing->>'project' "
                "= %s ORDER BY j.created_at DESC, j.id DESC",
                (project,),
            )
            assert len(rows) >= 2 and any(r["kind"] == "install" for r in rows)
            filters = (
                {},
                {"state": "canceled"},
                {"kind": "install"},
                {"system_id": system.system_id},
                {"system_id": _ABSENT_ID},
                {"investigation_id": inv.id},
                {"investigation_id": _ABSENT_ID},
                {
                    "state": "canceled",
                    "kind": "install",
                    "investigation_id": inv.id,
                    "system_id": system.system_id,
                },
            )
        for request in filters:
            expected = _selected(tool, rows, request)
            _assert_page(await _pages(c, g, tool, dict(request)), expected)
        return {"tool": tool, "filters": len(filters), "records": len(rows), "owned": [inv.id]}


def _selected(tool: str, rows: list[dict[str, Any]], request: Mapping[str, object]) -> list[str]:
    def matches(row: dict[str, Any]) -> bool:
        for name, value in request.items():
            if name == "shape":
                actual = row["shape"] or "__custom__"
            elif tool == "jobs.list" and name == "system_id":
                actual = row["payload"].get("system_id")
            elif tool == "jobs.list" and name == "investigation_id":
                actual = str(row["inv"]) if row["inv"] else None
            else:
                actual = str(row[name]) if row[name] is not None else None
            if actual != value:
                return False
        return True

    return [str(row["id"]) for row in rows if matches(row)]


async def _system_body(c: HttpCaller, g: Grants, *, db: str, system: Any) -> dict[str, object]:
    async with _owned(c, system.project, db) as inv:
        run_id = await _create(inv, inv.caller, _contributor(system.project), system.system_id)
        row = await _row(db, _SYSTEM, system.system_id)
        env = await _call(c, g, "systems.get", system_id=system.system_id)
        assert env.object_id == system.system_id and env.status == row["state"] == "ready"
        for name in ("project", "shape", "label"):
            assert env.data[name] == row[name]
        allocation = await _row(
            db, "SELECT * FROM allocations WHERE id = %s", str(row["allocation_id"])
        )
        assert env.data["allocation_id"] == str(row["allocation_id"])
        assert env.data["resource_id"] == str(allocation["resource_id"])
        assert env.data["resource_kind"] == "local-libvirt"
        assert allocation["project"] == system.project and allocation["state"] == "active"
        assert env.data["active_run"] == {"id": run_id, "state": "created"}
        assert env.data["run_investigation_ids"] == [inv.id]
        assert (
            env.data["supports_snapshots"] is True and env.data["supports_traffic_capture"] is True
        )
        assert env.data["supported_capture_methods"] == ["host_dump", "kdump"]
        return {"relationships": True, "capabilities": True, "owned": [inv.id, run_id]}


async def _wait(c: HttpCaller, g: Grants, *, db: str, system: Any) -> dict[str, object]:
    queued = ok(
        await scalar(system.op, "systems.check_ssh_reachable", system_id=system.system_id),
        "ssh-check",
    )
    assert queued.status == "queued"
    deadline = time.monotonic() + 600
    while True:
        env = await _call(c, g, "jobs.wait", job_id=queued.object_id, timeout_s=30)
        if env.status in {"succeeded", "failed", "canceled"}:
            break
        assert time.monotonic() < deadline, "SSH job did not settle within 600 seconds"
    row = await _row(db, _JOB, queued.object_id)
    assert env.status == row["state"] == "succeeded"
    assert row["attempt"] >= 1 and row["worker_id"]
    assert row["payload"]["system_id"] == system.system_id
    assert row["authorizing"]["project"] == system.project

    def banner() -> bytes:
        with socket.create_connection(system.endpoint, timeout=10) as connection:
            connection.settimeout(10)
            return connection.recv(255)

    assert (await asyncio.to_thread(banner)).startswith(b"SSH-"), (
        "independent endpoint has no SSH banner"
    )
    return {
        "terminal": "succeeded",
        "worker_attempt": row["attempt"],
        "ssh_banner": True,
        "owned": [queued.object_id],
    }


async def _cancel_state(db: str, system: Any, run_id: str) -> dict[str, object]:
    return {
        "system": await _row(db, _SYSTEM, system.system_id),
        "run": await _row(db, _RUN, run_id),
        "steps": await _rows(db, _STEPS, (run_id,)),
        "uses": await _rows(
            db,
            "SELECT u.* FROM investigation_build_uses u JOIN jobs j ON j.id = "
            "u.job_id WHERE j.payload->>'run_id' = %s",
            (run_id,),
        ),
        "journal": await _rows(
            db,
            "SELECT * FROM external_boot_authority_journal_heads WHERE system_id = %s",
            (system.system_id,),
        ),
        "authorities": await _rows(
            db, "SELECT * FROM external_boot_authorities WHERE system_id = %s", (system.system_id,)
        ),
        "native": await asyncio.to_thread(_native, system.system_id),
    }


async def _queued_install(
    c: HttpCaller,
    g: Grants,
    db: str,
    system: Any,
    inv: _Investigation,
    *,
    cancel_caller: HttpCaller | None = None,
) -> dict[str, object]:
    if os.environ.get("KDIVE_3097_EXCLUSIVE_QUEUE") != "approved":
        raise ScenarioStop(
            Outcome.BLOCKED,
            "queued-install proof needs separate operator approval of the exclusive idle queue",
        )
    tree, _manifest = _fixture()
    run_id = await _create(inv, inv.caller, _contributor(system.project), system.system_id)
    await build_and_upload_kernel(system.op, run_id=run_id, kernel_tree=tree, with_vmlinux=True)
    assert (await _row(db, _RUN, run_id))["state"] == "succeeded"
    inv.runs.remove(run_id)
    queue = await _rows(db, "SELECT queue_paused FROM ops_control")
    assert queue == [{"queue_paused": False}], "queue is already paused; refusing ownership"
    active = await _rows(db, "SELECT id FROM jobs WHERE state IN ('queued', 'running')")
    assert not active, "queue contains foreign work; exclusive lane is not idle"
    operator = Grants(
        f"{system.project}-queue",
        (system.project,),
        {system.project: "admin"},
        ("platform_operator",),
    )
    direct = HttpCaller("direct", c.base_url, c.issuer)
    paused = False
    pause_requested = False
    attempted = False
    admission_key = f"cov-install-{run_id}"
    job_id: str | None = None
    before = await _cancel_state(db, system, run_id)
    assert not before["uses"] and not before["journal"] and not before["authorities"]
    assert [r["step"] for r in cast(list[dict[str, Any]], before["steps"])] == ["build"]
    try:
        pause_requested = True
        await _call(direct, operator, "ops.set_queue_paused", paused=True)
        paused = True
        assert await _rows(db, "SELECT queue_paused FROM ops_control") == [{"queue_paused": True}]
        attempted = True
        queued = ok(
            await scalar(system.op, "runs.install", run_id=run_id, idempotency_key=admission_key),
            "enqueue-install",
        )
        job_id = queued.object_id
        _assert_unclaimed(await _row(db, _JOB, job_id), "queued")
        _assert_retained(before, await _cancel_state(db, system, run_id))
        env = await _call(cancel_caller or c, g, "jobs.cancel", job_id=job_id)
        assert env.status == "canceled"
        _assert_unclaimed(await _row(db, _JOB, job_id), "canceled")
        _assert_retained(before, await _cancel_state(db, system, run_id))
    finally:
        if paused:
            primary = sys.exception()
            try:
                await _finish_queue(
                    direct, operator, db, system.project, run_id, admission_key, attempted
                )
            except BaseException:
                if primary is None:
                    raise
                primary.add_note(
                    "owned install or queue restoration remains unresolved; "
                    "operator must inspect the exclusive paused lane"
                )
        elif pause_requested:
            primary = sys.exception()
            if primary is not None:
                primary.add_note(
                    "queue pause outcome is unknown; operator must inspect the exclusive lane"
                )
    assert job_id is not None
    # Cover at least one worker poll and one reconciler interval without accepting a late claim.
    deadline = time.monotonic() + 35
    while True:
        _assert_unclaimed(await _row(db, _JOB, job_id), "canceled")
        _assert_retained(before, await _cancel_state(db, system, run_id))
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(1)
    return {
        "terminal": "canceled",
        "queued_unclaimed": True,
        "retained": True,
        "owned": [run_id, job_id],
    }


async def _cancel(c: HttpCaller, g: Grants, *, db: str, system: Any) -> dict[str, object]:
    async with _owned(c, system.project, db) as inv:
        return await _queued_install(c, g, db, system, inv)


_NATIVE = r"""
import hashlib, json, stat, sys
from pathlib import Path
from uuid import UUID
import libvirt
from defusedxml.ElementTree import fromstring
sid = str(UUID(sys.argv[1]))
endpoint = Path("/run/kdive/provider-authority/libvirt/libvirt-sock")
if not stat.S_ISSOCK(endpoint.lstat().st_mode):
    raise SystemExit("authority endpoint is not the expected socket")
connection = libvirt.openReadOnly("qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock")
if connection is None:
    raise SystemExit("authority daemon unavailable")
try:
    domain = connection.lookupByName("kdive-" + sid)
    xml = domain.XMLDesc(0)
    disks = []
    for source in fromstring(xml).findall("./devices/disk//source"):
        name = source.get("file")
        if name:
            path = Path(name)
            st = path.stat()
            disks.append((str(path), st.st_dev, st.st_ino))
    journal = Path("/var/lib/kdive/provider-authority/journal") / (sid + ".jsonl")
    if journal.exists() and journal.stat().st_size > 1024 * 1024:
        raise SystemExit("authority journal exceeds bounded observation")
    raw = journal.read_bytes() if journal.exists() else b""
    print(json.dumps({"xml": xml, "active": domain.isActive(), "disks": disks,
                      "journal_sha256": hashlib.sha256(raw).hexdigest()}))
finally:
    connection.close()
"""


def _native(system_id: str) -> object:
    import subprocess
    from uuid import UUID

    result = subprocess.run(
        [
            "sudo",
            "-n",
            "-H",
            "-u",
            "kdive-provider-authority",
            "/opt/kdive-provider-authority/.venv/bin/python",
            "-I",
            "-c",
            _NATIVE,
            str(UUID(system_id)),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, "independent authority domain/storage observation failed"
    assert len(result.stdout) < 1024 * 1024, "authority observation exceeded its bound"
    return json.loads(result.stdout)


def _valid(tool: str, project: str) -> dict[str, object]:
    if tool in _LISTS:
        return {"request": {} if tool == "jobs.list" else {"project": project}}
    if tool == "runs.create":
        return {
            "investigation_id": _ABSENT_ID,
            "build_profile": _BUILD_PROFILE,
            "target_kind": "local-libvirt",
        }
    if tool == "runs.bind":
        return {"run_id": _ABSENT_ID, "system_id": _ABSENT_ID}
    if tool == "runs.set":
        return {"run_id": _ABSENT_ID, "outcome_note": "cov note"}
    if tool.startswith("runs."):
        return {"run_id": _ABSENT_ID}
    if tool == "systems.get":
        return {"system_id": _ABSENT_ID}
    return {"job_id": _ABSENT_ID}


def _rejection(tool: str, boundary: Boundary, project: str) -> Rejection:
    args = _valid(tool, project)
    if boundary == "validation":
        args = {"request": {"limit": "many"}} if tool in _LISTS else {}
        return Rejection(args, _viewer(project) if tool in _READERS else _contributor(project))
    if boundary == "authentication":
        return Rejection(args, _viewer(project))
    if boundary == "project-isolation":
        hidden = tool in {"runs.create", "runs.bind"}
        category = (
            ErrorCategory.CONFIGURATION_ERROR.value if hidden else ErrorCategory.NOT_FOUND.value
        )
        return Rejection(
            args, _stranger(), frozenset({category}), absent_twin=args if hidden else None
        )
    return Rejection(args, _member(project) if tool in _READERS else _viewer(project))


async def _target(
    inv: _Investigation, tool: str, db: str, system: Any | None
) -> tuple[dict[str, object], str]:
    if tool == "runs.create":
        return {"investigation_id": inv.id}, inv.id
    if tool == "systems.get":
        assert system is not None
        return {"system_id": system.system_id}, system.system_id
    if tool == "systems.list":
        assert system is not None
        return {}, system.system_id
    if tool.startswith("jobs."):
        assert system is not None
        jobs = await _rows(
            db,
            "SELECT id FROM jobs WHERE authorizing->>'project' = %s AND kind = "
            "'provision' AND state = 'succeeded' ORDER BY created_at DESC LIMIT 1",
            (system.project,),
        )
        assert len(jobs) == 1, "missing real succeeded provisioning control"
        job_id = str(jobs[0]["id"])
        return ({} if tool == "jobs.list" else {"job_id": job_id}), job_id
    if tool == "runs.complete_build":
        run_id, _keys = await _stage(inv, db)
        return {"run_id": run_id, "build_id": _BUILD_ID}, run_id
    run_id = (await _built_run(inv))[0] if tool == "runs.bind" else await _unbound_run(inv)
    args: dict[str, object] = {} if tool == "runs.list" else {"run_id": run_id}
    if tool == "runs.bind":
        assert system is not None
        args["system_id"] = system.system_id
    return args, run_id


async def _denied(
    run: CellRun, c: HttpCaller, project: str, db: str, system: Any | None = None
) -> None:
    boundary, tool = boundary_of(run.cell), run.cell.operation
    rejection = _rejection(tool, boundary, project)
    snapshot = partial(_live_state, db, project)
    protected = partial(_protected, db, project)
    if boundary in {"authentication", "validation"}:
        await prove_rejection(run, c, boundary, rejection, protected)
        return
    before = await snapshot()
    async with _owned(c, project, db) as inv:
        overrides, expected_id = await _target(inv, tool, db, system)
        rejection = replace(rejection, args={**rejection.args, **overrides})
        if (tool, boundary) in FILTERED_LIST_BOUNDARIES:

            async def expected() -> str:
                query = _SYSTEM if tool == "systems.list" else _JOB if tool == "jobs.list" else _RUN
                await _row(db, query, expected_id)
                return expected_id

            rejection = replace(rejection, filtered=FilteredListControl(_viewer(project), expected))
        await prove_rejection(run, c, boundary, rejection, protected)
    assert await snapshot() == before, "rejection fixture left live project state"


async def _scenario_body(run: CellRun, base_url: str, issuer: OidcIssuer, db: str) -> None:
    from tests.integration.live_stack.tool_cells import on_lane_system

    cell, tool = run.cell, run.cell.operation
    c = caller_for(cell.exposure, base_url, issuer)
    project = "cov-" + secrets.token_hex(4)
    run.artifacts.append(run.writer.artifact({"fixture_project": project, "operation": tool}))
    functional = cell.kind == "functional"
    targeted = functional or boundary_of(cell) in {"authorization", "project-isolation"}
    needs_system = (
        tool in _NEEDS_SYSTEM
        and targeted
        and (functional or tool not in {"runs.create", "runs.list"})
    )
    if not needs_system:
        if functional:
            grants = _viewer(project) if tool in _READERS else _contributor(project)
            await prove_functional(
                run,
                c,
                grants,
                partial(_run_body, tool=tool, db=db),
                partial(_live_state, db, project),
            )
        else:
            await _denied(run, c, project, db)
        return
    # A fixture run owns setup/cleanup evidence, distinct from the rejected tool's proof.
    fixture = CellRun(cell, run.writer)

    async def body(system: Any) -> dict[str, object]:
        if not functional:
            await _denied(run, c, project, db, system)
            return {"fixture": True}
        g = _viewer(project) if tool in _READERS else _contributor(project)
        if tool in _LISTS:
            return await _list_body(c, g, tool=tool, db=db, system=system)
        if tool == "systems.get":
            return await _system_body(c, g, db=db, system=system)
        if tool == "jobs.wait":
            return await _wait(c, g, db=db, system=system)
        if tool == "jobs.cancel":
            return await _cancel(c, g, db=db, system=system)
        return await _run_body(c, g, tool=tool, db=db, system=system)

    frame = (
        on_authority_system
        if functional and tool in {"jobs.cancel", "jobs.list"}
        else on_lane_system
    )
    try:
        if functional and tool == "systems.list":

            async def history(_system: Any) -> dict[str, object]:
                return {"historical_control": True}

            await on_lane_system(fixture, base_url, issuer, db, project=project, body=history)
            run.artifacts.extend(fixture.assertions.values())
            run.artifacts.append(run.writer.artifact({"fixture_observed": fixture.observed}))
        await frame(fixture, base_url, issuer, db, project=project, body=body)
    finally:
        run.artifacts.extend(fixture.artifacts)
        run.artifacts.extend(fixture.assertions.values())
        run.artifacts.append(run.writer.artifact({"fixture_observed": fixture.observed}))
    if functional:
        run.assertions["effect"] = fixture.assertions["effect"]
    run.assertions["cleanup"] = fixture.assertions["cleanup"]


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db: str) -> None:
    try:
        await _scenario_body(run, base_url, issuer, db)
    finally:
        retained = [item for items in _RETENTION.values() for item in items]
        _RETENTION.clear()
        if retained:
            run.artifacts.append(
                run.writer.artifact(
                    {
                        "policy_retained_adopted_objects": retained,
                        "retained_count": len(retained),
                        "retained_sha256": hashlib.sha256(
                            json.dumps(retained, sort_keys=True).encode()
                        ).hexdigest(),
                        "post_grace_collection_proven": False,
                    }
                )
            )


def _assert_build_versions(result: Mapping[str, Any], build: Mapping[str, Any]) -> None:
    versions = {name: artifact["version_id"] for name, artifact in build["artifacts"].items()}
    assert versions and result.get("artifact_versions") == versions, (
        "build step versions disagree with the adopted generation"
    )
    assert result["build_ref"] == build["build_ref"], (
        "build step reference disagrees with the adopted generation"
    )


def _assert_digest(stored: bytes, expected: bytes) -> None:
    assert (
        len(stored) == len(expected)
        and hashlib.sha256(stored).digest() == hashlib.sha256(expected).digest()
    ), "stored digest disagrees with uploaded build"


async def _gc_handoff(db: str, inv: _Investigation) -> None:
    row = await _row(db, "SELECT * FROM investigations WHERE id = %s", inv.id)
    assert row["state"] == "closed", "owned investigation did not close"
    builds = await _rows(
        db, "SELECT * FROM investigation_builds WHERE investigation_id = %s", (inv.id,)
    )
    artifacts = await _rows(
        db,
        "SELECT a.object_key, a.owner_kind, a.owner_id FROM artifacts a "
        "WHERE (a.owner_kind = 'investigations' AND a.owner_id = %s) OR "
        "(a.owner_kind = 'runs' AND a.owner_id IN "
        "(SELECT id FROM runs WHERE investigation_id = %s))",
        (inv.id, inv.id),
    )
    if builds or artifacts:
        assert row["cleanup_pending_at"] is not None, "closed build lacks its one-day GC handoff"
    owned = {a["object_key"]: a for a in artifacts}
    retained = _RETENTION.setdefault(str(row["project"]), [])
    for build in builds:
        for artifact in build["artifacts"].values():
            key, version = artifact["key"], artifact["version_id"]
            assert key in owned and owned[key]["owner_kind"] == "investigations"
            assert str(owned[key]["owner_id"]) == inv.id
            assert version in await asyncio.to_thread(_versions, key), (
                "adopted object lost its exact retained version"
            )
            retained.append({"owner": inv.id, "key": key, "version": version})
    for key, artifact in owned.items():
        if artifact["owner_kind"] == "runs":
            assert key.startswith(f"local/runs/{artifact['owner_id']}/"), (
                "artifact escaped Run ownership"
            )
            versions = await asyncio.to_thread(_versions, key)
            assert versions, "Run-owned provenance lost its retained versions"
            retained.extend(
                {"owner": str(artifact["owner_id"]), "key": key, "version": v} for v in versions
            )


@asynccontextmanager
async def _owned(c: HttpCaller, project: str, db: str) -> AsyncIterator[_Investigation]:
    inv = None
    primary = None
    try:
        async with _investigation(c.base_url, c.issuer, project) as inv:
            try:
                yield inv
            finally:
                # Close under the existing Investigation lock before reconciling adoption.
                # This bars an in-flight finalizer or runs.create from publishing afterward.
                inv.runs.clear()
                inv.keys.clear()
    except BaseException as exc:
        primary = exc
    if inv is not None:
        try:
            await _end_runs(http_caller(c), project, db, inv.id)
            await _reconcile_uploads(db, inv)
            await _gc_handoff(db, inv)
        except BaseException:
            if primary is None:
                raise
            primary.add_note(
                "owned Run/upload cleanup remains unresolved; "
                "retain this fixture's ownership evidence"
            )
    if primary is not None:
        raise primary


async def _end_runs(c: HttpCaller, project: str, db: str, investigation: str) -> None:
    closed = await _row(db, "SELECT * FROM investigations WHERE id = %s", investigation)
    assert closed["state"] == "closed", "cannot reconcile uploads before the Investigation closes"
    rows = await _rows(
        db, "SELECT * FROM runs WHERE investigation_id = %s ORDER BY id", (investigation,)
    )
    for row in rows:
        if row["state"] not in {"succeeded", "failed", "canceled"}:
            env = await _call(c, _contributor(project), "runs.cancel", run_id=str(row["id"]))
            assert env.status == "canceled"
        current = await _row(db, _RUN, str(row["id"]))
        assert current["state"] in {"succeeded", "failed", "canceled"}, "owned Run is not terminal"


def _own_versions(prefix: str) -> list[dict[str, Any]]:
    client = _s3()
    items = []
    for page in client.get_paginator("list_object_versions").paginate(
        Bucket=os.environ["KDIVE_S3_BUCKET"], Prefix=prefix
    ):
        items.extend([*page.get("Versions", []), *page.get("DeleteMarkers", [])])
    assert all(item["Key"].startswith(prefix) for item in items), (
        "object enumeration escaped owned prefix"
    )
    return items


def _purge_versions(items: list[dict[str, Any]]) -> None:
    client, bucket = _s3(), os.environ["KDIVE_S3_BUCKET"]
    for item in items:
        client.delete_object(Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"])


def _partition_versions(
    items: list[dict[str, Any]], adopted: Mapping[str, set[str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    retained, unadopted = [], []
    for item in items:
        (retained if item["VersionId"] in adopted.get(item["Key"], set()) else unadopted).append(
            item
        )
    return retained, unadopted


async def _reconcile_uploads(db: str, inv: _Investigation) -> None:
    runs = await _rows(
        db, "SELECT id, state FROM runs WHERE investigation_id = %s ORDER BY id", (inv.id,)
    )
    assert all(r["state"] in {"succeeded", "failed", "canceled"} for r in runs)
    builds = await _rows(
        db, "SELECT artifacts FROM investigation_builds WHERE investigation_id = %s", (inv.id,)
    )
    adopted: dict[str, set[str]] = {}
    for build in builds:
        for artifact in build["artifacts"].values():
            adopted.setdefault(artifact["key"], set()).add(artifact["version_id"])
    for run in runs:
        prefix = f"local/runs/{run['id']}/"
        items = await asyncio.to_thread(_own_versions, prefix)
        keys = sorted({item["Key"] for item in items})
        catalog = await _rows(
            db,
            "SELECT object_key, owner_kind, owner_id, etag FROM artifacts WHERE "
            "object_key = ANY(%s)",
            (keys,),
        )
        for artifact in catalog:
            key = artifact["object_key"]
            if key in adopted:
                assert (
                    artifact["owner_kind"] == "investigations"
                    and str(artifact["owner_id"]) == inv.id
                )
                continue
            assert artifact["owner_kind"] == "runs" and str(artifact["owner_id"]) == str(
                run["id"]
            ), "unexpected catalog owner; refusing object deletion"
            versions = [item for item in items if item["Key"] == key]
            assert len(versions) == 1 and str(versions[0].get("ETag", "")).strip('"') == str(
                artifact["etag"]
            ).strip('"'), "Run-owned version identity is ambiguous; refusing deletion"
            adopted.setdefault(key, set()).add(str(versions[0]["VersionId"]))
        _retained, unadopted = _partition_versions(items, adopted)
        await asyncio.to_thread(_purge_versions, unadopted)
        remaining = await asyncio.to_thread(_own_versions, prefix)
        assert not _partition_versions(remaining, adopted)[1], (
            "unadopted owned upload versions remain"
        )


async def _protected(db: str, project: str) -> dict[str, object]:
    return {
        "project": await _live_state(db, project),
        "objects": await _protected_objects(db, project),
        "runs": await _rows(db, "SELECT * FROM runs WHERE project = %s ORDER BY id", (project,)),
        "jobs": await _rows(
            db, "SELECT * FROM jobs WHERE authorizing->>'project' = %s ORDER BY id", (project,)
        ),
        "steps": await _rows(
            db,
            "SELECT s.* FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE "
            "r.project = %s ORDER BY s.run_id, s.step",
            (project,),
        ),
        "builds": await _rows(
            db,
            "SELECT b.* FROM investigation_builds b JOIN investigations i ON i.id = "
            "b.investigation_id WHERE i.project = %s ORDER BY b.generation",
            (project,),
        ),
        "uses": await _rows(
            db,
            "SELECT u.* FROM investigation_build_uses u JOIN investigations i ON i.id "
            "= u.investigation_id WHERE i.project = %s ORDER BY u.use_id",
            (project,),
        ),
    }


async def _finish_queue(
    c: HttpCaller,
    operator: Grants,
    db: str,
    project: str,
    run_id: str,
    admission_key: str,
    attempted: bool,
    *,
    deadline_s: float = 30,
) -> None:
    if attempted:
        deadline = time.monotonic() + deadline_s
        while True:
            recorded = await _rows(
                db,
                "SELECT result FROM idempotency_keys WHERE project = %s AND key = %s "
                "AND kind = 'runs.install'",
                (project, admission_key),
            )
            jobs = await _rows(
                db, "SELECT * FROM jobs WHERE dedup_key = %s", (f"{run_id}:install",)
            )
            if len(recorded) == 1 and len(jobs) == 1:
                break
            assert time.monotonic() < deadline, (
                "install admission outcome unresolved; leave exclusive queue paused for operator"
            )
            await asyncio.sleep(0.2)
        job = jobs[0]
        result = recorded[0]["result"]
        envelope = result.get("envelope", result)
        assert envelope["object_id"] == str(job["id"])
        assert job["payload"]["run_id"] == run_id and job["authorizing"]["project"] == project
        assert job["state"] in {"queued", "canceled"}, (
            "owned install is not queued/unclaimed; see #3241; leave queue paused"
        )
        _assert_unclaimed(job, str(job["state"]))
        if job["state"] == "queued":
            env = await _call(c, _contributor(project), "jobs.cancel", job_id=str(job["id"]))
            assert env.status == "canceled"
        _assert_unclaimed(await _row(db, _JOB, str(job["id"])), "canceled")
    await _call(c, operator, "ops.set_queue_paused", paused=False)
    assert await _rows(db, "SELECT queue_paused FROM ops_control") == [{"queue_paused": False}]


async def _protected_objects(db: str, project: str) -> dict[str, object]:
    runs = await _rows(db, "SELECT id FROM runs WHERE project = %s ORDER BY id", (project,))
    return {
        str(run["id"]): sorted(
            await asyncio.to_thread(_own_versions, f"local/runs/{run['id']}/"),
            key=lambda item: (item["Key"], item["VersionId"]),
        )
        for run in runs
    }
