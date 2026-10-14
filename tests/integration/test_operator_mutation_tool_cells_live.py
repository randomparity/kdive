"""Prove the mutating operator MCP tool cells over HTTP (#3110, ADR-0722).

``live_stack``-marked. One parameter per contract cell of the fourteen tools owner group 3110
holds, framed by :func:`~tests.integration.live_stack.tool_cells.run_tool_cell` like #2812's
read-only operator cells. Each functional body seeds what its tool needs through the API or the
harness login's seeding writes (ADR-0769), observes the effect in the evidence database, the
object store, the configured ``systems.toml`` or the authority daemon, and restores what it
changed. Cell-owned runtime hosts are local-libvirt rows whose URI names the server's own
listener; nothing is provisioned on them. ``ops.reconcile_now`` and ``resources.drain`` run inside
the authority-owned System frame on an inner run, ``images.prune_expired`` inside the catalog
System frame. The snapshot adds the platform rows a cell targets to the per-project one.
``docs/operating/runbooks/live-testing.md`` covers the lane and the run.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, LiteralString, cast
from urllib.parse import urlsplit

import psycopg
import pytest

from kdive.diagnostics.contracts import WORKER_UNAVAILABLE_DETAIL
from kdive.diagnostics.contributions.multiarch_gdb import (
    DEPMOD_TOOLCHAIN_ID,
    GUEST_ARCH_ACCEL_ID,
    MULTIARCH_GDB_ID,
    PSERIES_FADUMP_ID,
)
from kdive.domain.accounting.cost import KCU_QUANTUM
from kdive.domain.errors import ErrorCategory
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.model import InventoryDoc
from kdive.inventory.path import systems_toml_path
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.reconciler.loop import DEFAULT_INTERVAL
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.authority_lane import (
    AuthoritySystem,
    authority_inventory,
    on_authority_system,
)
from tests.integration.live_stack.catalog import (
    _cleaning,
    _operator,
    _register,
    _release,
    _rows,
    _uploading,
    _versions,
)
from tests.integration.live_stack.scenario import CellRun, ScenarioStop, on_catalog_system
from tests.integration.live_stack.spine import seed_metering
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Exposure,
    Grants,
    HttpCaller,
    Rejection,
    boundary_of,
    lane_image,
    one,
    project_state,
    prove_functional,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

TOOLS = (
    "images.extend",
    "images.prune_expired",
    "inventory.clear_override",
    "ops.reconcile_now",
    "ops.reconcile_systems",
    "ops.set_cost_class_coeff",
    "ops.set_host_capacity",
    "ops.set_queue_paused",
    "resources.deregister",
    "resources.drain",
    "resources.register",
    "resources.renew",
    "resources.set_scheduling",
    "resources.set_status",
)
_ADMIN_TOOLS = frozenset(
    {
        "images.extend",
        "images.prune_expired",
        "inventory.clear_override",
        "ops.reconcile_systems",
        "resources.deregister",
        "resources.register",
        "resources.renew",
    }
)
_OPERATOR = "platform_operator"
_ADMIN = "platform_admin"
_AUDITOR = "platform_auditor"
_ABSENT_ID = "00000000-0000-4000-8000-000000000000"
# The cell-owned class: no file declares it, so the reconciler never re-asserts it, and no tool
# deletes a coefficient, so the row persists across runs (spec: Failure model).
COST_CLASS = "cov-tool-cells"
_ATTEMPTS = 3
_INTERVAL_S = DEFAULT_INTERVAL.total_seconds()
_POLL_S = 2.0
_CLAIM_DEADLINE_S = 120.0
_HOST_CAPS = {"vcpus": 2, "memory_mb": 2048}
_PROBE = {"vcpus": 1, "memory_gb": 1, "disk_gb": 1}
_LIVE = ("granted", "active", "releasing")
# The local worker-vantage diagnostics, which run only as a claimed worker job.
_WORKER_CHECKS = frozenset(
    {MULTIARCH_GDB_ID, PSERIES_FADUMP_ID, GUEST_ARCH_ACCEL_ID, DEPMOD_TOOLCHAIN_ID}
)
_REASON = "coverage cell #3110"
Rows = list[dict[str, Any]]


def _fresh() -> str:
    return f"cov-{secrets.token_hex(4)}"


def _platform(project: str, *roles: str) -> Grants:
    return Grants(
        f"{project}-{'-'.join(roles) or 'viewer'}", (project,), {project: "viewer"}, roles
    )


def _functional_grants(tool: str, project: str) -> Grants:
    if tool == "resources.drain":
        return _platform(project, _OPERATOR, _ADMIN)
    return _platform(project, _ADMIN if tool in _ADMIN_TOOLS else _OPERATOR)


def _direct(caller: HttpCaller) -> HttpCaller:
    """A ``direct`` caller for setup, placement and restore calls beside the cell's exposure."""
    return HttpCaller("direct", caller.base_url, caller.issuer)


async def _one_row(db_url: str, query: LiteralString, params: tuple[object, ...]) -> dict[str, Any]:
    rows = await _rows(db_url, query, params)
    assert len(rows) == 1, f"expected one row, found {len(rows)}"
    return rows[0]


async def _maybe_row(
    db_url: str, query: LiteralString, params: tuple[object, ...]
) -> dict[str, Any] | None:
    rows = await _rows(db_url, query, params)
    return rows[0] if rows else None


async def _db_now(db_url: str) -> datetime:
    return cast(datetime, (await _one_row(db_url, "SELECT now() AS t", ()))["t"])


async def _write(db_url: str, query: LiteralString, params: tuple[object, ...]) -> Rows:
    """One ADR-0769 seeding write through the harness login."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall() if cursor.description else []
    return [dict(zip([c.name for c in cursor.description or []], r, strict=True)) for r in rows]


# -- snapshot ---------------------------------------------------------------------------------

_HOST_ROWS: LiteralString = (
    "SELECT id, kind, name, status, cordoned, cost_class, managed_by, owner_project, "
    "capabilities->'concurrent_allocation_cap' AS cap FROM resources "
    "WHERE managed_by <> 'runtime' ORDER BY id"
)
_OWNED_HOSTS: LiteralString = (
    "SELECT id, kind, name, status, cordoned, cost_class, capabilities, host_uri, managed_by, "
    "lease_expires_at FROM resources WHERE owner_project = %s ORDER BY id"
)


async def _state(db_url: str, project: str) -> dict[str, object]:
    """``project_state`` plus the platform rows a cell may touch and must leave as found.

    Declared and discovered hosts (scheduling, health, cap), override entries, the queue flag,
    every coefficient but the cell-owned class, and the runtime hosts and private images that
    ``project`` owns.
    """
    state: dict[str, object] = dict(await project_state(db_url, project))
    state["hosts"] = await _rows(db_url, _HOST_ROWS)
    state["overrides"] = await _rows(
        db_url,
        "SELECT source_kind, resource_kind, name, disposition FROM inventory_overrides "
        "ORDER BY source_kind, resource_kind, name",
    )
    state["queue"] = await _rows(db_url, "SELECT queue_paused FROM ops_control")
    state["coefficients"] = await _rows(
        db_url,
        "SELECT cost_class, coeff FROM cost_class_coefficients WHERE cost_class <> %s "
        "ORDER BY cost_class",
        (COST_CLASS,),
    )
    state["owned_hosts"] = await _rows(db_url, _OWNED_HOSTS, (project,))
    state["owned_images"] = await _rows(
        db_url,
        "SELECT id, name, state, expires_at, object_key FROM image_catalog WHERE owner = %s "
        "ORDER BY id",
        (project,),
    )
    return state


# -- cell-owned runtime hosts ------------------------------------------------------------------


def listener_uri(base_url: str) -> str:
    """A libvirt-shaped URI naming the server's own TCP listener (the register preflight)."""
    parts = urlsplit(base_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"qemu+tcp://{parts.hostname}:{port}/system"


async def _lane_cost_class(db_url: str) -> str:
    row = await _maybe_row(
        db_url,
        "SELECT cost_class FROM resources WHERE kind = 'local-libvirt' "
        "AND managed_by <> 'runtime' ORDER BY created_at LIMIT 1",
        (),
    )
    if row is None:
        raise ScenarioStop(Outcome.BLOCKED, "the stack has no local-libvirt host to price from")
    return str(row["cost_class"])


def host_args(base_url: str, owner: str, cost_class: str) -> dict[str, object]:
    """``resources.register`` arguments for a cap-2 runtime host owned by ``owner``."""
    return {
        "kind": "local-libvirt",
        "name": _fresh(),
        "cost_class": cost_class,
        "concurrent_allocation_cap": 2,
        "host_uri": listener_uri(base_url),
        "owner_project": owner,
        **_HOST_CAPS,
    }


_HOST: LiteralString = "SELECT * FROM resources WHERE id = %s"
_ALLOCATIONS_OF: LiteralString = "SELECT id FROM allocations WHERE project = %s"
_HELD_BY: LiteralString = "SELECT id FROM allocations WHERE resource_id = %s"
_STATE_OF: LiteralString = "SELECT state FROM allocations WHERE id = %s"
_ALLOCATION: LiteralString = "SELECT * FROM allocations WHERE id = %s"


@dataclass
class Hosts:
    """The cell's runtime hosts and the allocations it placed; :meth:`remove` undoes both."""

    caller: HttpCaller
    db_url: str
    ids: list[str] = field(default_factory=list)
    placed: dict[str, str] = field(default_factory=dict)  # allocation id -> project

    def _admin(self, project: str) -> str:
        return self.caller.token(_platform(project, _ADMIN))

    async def add(self, owner: str, *, cost_class: str | None = None) -> dict[str, Any]:
        """Register a runtime host owned by ``owner``; return its row."""
        cost_class = cost_class or await _lane_cost_class(self.db_url)
        args = host_args(self.caller.base_url, owner, cost_class)
        env = one(await self.caller.call("resources.register", args, self._admin(owner)))
        self.ids.append(env.object_id)
        return await _one_row(self.db_url, _HOST, (env.object_id,))

    async def place(self, project: str, host_id: str) -> ToolResponse:
        """The placement probe: request ``host_id`` in ``project``; a grant is tracked."""
        token = self.caller.token(Grants(f"{project}-placer", (project,), {project: "contributor"}))
        args = {"project": project, "resource": {"mode": "id", "resource_id": host_id}, **_PROBE}
        result = await self.caller.call("allocations.request", args, token)
        assert isinstance(result, ToolResponse), "allocations.request answered a list"
        if result.error_category is None:
            self.placed[result.object_id] = project
        return result

    async def unplaceable(self, project: str, host_id: str) -> str:
        """Require the probe to find no schedulable host and to write no allocation.

        An explicit host that is cordoned (or not ``available``) yields no placement candidate,
        which admission answers ``configuration_error``; a capacity, budget or quota denial is
        ``allocation_denied`` or ``quota_exceeded`` and fails this check.
        """
        before = await _rows(self.db_url, _ALLOCATIONS_OF, (project,))
        result = await self.place(project, host_id)
        assert result.error_category == ErrorCategory.CONFIGURATION_ERROR.value, (
            f"placement on {host_id} answered {result.status} {result.error_category}"
        )
        assert await _rows(self.db_url, _ALLOCATIONS_OF, (project,)) == before
        return "no-candidate"

    async def granted(self, project: str, host_id: str) -> str:
        """Require the probe to be granted on ``host_id``; return the allocation id."""
        result = await self.place(project, host_id)
        assert result.status == "granted", f"placement answered {result.status}"
        row = await _one_row(self.db_url, _ALLOCATION, (result.object_id,))
        assert str(row["resource_id"]) == host_id, "the grant landed on another host"
        return result.object_id

    async def release(self, allocation_id: str) -> list[str]:
        project = self.placed.pop(allocation_id)
        token = self.caller.token(Grants(f"{project}-placer", (project,), {project: "contributor"}))
        return await _release(self.caller, token, self.db_url, allocation_id)

    async def remove(self) -> list[str]:
        """Release every tracked placement, then deregister and check every host."""
        problems: list[str] = []
        for allocation_id in list(self.placed):
            problems += await self.release(allocation_id)
        for host_id in self.ids:
            problems += await self._deregister(host_id)
        return problems

    async def _deregister(self, host_id: str) -> list[str]:
        """Deregister ``host_id`` and require its lifecycle disposition (ADR-0112).

        A host that never held an allocation is deleted; one that did keeps its row for the
        retained allocations' foreign key, cordoned with its lease cleared.
        """
        row = await _maybe_row(self.db_url, _HOST, (host_id,))
        if row is None:
            return []  # the body deregistered it
        token = self._admin(str(row["owner_project"]))
        result = await self.caller.call("resources.deregister", {"resource_id": host_id}, token)
        after = await _maybe_row(self.db_url, _HOST, (host_id,))
        allocated = await _rows(self.db_url, _HELD_BY, (host_id,))
        if after is None and not allocated:
            return []
        if after is not None and allocated and after["cordoned"] and not after["lease_expires_at"]:
            return []
        return [f"deregister of {host_id} answered {getattr(result, 'status', result)}"]

    def owned(self) -> list[str]:
        return [*self.ids]


@asynccontextmanager
async def _hosts(caller: HttpCaller, db_url: str) -> AsyncIterator[Hosts]:
    hosts = Hosts(_direct(caller), db_url)
    async with _cleaning(hosts.remove, "the cell's runtime hosts"):
        yield hosts


async def _workload(db_url: str) -> str:
    """A fresh metered project for placements; its rows are history, never snapshot."""
    project = _fresh()
    await seed_metering(db_url, project)
    return project


async def _attributed(
    attempt: Callable[[bool], Awaitable[dict[str, object] | None]],
) -> dict[str, object]:
    """Run ``attempt`` until the cell's own call made the repair; its observation.

    The reconciler loop runs the same repairs every 30 s and may reach a fresh seed first; such
    an attempt returns ``None`` and is never counted as the tool's effect. ``attempt`` gets
    whether it is the first, which discovers the tool through the gateway.
    """
    for count in range(1, _ATTEMPTS + 1):
        observed = await attempt(count == 1)
        if observed is not None:
            return {**observed, "attempts": count}
    raise AssertionError(f"the reconciler loop repaired the seed first {_ATTEMPTS} times")


def _restorer(grants: Grants) -> Grants:
    """``grants`` under a subject of its own, for restore calls beside the call under test."""
    return replace(grants, subject=f"{grants.subject}-restore")


def _restoring(
    action: Callable[[], Awaitable[object]], what: str
) -> AbstractAsyncContextManager[None]:
    """Run ``action`` on exit, success or failure; its own failure never hides the body's.

    A tool answer ``action`` returns must be a success: a refused restore is a cleanup problem.
    """

    async def restore() -> list[str]:
        result = await action()
        if isinstance(result, (ToolResponse, list)):
            one(cast(ToolResponse | list[ToolResponse], result))
        return []

    return _cleaning(restore, what)


def _same_except(before: Mapping[str, Any], after: Mapping[str, Any], *keys: str) -> None:
    ignored = {"updated_at", *keys}
    changed = sorted(k for k in before if k not in ignored and before[k] != after[k])
    assert not changed, f"unrelated host columns changed: {changed}"


# -- resource tools -----------------------------------------------------------------------------


async def _register_body(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    project = grants.projects[0]
    async with _hosts(caller, db_url) as hosts:
        cost_class = await _lane_cost_class(db_url)
        args = host_args(caller.base_url, project, cost_class)
        env = one(
            await caller.call("resources.register", args, caller.token(grants), discover=True)
        )
        hosts.ids.append(env.object_id)
        assert env.status == "registered", f"resources.register answered {env.status}"
        row = await _one_row(db_url, _HOST, (env.object_id,))
        expected = {
            "kind": args["kind"],
            "name": args["name"],
            "managed_by": "runtime",
            "owner_project": project,
            "status": "available",
            "cordoned": False,
            "host_uri": args["host_uri"],
            "cost_class": cost_class,
            "capabilities": {**_HOST_CAPS, "concurrent_allocation_cap": 2},
        }
        observed = {k: row[k] for k in expected}
        differs = sorted(k for k in expected if observed[k] != expected[k])
        assert not differs, f"the registered row differs in {differs}"
        assert row["lease_expires_at"] > await _db_now(db_url), "the lease is not in the future"
    return {"registered": sorted(expected), "owned": hosts.owned()}


async def _deregister_body(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    workload = await _workload(db_url)
    token = caller.token(grants)
    async with _hosts(caller, db_url) as hosts:
        idle, busy = await hosts.add(workload), await hosts.add(workload)
        held = await hosts.granted(workload, str(busy["id"]))
        refused = await caller.call(
            "resources.deregister", {"resource_id": str(busy["id"])}, token, discover=True
        )
        assert isinstance(refused, ToolResponse), "resources.deregister answered a list"
        assert refused.error_category == ErrorCategory.CONFLICT.value, refused.error_category
        assert await _one_row(db_url, _HOST, (str(busy["id"]),)) == busy, "a refusal changed it"
        state = await _one_row(db_url, _STATE_OF, (held,))
        assert state["state"] == "granted", "the refused deregister touched the allocation"
        env = one(
            await caller.call("resources.deregister", {"resource_id": str(idle["id"])}, token)
        )
        assert (env.status, env.data.get("disposition")) == ("deregistered", "deleted")
        assert await _maybe_row(db_url, _HOST, (str(idle["id"]),)) is None, "the row remains"
        assert await _one_row(db_url, _HOST, (str(busy["id"]),)) == busy, "the other host changed"
        owned = [*hosts.owned(), held]
    return {"live_refused": "conflict", "idle": "deleted", "other_unchanged": True, "owned": owned}


async def _renew_body(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    async with _hosts(caller, db_url) as hosts:
        before = await hosts.add(grants.projects[0])
        await asyncio.sleep(1.1)
        env = one(
            await caller.call(
                "resources.renew",
                {"resource_id": str(before["id"])},
                caller.token(grants),
                discover=True,
            )
        )
        after = await _one_row(db_url, _HOST, (str(before["id"]),))
        returned = datetime.fromisoformat(str(env.data["lease_expires_at"]))
        assert env.status == "renewed" and after["lease_expires_at"] == returned
        assert returned > before["lease_expires_at"], "the lease did not move forward"
        _same_except(before, after, "lease_expires_at")
    return {
        "lease_extended_s": (returned - before["lease_expires_at"]).total_seconds(),
        "owned": hosts.owned(),
    }


async def _set_status_body(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    token = caller.token(grants)
    async with _hosts(caller, db_url) as hosts:
        host = await hosts.add(grants.projects[0])
        host_id = str(host["id"])
        operator = hosts.caller.token(_platform(grants.projects[0], _OPERATOR))
        args = {"resource_id": host_id, "state": "cordoned"}
        one(await hosts.caller.call("resources.set_scheduling", args, operator))
        seen = []
        for status in ("degraded", "offline", "available"):
            args = {"resource_id": host_id, "status": status}
            env = one(await caller.call("resources.set_status", args, token, discover=not seen))
            row = await _one_row(db_url, _HOST, (host_id,))
            assert env.object_id == host_id and (row["status"], row["cordoned"]) == (status, True)
            seen.append(status)
    return {"statuses": seen, "cordoned_throughout": True, "owned": hosts.owned()}


async def _set_scheduling_body(
    caller: HttpCaller, grants: Grants, *, db_url: str
) -> dict[str, object]:
    workload = await _workload(db_url)
    token = caller.token(grants)
    async with _hosts(caller, db_url) as hosts:
        host_id = str((await hosts.add(workload))["id"])
        args = {"resource_id": host_id, "state": "cordoned"}
        one(await caller.call("resources.set_scheduling", args, token, discover=True))
        row = await _one_row(db_url, _HOST, (host_id,))
        assert (row["cordoned"], row["status"]) == (True, "available"), "cordon changed health"
        refused = await hosts.unplaceable(workload, host_id)
        args = {"resource_id": host_id, "state": "schedulable"}
        one(await caller.call("resources.set_scheduling", args, token))
        row = await _one_row(db_url, _HOST, (host_id,))
        assert (row["cordoned"], row["status"]) == (False, "available"), "uncordon changed health"
        placed = await hosts.granted(workload, host_id)
        owned = [*hosts.owned(), placed]
    return {"cordoned_probe": refused, "schedulable_probe": "granted", "owned": owned}


async def _set_capacity_body(
    caller: HttpCaller, grants: Grants, *, db_url: str
) -> dict[str, object]:
    workload = await _workload(db_url)
    token = caller.token(grants)
    async with _hosts(caller, db_url) as hosts:
        host_id = str((await hosts.add(workload))["id"])
        first = await hosts.granted(workload, host_id)
        denial: dict[str, object] = {}
        for cap in (1, 2):
            args = {"resource_id": host_id, "concurrent_allocation_cap": cap}
            env = one(await caller.call("ops.set_host_capacity", args, token, discover=cap == 1))
            row = await _one_row(db_url, _HOST, (host_id,))
            assert env.data.get("concurrent_allocation_cap") == cap
            assert row["capabilities"]["concurrent_allocation_cap"] == cap, "cap not stored"
            if cap == 1:
                result = await hosts.place(workload, host_id)
                denial = {k: result.data.get(k) for k in ("reason", "cap", "in_use")}
                assert result.error_category == ErrorCategory.ALLOCATION_DENIED.value
                assert denial == {"reason": "at_capacity", "cap": "1", "in_use": "1"}, denial
            else:
                await hosts.granted(workload, host_id)
        state = await _one_row(db_url, _STATE_OF, (first,))
        assert state["state"] == "granted", "lowering the cap touched the existing allocation"
        owned = [*hosts.owned(), *hosts.placed]
    return {"cap_1": denial, "cap_2": "granted", "existing": "granted", "owned": owned}


async def _coefficient(db_url: str) -> Decimal | None:
    row = await _maybe_row(
        db_url, "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s", (COST_CLASS,)
    )
    return None if row is None else Decimal(row["coeff"])


async def _reserved(db_url: str, allocation_id: str) -> Decimal:
    row = await _one_row(
        db_url,
        "SELECT cost_class, kcu_delta FROM ledger WHERE allocation_id = %s "
        "AND event_type = 'reserved'",
        (allocation_id,),
    )
    assert row["cost_class"] == COST_CLASS, "the charge used another class"
    return Decimal(row["kcu_delta"])


async def _set_coeff_body(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    workload = await _workload(db_url)
    token = caller.token(grants)
    prior = await _coefficient(db_url)
    charges: dict[int, Decimal] = {}
    actor = _direct(caller)
    restore = {"cost_class": COST_CLASS, "coeff": str(prior or 1)}
    put_back = partial(
        actor.call, "ops.set_cost_class_coeff", restore, actor.token(_restorer(grants))
    )
    async with _restoring(put_back, "the coefficient"), _hosts(caller, db_url) as hosts:
        host_id = str((await hosts.add(workload, cost_class=COST_CLASS))["id"])
        for coeff in (1, 2):
            args = {"cost_class": COST_CLASS, "coeff": str(coeff)}
            env = one(
                await caller.call("ops.set_cost_class_coeff", args, token, discover=coeff == 1)
            )
            assert Decimal(str(env.data["coeff"])) == coeff == await _coefficient(db_url)
            charges[coeff] = await _reserved(db_url, await hosts.granted(workload, host_id))
        owned = [*hosts.owned(), *hosts.placed]
    assert await _coefficient(db_url) == (prior or 1), "the coefficient was not restored"
    assert charges[1] > 0 and abs(charges[2] - 2 * charges[1]) <= KCU_QUANTUM, charges
    return {
        "reserved": {k: str(v) for k, v in charges.items()},
        "restored": str(prior or 1),
        "owned": owned,
    }


# -- queue, override and inventory reconcile ---------------------------------------------------


async def _queue_paused(db_url: str) -> bool:
    return bool(
        (await _one_row(db_url, "SELECT queue_paused FROM ops_control", ()))["queue_paused"]
    )


async def _unavailable(caller: HttpCaller, token: str) -> list[str]:
    """Run ``ops.diagnostics``; the checks that reported the worker unavailable."""
    env = one(await caller.call("ops.diagnostics", {}, token))
    return sorted(
        str(i.data["check"]) for i in env.items if i.data.get("detail") == WORKER_UNAVAILABLE_DETAIL
    )


# One worker-check job per provider; follow the local one.
_NEWEST_CHECK: LiteralString = (
    "SELECT id, state, worker_id FROM jobs WHERE kind = 'diagnostics_worker_check' "
    "AND payload->>'provider' = 'local-libvirt' AND created_at >= %s "
    "ORDER BY created_at DESC LIMIT 1"
)
_TERMINAL_JOBS = frozenset({"succeeded", "failed", "canceled"})


async def _resume(caller: HttpCaller, token: str, db_url: str) -> None:
    if await _queue_paused(db_url):
        one(await caller.call("ops.set_queue_paused", {"paused": False}, token))


async def _queue_body(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    if await _queue_paused(db_url):
        raise ScenarioStop(Outcome.BLOCKED, "the queue is already paused; resume it first")
    token, actor = caller.token(grants), _direct(caller)
    operator = actor.token(_restorer(grants))
    async with _restoring(partial(_resume, actor, operator, db_url), "the queue pause"):
        start = await _db_now(db_url)
        env = one(await caller.call("ops.set_queue_paused", {"paused": True}, token, discover=True))
        assert env.status == "paused" and await _queue_paused(db_url)
        stalled = await _unavailable(actor, operator)
        assert _WORKER_CHECKS.issubset(stalled), f"paused, only {stalled} were held"
        job = await _one_row(db_url, _NEWEST_CHECK, (start,))
        assert (job["state"], job["worker_id"]) == ("queued", None), f"job was {job['state']}"
        env = one(await caller.call("ops.set_queue_paused", {"paused": False}, token))
        assert env.status == "running" and not await _queue_paused(db_url)
        deadline = time.monotonic() + _CLAIM_DEADLINE_S
        query: LiteralString = "SELECT state, worker_id FROM jobs WHERE id = %s"
        while (row := await _one_row(db_url, query, (job["id"],)))["state"] not in _TERMINAL_JOBS:
            assert time.monotonic() < deadline, f"the held job stayed {row['state']}"
            await asyncio.sleep(_POLL_S)
        assert row["state"] == "succeeded" and row["worker_id"], f"the held job {row['state']}"
        resumed = await _unavailable(actor, operator)
        assert not _WORKER_CHECKS & set(resumed), f"resumed, {resumed} still held"
    return {
        "paused_checks_unavailable": stalled,
        "held_job": "claimed-after-resume",
        "resumed": "ran",
    }


@dataclass(frozen=True)
class _Declared:
    kind: str
    name: str
    cap: int
    resource_id: str


async def _declared_host(db_url: str) -> _Declared:
    """The first declared config host the reconciler owns at its file cap, with no override."""
    doc = load_inventory_optional(systems_toml_path())
    entries = [
        *(
            ("fault-inject", e.name, e.concurrent_allocation_cap)
            for e in (doc.fault_inject if doc else [])
        ),
        *(
            ("remote-libvirt", e.name, e.concurrent_allocation_cap)
            for e in (doc.remote_libvirt if doc else [])
        ),
    ]
    for kind, name, cap in entries:
        row = await _maybe_row(
            db_url,
            "SELECT r.id, r.capabilities->'concurrent_allocation_cap' AS cap FROM resources r "
            "WHERE r.kind = %s AND r.name = %s AND r.managed_by = 'config' AND NOT EXISTS ("
            "SELECT 1 FROM inventory_overrides o "
            "WHERE o.resource_kind = r.kind AND o.name = r.name)",
            (kind, name),
        )
        if row is not None and row["cap"] == cap:
            return _Declared(kind, name, cap, str(row["id"]))
    raise ScenarioStop(
        Outcome.BLOCKED,
        "the exported KDIVE_SYSTEMS_TOML declares no [[fault_inject]] or [[remote_libvirt]] host "
        "that is config-owned at its file cap without an override entry",
    )


_OVERRIDE: LiteralString = (
    "SELECT disposition FROM inventory_overrides WHERE source_kind = 'resource' "
    "AND resource_kind = %s AND name = %s"
)
_CAP_OF: LiteralString = (
    "SELECT capabilities->'concurrent_allocation_cap' AS cap FROM resources WHERE id = %s"
)


async def _until_cap(db_url: str, resource_id: str, cap: int, deadline_s: float) -> float:
    """Wait for the reconciler to set ``cap``; the seconds it took."""
    start = time.monotonic()
    while (await _one_row(db_url, _CAP_OF, (resource_id,)))["cap"] != cap:
        assert time.monotonic() - start < deadline_s, f"the cap did not return to {cap}"
        await asyncio.sleep(_POLL_S)
    return round(time.monotonic() - start, 1)


async def _clear_override_body(
    caller: HttpCaller, grants: Grants, *, db_url: str
) -> dict[str, object]:
    host = await _declared_host(db_url)
    actor = _direct(caller)
    operator = actor.token(_platform(grants.projects[0], _OPERATOR))
    identity = {"resource_kind": host.kind, "name": host.name}

    async def put_back() -> None:
        if await _rows(db_url, _OVERRIDE, (host.kind, host.name)):
            restore = actor.token(_restorer(grants))
            one(await actor.call("inventory.clear_override", identity, restore))
        await _until_cap(db_url, host.resource_id, host.cap, 3 * _INTERVAL_S)

    async with _restoring(put_back, "the declared host's cap"):
        args = {"resource_id": host.resource_id, "concurrent_allocation_cap": host.cap + 1}
        one(await actor.call("ops.set_host_capacity", args, operator))
        assert await _one_row(db_url, _OVERRIDE, (host.kind, host.name)) == {
            "disposition": "detached"
        }
        await asyncio.sleep(_INTERVAL_S + 5)
        held = (await _one_row(db_url, _CAP_OF, (host.resource_id,)))["cap"]
        assert held == host.cap + 1, "a reconcile overwrote the detached value"
        token = caller.token(grants)
        env = one(await caller.call("inventory.clear_override", identity, token, discover=True))
        assert env.status == "cleared" and not await _rows(
            db_url, _OVERRIDE, (host.kind, host.name)
        )
        restored_s = await _until_cap(db_url, host.resource_id, host.cap, 3 * _INTERVAL_S)
    return {"held_detached": held, "file_cap": host.cap, "restored_after_s": restored_s}


_DEPARTED: LiteralString = (
    "INSERT INTO resources (kind, name, capabilities, pool, cost_class, status, host_uri, "
    "managed_by) VALUES ('fault-inject', %s, %s::jsonb, 'default', %s, 'available', %s, "
    "'config')"
)
_DEPARTED_CAPS = '{"vcpus": 1, "memory_mb": 1024, "concurrent_allocation_cap": 1}'
_CONFIG_HOSTS: LiteralString = "SELECT kind, name FROM resources WHERE managed_by = 'config'"
_COEFFICIENTS: LiteralString = "SELECT cost_class, coeff FROM cost_class_coefficients"
_CONFIG_IMAGES: LiteralString = (
    "SELECT provider, name FROM image_catalog WHERE managed_by = 'config'"
)


async def _prune_departed(
    caller: HttpCaller, token: str, db_url: str, discover: bool
) -> dict[str, object] | None:
    """Seed an undeclared config host and reconcile; ``None`` when the loop pruned it first."""
    name = f"{_fresh()}-departed"
    seed = (name, _DEPARTED_CAPS, await _lane_cost_class(db_url), f"fault-inject://{name}")
    await _write(db_url, _DEPARTED, seed)
    env = one(await caller.call("ops.reconcile_systems", {}, token, discover=discover))
    assert not await _rows(db_url, "SELECT id FROM resources WHERE name = %s", (name,))
    pruned = name in str(env.data.get("pruned_names", "")).split(",")
    return {"pruned": "departed seed", "diff": dict(env.data)} if pruned else None


async def _converged(db_url: str, doc: InventoryDoc) -> int:
    """Require a config row for every declared host and image, and each file coefficient.

    An image row may stay ``defined`` when its source cannot be realized; the pass still owns it.
    """
    declared = {
        *(("fault-inject", e.name) for e in doc.fault_inject),
        *(("remote-libvirt", e.name) for e in doc.remote_libvirt),
    }
    hosts = {(r["kind"], r["name"]) for r in await _rows(db_url, _CONFIG_HOSTS)}
    assert declared <= hosts, f"declared hosts missing: {sorted(declared - hosts)}"
    coefficients = {r["cost_class"]: r["coeff"] for r in await _rows(db_url, _COEFFICIENTS)}
    assert all(coefficients.get(c.name) == c.coeff for c in doc.cost_class), "a coefficient differs"
    images = {(r["provider"], r["name"]) for r in await _rows(db_url, _CONFIG_IMAGES)}
    assert {(i.provider, i.name) for i in doc.image} <= images, "a declared image is missing"
    return len(declared)


async def _reconcile_systems_body(
    caller: HttpCaller, grants: Grants, *, db_url: str
) -> dict[str, object]:
    doc = load_inventory_optional(systems_toml_path())
    if doc is None:
        raise ScenarioStop(Outcome.BLOCKED, "export KDIVE_SYSTEMS_TOML naming the lane's file")
    token = caller.token(grants)
    async with _hosts(caller, db_url) as hosts:
        kept = await hosts.add(grants.projects[0])
        observed = await _attributed(partial(_prune_departed, caller, token, db_url))
        assert await _one_row(db_url, _HOST, (str(kept["id"]),)) == kept, "a runtime host changed"
        declared = await _converged(db_url, doc)
    return {**observed, "runtime_kept": True, "declared_hosts": declared, "owned": hosts.owned()}


# -- images ------------------------------------------------------------------------------------

_IMAGE: LiteralString = (
    "SELECT name, owner, visibility, state, digest, object_key, expires_at FROM image_catalog "
    "WHERE id = %s"
)


async def _extend_body(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    project = grants.projects[0]
    actor = _direct(caller)
    async with _uploading(caller.base_url, caller.issuer, db_url, project, name="cov-extend") as up:
        image_id = await _register(
            actor, actor.token(_operator(project)), up, db_url, lifetime_s=600
        )
        before = await _one_row(db_url, _IMAGE, (image_id,))
        stored = await asyncio.to_thread(_versions, str(before["object_key"]))
        start = await _db_now(db_url)
        args = {"image_id": image_id, "seconds": 3600, "reason": _REASON}
        env = one(await caller.call("images.extend", args, caller.token(grants), discover=True))
        end = await _db_now(db_url)
        after = await _one_row(db_url, _IMAGE, (image_id,))
        returned = datetime.fromisoformat(str(env.data["expires_at"]))
        window = timedelta(seconds=3600)
        assert env.status == "extended" and after["expires_at"] == returned
        assert start + window <= returned <= end + window, "the expiry is not now + 3600 s"
        _same_except(before, after, "expires_at")
        assert await asyncio.to_thread(_versions, str(after["object_key"])) == stored
        owned = [image_id, *sorted(up.object_keys)]
    moved = (returned - before["expires_at"]).total_seconds()
    return {"rearmed_s": 3600, "moved_s": moved, "contents_unchanged": True, "owned": owned}


async def _expired(db_url: str, image_id: str) -> None:
    """Wait, briefly, until the database clock has passed ``image_id``'s expiry."""
    for _ in range(20):
        if not await _rows(db_url, _IMAGE + " AND expires_at >= now()", (image_id,)):
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"image {image_id} did not expire")


_EXPIRED_PRIVATE: LiteralString = (
    "SELECT id FROM image_catalog WHERE visibility = 'private' AND state <> 'pending' "
    "AND expires_at < now()"
)


async def _prune_once(
    caller: HttpCaller, token: str, db_url: str, referenced: str, discover: bool
) -> dict[str, object] | None:
    """Upload an expiring image and prune; ``None`` when the reconciler loop pruned it first.

    The answer carries only a count, so the call owns the prune when, just before it, the
    expired private images are exactly the new one and the ``referenced`` one, and it prunes one.
    """
    project, actor = _fresh(), _direct(caller)
    async with _uploading(caller.base_url, caller.issuer, db_url, project, name="cov-prune") as up:
        image_id = await _register(actor, actor.token(_operator(project)), up, db_url, lifetime_s=1)
        key = str((await _one_row(db_url, _IMAGE, (image_id,)))["object_key"])
        await _expired(db_url, image_id)
        expired = {str(r["id"]) for r in await _rows(db_url, _EXPIRED_PRIVATE)}
        assert expired <= {image_id, referenced}, "other expired private images are present"
        args = {"reason": _REASON}
        env = one(await caller.call("images.prune_expired", args, token, discover=discover))
        assert await _maybe_row(db_url, _IMAGE, (image_id,)) is None, "the image survived"
        if image_id not in expired or str(env.data.get("pruned")) != "1":
            return None
        assert not await asyncio.to_thread(_versions, key), "the pruned image's bytes remain"
        return {"pruned": 1, "expired_unreferenced": "pruned"}


async def _prune_body(
    caller: HttpCaller, grants: Grants, *, run: CellRun, db_url: str
) -> dict[str, object]:
    name, _entry = lane_image()
    token, actor = caller.token(grants), _direct(caller)
    shadow_project, live_project, frame_project = _fresh(), _fresh(), _fresh()
    seen: dict[str, object] = {}
    inner = CellRun(run.cell, run.writer)

    async def body(_op: LiveStackClient, _system_id: str, _owned: list[str]) -> None:
        shadow = await _register(
            actor, actor.token(_operator(shadow_project)), shadowed, db_url, lifetime_s=1
        )
        await _expired(db_url, shadow)
        async with _uploading(
            caller.base_url, caller.issuer, db_url, live_project, name="cov-live"
        ) as live_up:
            live = await _register(actor, actor.token(_operator(live_project)), live_up, db_url)
            seen.update(await _attributed(partial(_prune_once, caller, token, db_url, shadow)))
            for kept, why in ((shadow, "referenced"), (live, "unexpired")):
                assert await _maybe_row(db_url, _IMAGE, (kept,)), f"the {why} image was pruned"
            seen["kept"] = ["referenced", "unexpired"]
        # Re-arm the shadow: once the frame tears the System down it is expired and unreferenced,
        # and the loop's prune would race its owner's delete.
        args = {"image_id": shadow, "seconds": 3600, "reason": _REASON}
        one(await actor.call("images.extend", args, actor.token(_restorer(grants))))

    try:
        async with _uploading(
            caller.base_url, caller.issuer, db_url, shadow_project, name=name
        ) as shadowed:
            await on_catalog_system(
                inner,
                caller.base_url,
                caller.issuer,
                db_url,
                project=frame_project,
                image=name,
                body=body,
            )
    finally:
        run.artifacts.extend([*inner.artifacts, *inner.assertions.values()])
    return {
        **seen,
        "frame_cleanup": inner.assertions["cleanup"],
        "owned": [frame_project, shadow_project, live_project],
    }


# -- authority-framed tools ---------------------------------------------------------------------

_LIVE_ALLOCATIONS: LiteralString = (
    "SELECT id, state FROM allocations WHERE state = ANY(%s) ORDER BY id"
)
_SEED_EXPIRED: LiteralString = (
    "INSERT INTO allocations (resource_id, state, lease_expiry, principal, agent_session, "
    "project) VALUES (%s, 'granted', now() - interval '1 minute', %s, %s, %s) RETURNING id"
)
_SWEEPS: LiteralString = (
    "SELECT object_id, ts FROM audit_log WHERE tool = 'reconciler.sweep_expired' "
    "AND ts >= %s AND ts <= %s"
)


async def _system_held(db_url: str, system: AuthoritySystem) -> dict[str, object]:
    """The authority System's database state and its domain on the authority daemon."""
    row = await _one_row(
        db_url,
        "SELECT s.state, a.state AS allocation FROM systems s JOIN allocations a "
        "ON a.id = s.allocation_id WHERE s.id = %s",
        (system.system_id,),
    )
    facts = (await asyncio.to_thread(authority_inventory, system.system_id)).system
    held = {
        "system": row["state"],
        "allocation": row["allocation"],
        "domain_active": bool(facts and facts.active),
    }
    assert held["system"] == "ready" and held["domain_active"], f"authority System {held}"
    return held


async def _framed(
    run: CellRun,
    caller: HttpCaller,
    db_url: str,
    body: Callable[[AuthoritySystem], Awaitable[dict[str, object]]],
) -> dict[str, object]:
    """Run ``body`` on an authority-owned System on an inner run; keep its records as artifacts."""
    inner = CellRun(run.cell, run.writer)
    seen: dict[str, object] = {}

    async def observed(system: AuthoritySystem) -> dict[str, object]:
        seen.update(await body(system))
        return dict(seen)

    try:
        await on_authority_system(
            inner, caller.base_url, caller.issuer, db_url, project=_fresh(), body=observed
        )
    finally:
        run.artifacts.extend([*inner.artifacts, *inner.assertions.values()])
    return {
        **seen,
        "frame_effect": inner.assertions["effect"],
        "frame_cleanup": inner.assertions["cleanup"],
    }


async def _expire_once(
    caller: HttpCaller,
    token: str,
    db_url: str,
    host: tuple[str, str],
    discover: bool,
) -> dict[str, object] | None:
    """Seed one lapsed grant on ``host`` and reconcile; ``None`` when the loop expired it first.

    The call owns the repair when the seed's expiry audit row falls inside the call and the
    pass counts exactly the expiries recorded inside it.
    """
    host_id, project = host
    seed = (host_id, f"{project}-seed", f"{project}-sess", project)
    drift = str((await _write(db_url, _SEED_EXPIRED, seed))[0]["id"])
    start = await _db_now(db_url)
    env = one(await caller.call("ops.reconcile_now", {}, token, discover=discover))
    sweeps = await _rows(db_url, _SWEEPS, (start, await _db_now(db_url)))
    state = (await _one_row(db_url, _STATE_OF, (drift,)))["state"]
    assert state == "expired", f"the seeded drift is {state}"
    if drift not in {str(s["object_id"]) for s in sweeps}:
        return None
    if int(str(env.data["expired_allocations"])) != len(sweeps):
        return None
    return {"repaired": "expired lease", "drift": drift, "failures": env.data.get("failures")}


async def _reconcile_now_body(
    caller: HttpCaller, grants: Grants, *, run: CellRun, db_url: str
) -> dict[str, object]:
    token, owned = caller.token(grants), []

    async def body(system: AuthoritySystem) -> dict[str, object]:
        project = _fresh()
        async with _hosts(caller, db_url) as hosts:
            host = (str((await hosts.add(project))["id"]), project)
            live = await _rows(db_url, _LIVE_ALLOCATIONS, (list(_LIVE),))
            held = await _system_held(db_url, system)
            observed = await _attributed(partial(_expire_once, caller, token, db_url, host))
            after = await _rows(db_url, _LIVE_ALLOCATIONS, (list(_LIVE),))
            assert after == live, "the pass moved a live allocation"
            assert await _system_held(db_url, system) == held, "the authority System changed"
            owned.extend(hosts.owned())
        return {**observed, "retained": held, "live_retained": len(live)}

    return {**await _framed(run, caller, db_url, body), "owned": owned}


_ON_HOST: LiteralString = (
    "SELECT id, state FROM allocations WHERE resource_id = %s AND state = ANY(%s)"
)


async def _drain_passive(
    caller: HttpCaller,
    token: str,
    hosts: Hosts,
    system: AuthoritySystem,
    workload: str,
) -> dict[str, object]:
    """Passively drain the lane host carrying ``system``; uncordon it whatever happens."""
    db_url = hosts.db_url
    lane_id = str((await _one_row(db_url, _ALLOCATION, (system.allocation_id,)))["resource_id"])
    on_lane = sorted(
        (str(r["id"]), str(r["state"]))
        for r in await _rows(db_url, _ON_HOST, (lane_id, list(_LIVE)))
    )
    before = await _one_row(db_url, _HOST, (lane_id,))
    held = await _system_held(db_url, system)
    args = {"resource_id": lane_id, "state": "schedulable"}
    restore = hosts.caller.token(_platform(workload, _OPERATOR))
    uncordon = partial(hosts.caller.call, "resources.set_scheduling", args, restore)
    async with _restoring(uncordon, "the lane host's cordon"):
        args = {"resource_id": lane_id, "mode": "passive"}
        env = one(await caller.call("resources.drain", args, token, discover=True))
        listed = sorted((i.object_id, i.status) for i in env.items)
        assert env.status == "cordoned" and listed == on_lane, "drain listed other work"
        assert system.allocation_id in dict(listed), "the System's allocation is not listed"
        after = await _one_row(db_url, _HOST, (lane_id,))
        assert after["cordoned"] and after["status"] == before["status"], "drain changed health"
        assert await _system_held(db_url, system) == held, "a passive drain touched the System"
        probe = await hosts.unplaceable(workload, lane_id)
    _same_except(before, await _one_row(db_url, _HOST, (lane_id,)))
    return {"listed": len(listed), "placement": probe, "retained": held}


async def _drain_body(
    caller: HttpCaller, grants: Grants, *, run: CellRun, db_url: str
) -> dict[str, object]:
    token, owned = caller.token(grants), []

    async def body(system: AuthoritySystem) -> dict[str, object]:
        workload = await _workload(db_url)
        async with _hosts(caller, db_url) as hosts:
            host_id = str((await hosts.add(workload))["id"])
            placed = await hosts.granted(workload, host_id)
            passive = await _drain_passive(caller, token, hosts, system, workload)
            args = {"resource_id": host_id, "mode": "force_release", "reason": _REASON}
            env = one(await caller.call("resources.drain", args, token))
            tally = {k: env.data.get(k) for k in ("released", "skipped", "failed")}
            assert tally == {"released": "1", "skipped": "0", "failed": "0"}, tally
            assert [(i.object_id, i.status) for i in env.items] == [(placed, "released")]
            hosts.placed.pop(placed)
            assert (await _one_row(db_url, _STATE_OF, (placed,)))["state"] == "released"
            host = await _one_row(db_url, _HOST, (host_id,))
            assert host["cordoned"], "force_release did not cordon its host"
            held = await _system_held(db_url, system)
            assert held == passive["retained"], "force_release touched the System"
            owned.extend([*hosts.owned(), placed])
        return {"passive": passive, "force_release": tally}

    return {**await _framed(run, caller, db_url, body), "owned": owned}


# -- the cell -----------------------------------------------------------------------------------

Body = Callable[..., Awaitable[dict[str, object]]]
_FUNCTIONAL: dict[str, Body] = {
    "images.extend": _extend_body,
    "inventory.clear_override": _clear_override_body,
    "ops.reconcile_systems": _reconcile_systems_body,
    "ops.set_cost_class_coeff": _set_coeff_body,
    "ops.set_host_capacity": _set_capacity_body,
    "ops.set_queue_paused": _queue_body,
    "resources.deregister": _deregister_body,
    "resources.register": _register_body,
    "resources.renew": _renew_body,
    "resources.set_scheduling": _set_scheduling_body,
    "resources.set_status": _set_status_body,
}
# Bodies that frame a System on an inner run need the outer run for its records.
_FRAMED: dict[str, Body] = {
    "images.prune_expired": _prune_body,
    "ops.reconcile_now": _reconcile_now_body,
    "resources.drain": _drain_body,
}


def _valid(tool: str, project: str) -> dict[str, object]:
    """Arguments a role denial refuses before any lookup: absent ids, fresh names."""
    host = {"resource_id": _ABSENT_ID}
    table: dict[str, dict[str, object]] = {
        "images.extend": {"image_id": _ABSENT_ID, "seconds": 60, "reason": _REASON},
        "images.prune_expired": {"reason": _REASON},
        "inventory.clear_override": {"resource_kind": "fault-inject", "name": f"{project}-absent"},
        "ops.set_cost_class_coeff": {"cost_class": COST_CLASS, "coeff": "1"},
        "ops.set_host_capacity": {**host, "concurrent_allocation_cap": 1},
        "ops.set_queue_paused": {"paused": True},
        "resources.drain": {**host, "mode": "passive"},
        "resources.register": {
            "kind": "local-libvirt",
            "name": f"{project}-host",
            "cost_class": COST_CLASS,
            "host_uri": "qemu+tcp://127.0.0.1:1/system",
            "owner_project": project,
            **_HOST_CAPS,
        },
        "resources.set_scheduling": {**host, "state": "cordoned"},
        "resources.set_status": {**host, "status": "degraded"},
    }
    return table.get(tool, host if tool.startswith("resources.") else {})


def _invalid(tool: str, project: str) -> dict[str, object]:
    """One mistyped argument per tool, beside otherwise valid ones."""
    table: dict[str, dict[str, object]] = {
        "images.extend": {"image_id": _ABSENT_ID, "seconds": "many", "reason": _REASON},
        "images.prune_expired": {"reason": ["many"]},
        "inventory.clear_override": {"resource_kind": ["fault-inject"], "name": "absent"},
        "ops.set_cost_class_coeff": {"cost_class": COST_CLASS, "coeff": ["1"]},
        "ops.set_host_capacity": {"resource_id": _ABSENT_ID, "concurrent_allocation_cap": "many"},
        "ops.set_queue_paused": {"paused": "maybe"},
        "resources.deregister": {"resource_id": _ABSENT_ID, "force": "maybe"},
        "resources.drain": {"resource_id": _ABSENT_ID, "mode": ["passive"]},
        "resources.register": {**_valid("resources.register", project), "kind": "galaxy"},
        "resources.renew": {"resource_id": ["many"]},
        "resources.set_scheduling": {"resource_id": _ABSENT_ID, "state": ["cordoned"]},
        "resources.set_status": {"resource_id": _ABSENT_ID, "status": ["degraded"]},
    }
    return table[tool]


def _rejection_grants(tool: str, boundary: Boundary, project: str) -> Grants:
    if boundary == "validation":
        # tools.invoke reports field_errors only for a tool the token can see (ADR-0722 §3).
        return _functional_grants(tool, project)
    if boundary == "authorization":
        # Platform roles do not nest: an operator lacks admin, an auditor lacks operator.
        return _platform(project, _OPERATOR if tool in _ADMIN_TOOLS else _AUDITOR)
    return _platform(project)


Setup = Callable[[], AbstractAsyncContextManager[Mapping[str, object]]]
_HOST_TARGETS = frozenset(
    {
        "ops.set_host_capacity",
        "resources.deregister",
        "resources.drain",
        "resources.renew",
        "resources.set_scheduling",
        "resources.set_status",
    }
)


def _setup(caller: HttpCaller, db_url: str, tool: str, project: str) -> Setup | None:
    """A real target for an authorization cell: a runtime host or a private image of ``project``."""

    @asynccontextmanager
    async def host() -> AsyncIterator[Mapping[str, object]]:
        async with _hosts(caller, db_url) as hosts:
            yield {"resource_id": str((await hosts.add(project))["id"])}

    @asynccontextmanager
    async def image() -> AsyncIterator[Mapping[str, object]]:
        actor = _direct(caller)
        async with _uploading(caller.base_url, caller.issuer, db_url, project) as upload:
            yield {
                "image_id": await _register(actor, actor.token(_operator(project)), upload, db_url)
            }

    if tool in _HOST_TARGETS:
        return host
    return image if tool == "images.extend" else None


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    cell = run.cell
    caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
    project = _fresh()
    snapshot = partial(_state, db_url, project)
    if cell.kind == "functional":
        grants = _functional_grants(cell.operation, project)
        if cell.operation in _FRAMED:
            body = partial(_FRAMED[cell.operation], run=run, db_url=db_url)
        else:
            body = partial(_FUNCTIONAL[cell.operation], db_url=db_url)
        await prove_functional(run, caller, grants, body, snapshot)
        return
    boundary = boundary_of(cell)
    args = _invalid if boundary == "validation" else _valid
    rejection = Rejection(
        args(cell.operation, project), _rejection_grants(cell.operation, boundary, project)
    )
    setup = _setup(caller, db_url, cell.operation, project) if boundary == "authorization" else None
    await prove_rejection(run, caller, boundary, rejection, snapshot, setup=setup)


@pytest.mark.parametrize("cell", tool_cells(TOOLS), ids=lambda cell: cell.id)
def test_operator_mutation_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a mutating operator tool."""
    run_tool_cell(cell, _scenario)
