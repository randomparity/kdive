"""Prove ``systems.force_unplug_device`` over HTTP on the fault-inject provider (#3762, ADR-0828).

``live_stack``-marked. The fault-inject provider needs no hypervisor or guest, so this cell runs
against a stack whose server, worker and reconciler have ``KDIVE_FAULT_INJECT`` set. It registers
its own ``fault-inject`` Resource, funds a throwaway project, provisions two Systems (one whose
profile opts in to ``force_unplug_device``, one that does not) and drives the tool through the
gate with a project admin and a project contributor. Provider behaviour is flipped through the
Resource row's ``hotplug_behaviour`` capability, which the next operation's binding reads.

The cell proves, in order: (a) the denial without the profile opt-in names ``profile_opt_in``;
(b) the denial for a contributor names ``admin_role``; (c) a graceful un-plug under ``hang`` times
out and leaves a ``failed`` row, which a forced un-plug then removes; (d) under ``no_force`` the
forced job fails ``control_failure`` / ``unplug_refused`` and the row returns to its prior state;
(e) the audit log holds the denial rows and the two enqueue rows. Everything it created is torn
down in a ``finally``. ``docs/operating/runbooks/live-testing.md`` covers the stack it needs.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest

from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.security.audit import args_digest
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.spine import POLL_INTERVAL_S, ok
from tests.integration.live_stack.tool_cells import Exposure, Grants, HttpCaller, one

pytestmark = pytest.mark.live_stack

_PROFILE_BASE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 1024,
    "disk_gb": 10,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "force-unplug-cell",
}
_DEADLINE_S = 180.0
_TERMINAL = {"succeeded", "failed", "canceled"}


def _profile(*, opt_in: bool) -> dict[str, Any]:
    ops = ["force_unplug_device"] if opt_in else []
    section = {"destructive_ops": ops, "capture_method": "host_dump"}
    return {**_PROFILE_BASE, "provider": {"fault-inject": section}}


class _Cell:
    """The stack handles one run of the cell shares."""

    def __init__(self, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
        self.base_url = base_url
        self.db_url = db_url
        self.caller = HttpCaller(cast(Exposure, "direct"), base_url, issuer)
        self.project = f"cov-{secrets.token_hex(4)}"
        self.operator = self.caller.token(
            Grants(
                f"{self.project}-operator",
                (self.project,),
                {self.project: "admin"},
                ("platform_admin", "platform_operator"),
            )
        )
        self.admin = self.caller.token(self._grants("admin"))
        self.contributor = self.caller.token(self._grants("contributor"))
        self.resource_id = ""
        self.allocations: list[str] = []
        self.systems: list[str] = []

    def _grants(self, role: str) -> Grants:
        return Grants(f"{self.project}-{role}", (self.project,), {self.project: role})

    async def call(self, token: str, tool: str, **args: object) -> ToolResponse:
        async with LiveStackClient.over_http(self.base_url, token) as client:
            result = await client.call_tool(tool, **args)
        assert isinstance(result, ToolResponse), f"{tool} answered a list"
        return result

    async def setup(self, tool: str, **args: object) -> ToolResponse:
        return ok(await self.call(self.operator, tool, **args), tool)

    async def settle(self, job_id: str) -> ToolResponse:
        """Poll ``jobs.wait`` until the job is terminal and return that envelope."""
        deadline = time.monotonic() + _DEADLINE_S
        while True:
            env = await self.call(self.operator, "jobs.wait", job_id=job_id, timeout_s=60.0)
            if env.status in _TERMINAL:
                return env
            assert time.monotonic() < deadline, f"job {job_id} stayed {env.status}"
            await asyncio.sleep(POLL_INTERVAL_S)

    async def succeed(self, env: ToolResponse, tool: str) -> None:
        assert env.status == "running", f"{tool} answered {env.status}: {env.model_dump()}"
        done = await self.settle(str(env.data["job_id"]))
        assert done.status == "succeeded", f"{tool} job ended {done.model_dump()}"

    async def provision(self, *, opt_in: bool) -> str:
        grant = await self.setup(
            "allocations.request",
            project=self.project,
            vcpus=2,
            memory_gb=1,
            disk_gb=10,
            resource={"mode": "id", "resource_id": self.resource_id},
        )
        self.allocations.append(grant.object_id)
        provisioned = await self.setup(
            "systems.provision", allocation_id=grant.object_id, profile=_profile(opt_in=opt_in)
        )
        system_id = str(provisioned.data["system_id"])
        self.systems.append(system_id)
        done = await self.settle(provisioned.object_id)
        assert done.status == "succeeded", f"provision ended {done.model_dump()}"
        return system_id

    async def sql(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        async with await psycopg.AsyncConnection.connect(self.db_url, autocommit=True) as conn:
            cursor = await conn.execute(query.encode(), params)
            return await cursor.fetchall() if cursor.description else []

    async def behaviour(self, value: str | None) -> None:
        """Set or clear the Resource's ``hotplug_behaviour``; the next operation reads it."""
        patch = json.dumps({"hotplug_behaviour": value} if value else {})
        await self.sql(
            "UPDATE resources SET capabilities = (capabilities - 'hotplug_behaviour') || %s::jsonb "
            "WHERE id = %s",
            patch,
            self.resource_id,
        )

    async def audit(self, system_id: str) -> list[tuple[str, str, str]]:
        rows = await self.sql(
            "SELECT tool, transition, args_digest FROM audit_log WHERE object_id = %s "
            "AND tool = 'systems.force_unplug_device' ORDER BY ts, id",
            system_id,
        )
        return [(str(t), str(tr), str(d)) for t, tr, d in rows]

    async def devices(self, system_id: str) -> list[Mapping[str, Any]]:
        env = await self.call(self.contributor, "systems.list_devices", system_id=system_id)
        rows = ok(env, "systems.list_devices").data["devices"]
        assert isinstance(rows, list)
        return [row for row in rows if isinstance(row, dict)]

    async def teardown(self) -> None:
        for system_id in reversed(self.systems):
            await self.call(self.operator, "systems.teardown", system_id=system_id)
        for allocation_id in reversed(self.allocations):
            await self.call(self.operator, "allocations.release", allocation_id=allocation_id)
        if self.resource_id:
            await self.call(self.operator, "resources.deregister", resource_id=self.resource_id)


@asynccontextmanager
async def _cell(base_url: str, issuer: OidcIssuer, db_url: str) -> AsyncGenerator[_Cell]:
    cell = _Cell(base_url, issuer, db_url)
    try:
        await cell.setup("accounting.set_budget", project=cell.project, limit_kcu="1000000")
        await cell.setup(
            "accounting.set_quota",
            project=cell.project,
            max_concurrent_allocations=4,
            max_concurrent_systems=4,
            max_pending_allocations=0,
        )
        registered = await cell.setup(
            "resources.register",
            kind="fault-inject",
            name=f"fi-{cell.project}",
            cost_class="local",
            vcpus=8,
            memory_mb=8192,
            concurrent_allocation_cap=4,
        )
        cell.resource_id = str(registered.data["id"])
        yield cell
    finally:
        await cell.teardown()


async def _plug(cell: _Cell, system_id: str, kind: str) -> str:
    args: dict[str, object] = {"system_id": system_id, "kind": kind}
    if kind == "disk":
        args["size_gb"] = 1
    plugged = ok(await cell.call(cell.contributor, "systems.plug_device", **args), "plug")
    await cell.succeed(plugged, "systems.plug_device")
    return str(plugged.object_id)


async def _force(cell: _Cell, token: str, system_id: str, device_id: str) -> ToolResponse:
    return await cell.call(
        token, "systems.force_unplug_device", system_id=system_id, device_id=device_id
    )


async def _body(cell: _Cell) -> dict[str, object]:
    opted = await cell.provision(opt_in=True)
    plain = await cell.provision(opt_in=False)
    got = one(await cell.call(cell.contributor, "systems.get", system_id=opted))
    assert got.data["forced_unplug_devices"] == ["disk", "nic"], got.data

    # (a) denial without the profile opt-in: an admin is not enough.
    absent = str(uuid4())
    denied = await _force(cell, cell.admin, plain, absent)
    assert denied.error_category == "authorization_denied", denied.model_dump()
    assert denied.data["missing_checks"] == ["profile_opt_in"], denied.data

    # (b) denial for a contributor on the opted-in System: the role is missing, whatever the
    # device or state, so a made-up device id is denied rather than reported as not_found.
    outranked = await _force(cell, cell.contributor, opted, absent)
    assert outranked.error_category == "authorization_denied", outranked.model_dump()
    assert outranked.data["missing_checks"] == ["admin_role"], outranked.data

    # (c) a graceful un-plug cannot clear a hung device; the forced one does.
    nic = await _plug(cell, opted, "nic")
    await cell.behaviour("hang")
    graceful = ok(
        await cell.call(cell.contributor, "systems.unplug_device", system_id=opted, device_id=nic),
        "systems.unplug_device",
    )
    timed_out = await cell.settle(str(graceful.data["job_id"]))
    assert timed_out.status == "failed", timed_out.model_dump()
    assert [(d["device_id"], d["state"]) for d in await cell.devices(opted)] == [(nic, "failed")]
    forced = await _force(cell, cell.admin, opted, nic)
    assert forced.status == "running" and forced.data["forced"] is True, forced.model_dump()
    assert forced.data["state"] == "unplugging" and forced.data["kind"] == "nic"
    await cell.succeed(forced, "systems.force_unplug_device")
    assert await cell.devices(opted) == [], "the forced un-plug left the ledger row"

    # (d) a provider that refuses to force returns the row to where it started.
    await cell.behaviour(None)
    disk = await _plug(cell, opted, "disk")
    await cell.behaviour("no_force")
    refused = await _force(cell, cell.admin, opted, disk)
    assert refused.status == "running", refused.model_dump()
    failed = await cell.settle(str(refused.data["job_id"]))
    assert failed.status == "failed" and failed.error_category == "control_failure", (
        failed.model_dump()
    )
    assert "unplug_refused" in failed.model_dump_json(), failed.model_dump()
    assert [(d["device_id"], d["state"]) for d in await cell.devices(opted)] == [(disk, "attached")]

    # (e) the audit log holds each denial and each enqueue, with the documented arguments.
    assert await cell.audit(plain) == [
        (
            "systems.force_unplug_device",
            "force_unplug_device:denied",
            args_digest({"system_id": plain, "missing": ["profile_opt_in"]}),
        )
    ]
    assert await cell.audit(opted) == [
        (
            "systems.force_unplug_device",
            "force_unplug_device:denied",
            args_digest({"system_id": opted, "missing": ["admin_role"]}),
        ),
        (
            "systems.force_unplug_device",
            "force_unplug_device",
            args_digest(
                {"system_id": opted, "device_id": nic, "kind": "nic", "from_state": "failed"}
            ),
        ),
        (
            "systems.force_unplug_device",
            "force_unplug_device",
            args_digest(
                {"system_id": opted, "device_id": disk, "kind": "disk", "from_state": "attached"}
            ),
        ),
    ]

    await cell.behaviour(None)
    await cell.succeed(
        ok(
            await cell.call(
                cell.contributor, "systems.unplug_device", system_id=opted, device_id=disk
            ),
            "systems.unplug_device",
        ),
        "systems.unplug_device",
    )
    return {"opted": opted, "plain": plain, "nic": nic, "disk": disk}


def test_force_unplug_device_on_fault_inject() -> None:
    """Gate, force under ``hang``, refuse under ``no_force`` and audit, on a real stack."""
    issuer = require_issuer()
    base_url = require_stack()
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    if not db_url:
        pytest.fail('set KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"')

    async def run() -> None:
        async with _cell(base_url, issuer, db_url) as cell:
            await _body(cell)

    asyncio.run(run())
