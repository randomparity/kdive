"""Real allocation, accounting and report service cells (#3098).

Expected KCU is derived here from ``cost_class_coefficients`` and the documented formula, never
from the product's cost functions, so a pricing defect cannot also satisfy its own check.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, LiteralString, cast

import httpx
import openpyxl
import psycopg

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.services.allocation.lease_bounds import configured_lease_bounds
from scripts.coverage_campaign.contract import FILTERED_LIST_BOUNDARIES
from tests.integration.live_stack.run_system_job_cells import _create, _owned
from tests.integration.live_stack.scenario import CellRun, Sizing, catalog_profile
from tests.integration.live_stack.service_cells import (
    _ABSENT_ID,
    _DIGEST,
    _contributor,
    _member,
    _rows,
    _stranger,
    _viewer,
)
from tests.integration.live_stack.spine import (
    LOCAL_ALLOCATION_DISK_GB,
    await_system_state,
    seed_metering,
)
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Exposure,
    FilteredListControl,
    Grants,
    Guest,
    HttpCaller,
    Rejection,
    boundary_of,
    lane_image,
    on_lane_system,
    project_state,
    prove_functional,
    prove_rejection,
)

TOOLS = (
    "accounting.estimate",
    "accounting.report",
    "accounting.set_budget",
    "accounting.set_quota",
    "accounting.usage",
    "allocations.list",
    "allocations.release",
    "allocations.renew",
    "allocations.request",
    "allocations.wait",
    "reports.generate",
)


_VIEWER_FLOOR = frozenset(
    {
        "accounting.estimate",
        "accounting.report",
        "accounting.usage",
        "allocations.list",
        "allocations.wait",
        "reports.generate",
    }
)


_ADMIN_FLOOR = frozenset({"accounting.set_budget", "accounting.set_quota"})


# Tools addressed by an allocation id: a foreign row answers not_found, exactly like no row.
_BY_ALLOCATION = frozenset({"allocations.wait", "allocations.renew", "allocations.release"})


# Functional bodies that need a ready System: Run attribution, report sections, real teardown.
_ON_SYSTEM = frozenset({"accounting.usage", "reports.generate", "allocations.release"})


_LIVE = ["requested", "granted", "active", "releasing"]


_QUANTUM = Decimal("0.0001")


_ACCEL = {"kvm": Decimal(1), "tcg": Decimal(4), None: Decimal(1)}


_SIZE: dict[str, object] = {"vcpus": 1, "memory_gb": 1, "disk_gb": LOCAL_ALLOCATION_DISK_GB}


_PROBE_SIZING = Sizing(vcpu=1, memory_mb=1024, disk_gb=LOCAL_ALLOCATION_DISK_GB)


_WAIT_DEADLINE_S = 180.0


_TEARDOWN_DEADLINE_S = 600.0


# ADR-0722 §4: terminal allocations and their ledger rows are history; live ones stay observed,
# and the budget/quota projections drop updated_at, which every update moves.
_NARROWED = ("allocations", "ledger", "budgets", "quotas")


_ACCOUNTING_QUERIES: dict[str, LiteralString] = {
    "allocations:live": "FROM allocations t WHERE t.project = %(p)s AND t.state = ANY(%(live)s)",
    "ledger:live": "FROM ledger t WHERE t.allocation_id IN (SELECT id FROM allocations "
    "WHERE project = %(p)s AND state = ANY(%(live)s))",
    # Unconstrained numeric keeps each write's scale: 0 and 0.0000 are one balance.
    "budgets:limit-spent": "FROM (SELECT project, trim_scale(limit_kcu), trim_scale(spent_kcu) "
    "FROM budgets "
    "WHERE project = %(p)s) t",
    "quotas:caps": "FROM (SELECT project, max_concurrent_allocations, max_concurrent_systems, "
    "max_pending_allocations FROM quotas WHERE project = %(p)s) t",
}


_ALLOCATION: LiteralString = "SELECT * FROM allocations WHERE id = %s"


_CAPS: LiteralString = (
    "SELECT max_concurrent_allocations, max_concurrent_systems, max_pending_allocations "
    "FROM quotas WHERE project = %s"
)


_LEDGER: LiteralString = (
    "SELECT event_type, kcu_delta FROM ledger WHERE allocation_id = %s ORDER BY ts, id"
)


def quantize(value: Decimal) -> Decimal:
    """The product's single rounding point: 0.0001 KCU, banker's rounding (ADR-0007 §2)."""
    return value.quantize(_QUANTUM)


def expected_rate(coeff: Decimal, accel: str | None, vcpus: int, memory_gb: int) -> Decimal:
    """``coeff × A(accel) × (vcpus + 0.25 × memory_gb)``, unquantized (ADR-0007, ADR-0362)."""
    return coeff * _ACCEL[accel] * (Decimal(vcpus) + Decimal("0.25") * memory_gb)


def expected_reconcile(rate: Decimal, active_seconds: float, reserved: Decimal) -> Decimal:
    """``actual − Σreserved`` where ``actual = rate × active seconds / 3600``; one quantize."""
    actual = rate * (Decimal(active_seconds) / 3600) if active_seconds else Decimal(0)
    return quantize(actual - reserved)


def assert_ledger(rows: Sequence[Mapping[str, Any]], expected: list[tuple[str, Decimal]]) -> None:
    """The allocation's ledger is exactly ``expected`` (event type, delta), in order."""
    actual = [(str(r["event_type"]), Decimal(r["kcu_delta"])) for r in rows]
    assert actual == expected, f"ledger rows {actual} are not the expected {expected}"


def assert_kcu(answered: object, expected: Decimal, what: str) -> None:
    assert Decimal(str(answered)) == expected, f"{what} is {answered}, not {expected}"


def assert_page(actual: list[str], expected: list[str]) -> None:
    assert len(actual) == len(set(actual)), "page repeated an allocation"
    assert actual == expected, "page differs from the independently ordered allocations"


def assert_prefix(
    key: str, inline: Sequence[Mapping[str, object]], rows: list[list[str]], truncated: bool
) -> None:
    """Inline rows are a prefix of the expected rows, the whole set unless truncated."""
    shown = [normalized(key, [cell(v) for v in row.values()]) for row in inline]
    wanted = [normalized(key, row) for row in rows]
    assert shown == wanted[: len(shown)], f"{key} inline rows are not a prefix of the rows"
    assert truncated == (len(shown) < len(rows)), f"{key} inline_truncated disagrees"


def cell(value: object) -> str:
    """One report value as the spreadsheet renders it: ``None`` is an empty cell."""
    return "" if value is None else str(value)


# KCU columns compare as numbers: a SUM's scale is the database's, not the renderer's.
_NUMERIC = {"costs": (2, 3, 4)}


def normalized(key: str, row: list[str]) -> list[object]:
    return [Decimal(v) if i in _NUMERIC.get(key, ()) else v for i, v in enumerate(row)]


async def accounting_state(db: str, project: str) -> dict[str, list[object]]:
    """``project_state`` with allocations, ledger, budgets and quotas narrowed (ADR-0722 §4)."""
    state = await project_state(db, project)
    for table in _NARROWED:
        del state[table]
    params = {"p": project, "live": _LIVE}
    for key, where in _ACCOUNTING_QUERIES.items():
        row = (await _rows(db, _DIGEST + where, params))[0]
        state[key] = [row["n"], row["h"]]
    return state


async def _row(db: str, query: LiteralString, key: str) -> dict[str, Any]:
    rows = await _rows(db, query, (key,))
    assert len(rows) == 1, "the independent record is absent or duplicated"
    return rows[0]


async def _answer(c: HttpCaller, g: Grants, tool: str, **args: object) -> ToolResponse:
    result = await c.call(tool, args, c.token(g), discover=True)
    assert isinstance(result, ToolResponse), f"{tool} answered {len(result)} envelopes"
    return result


async def _call(c: HttpCaller, g: Grants, tool: str, **args: object) -> ToolResponse:
    result = await _answer(c, g, tool, **args)
    assert result.error_category is None, f"{tool} failed: {result.error_category}"
    return result


def _admin(project: str) -> Grants:
    return Grants(f"{project}-admin", (project,), {project: "admin"})


def _auditor(project: str) -> Grants:
    return Grants(f"{project}-auditor", (project,), {project: "viewer"}, ("platform_auditor",))


async def _coeff(db: str, resource_id: str) -> Decimal:
    row = await _row(
        db,
        "SELECT c.coeff FROM resources r JOIN cost_class_coefficients c "
        "ON c.cost_class = r.cost_class WHERE r.id = %s",
        resource_id,
    )
    return Decimal(row["coeff"])


async def _in_use(c: HttpCaller, g: Grants, resource_id: str) -> int:
    env = await _call(c, g, "resources.availability")
    counts = [i.data["in_use"] for i in env.items if i.object_id == resource_id]
    assert len(counts) == 1 and isinstance(counts[0], int), "resource availability is missing"
    return counts[0]


async def _seed_pending(db: str, project: str, pending: int) -> None:
    """Set ``max_pending_allocations`` out of band, as ``seed_metering`` seeds the other caps."""
    async with await psycopg.AsyncConnection.connect(db) as conn:
        await conn.execute(
            "UPDATE quotas SET max_pending_allocations = %s WHERE project = %s", (pending, project)
        )


@dataclass
class _Fixture:
    """One cell's project, its setup caller and the allocations it must release."""

    project: str
    db: str
    setup: HttpCaller
    owned: list[str] = field(default_factory=list)

    async def request(self, *, queue: bool = False, window: int = 1) -> ToolResponse:
        args = {"project": self.project, "resource": {"mode": "kind"}, "window": window, **_SIZE}
        if queue:
            args["on_capacity"] = "queue"
        env = await _call(self.setup, _contributor(self.project), "allocations.request", **args)
        self.owned.append(env.object_id)
        return env

    async def release(self, allocation_id: str) -> None:
        await _call(
            self.setup,
            _contributor(self.project),
            "allocations.release",
            allocation_id=allocation_id,
        )

    async def release_all(self) -> list[str]:
        """Release every live allocation of the project; what could not be released.

        The project is the cell's own, so a probe the product wrongly admitted is swept too.
        """
        failed: list[str] = []
        live = await _rows(
            self.db,
            "SELECT id FROM allocations WHERE project = %s AND state = ANY(%s) ORDER BY created_at",
            (self.project, _LIVE),
        )
        for allocation_id in dict.fromkeys([*self.owned, *(str(r["id"]) for r in live)]):
            try:
                row = await _row(self.db, _ALLOCATION, allocation_id)
                if row["state"] in _LIVE:
                    await self.release(allocation_id)
            except Exception as exc:  # noqa: BLE001 - every owned allocation gets its attempt
                # The type only: a message can carry a URL or host detail (ADR-0715 evidence).
                failed.append(f"{allocation_id}: {type(exc).__name__}")
        return failed


@asynccontextmanager
async def _fixture(c: HttpCaller, project: str, db: str) -> AsyncIterator[_Fixture]:
    fixture = _Fixture(project, db, HttpCaller("direct", c.base_url, c.issuer))
    primary: BaseException | None = None
    try:
        yield fixture
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failed = await fixture.release_all()
        if failed and primary is None:
            raise AssertionError(f"owned allocations could not be released: {failed}")
        if failed and primary is not None:
            primary.add_note(f"owned allocations could not be released: {failed}")


async def _estimate_body(c: HttpCaller, g: Grants, *, db: str, project: str) -> dict[str, object]:
    coeff = Decimal(
        (await _rows(db, "SELECT coeff FROM cost_class_coefficients WHERE cost_class = 'local'"))[
            0
        ]["coeff"]
    )
    checked = []
    for accel, window in (("kvm", 2), ("tcg", 3)):
        request = {"vcpus": 2, "memory_gb": 4, "window": window, "cost_class": "local"}
        env = await _call(
            c, g, "accounting.estimate", project=project, request={**request, "accel": accel}
        )
        rate = expected_rate(coeff, accel, 2, 4)
        check_estimate(env.data, rate, accel, Decimal(window), coeff)
        checked.append({"accel": accel, "window": window, "estimate": env.data["estimate_kcu"]})
    return {"estimates": checked}


def check_estimate(
    data: Mapping[str, object], rate: Decimal, accel: str, hours: Decimal, coeff: Decimal
) -> None:
    """Every priced field of an ``accounting.estimate`` answer against the formula."""
    assert data["accel"] == accel and Decimal(str(data["accel_factor"])) == _ACCEL[accel]
    assert_kcu(data["estimate_kcu"], quantize(rate * hours), "estimate")
    assert_kcu(data["rate_kcu_per_hr"], quantize(rate), "rate")
    factor = coeff * _ACCEL[accel]
    assert_kcu(data["breakdown_vcpu_kcu_per_hr"], quantize(factor * 2), "vCPU component")
    assert_kcu(data["breakdown_memory_kcu_per_hr"], quantize(factor * Decimal(1)), "memory part")


async def _reserved(db: str, allocation_id: str, hours: Decimal) -> Decimal:
    """The reservation an allocation's grant must have debited: rate × window."""
    row = await _row(db, _ALLOCATION, allocation_id)
    rate = expected_rate(
        await _coeff(db, str(row["resource_id"])),
        None,
        int(row["requested_vcpus"]),
        int(row["requested_memory_gb"]),
    )
    return quantize(rate * hours)


async def _spent(db: str, project: str) -> Decimal:
    rows = await _rows(db, "SELECT spent_kcu FROM budgets WHERE project = %s", (project,))
    return Decimal(rows[0]["spent_kcu"])


async def _request_body(c: HttpCaller, g: Grants, *, db: str, project: str) -> dict[str, object]:
    async with _fixture(c, project, db) as fx:
        spent = await _spent(db, project)
        args = {"project": project, "resource": {"mode": "kind"}, "window": 1, **_SIZE}
        granted = await _call(c, g, "allocations.request", **args)
        fx.owned.append(granted.object_id)
        row = await _row(db, _ALLOCATION, granted.object_id)
        assert granted.status == row["state"] == "granted", "request was not granted"
        assert granted.data["resource_id"] == str(row["resource_id"])
        reserve = await _reserved(db, granted.object_id, Decimal(1))
        assert_ledger(await _rows(db, _LEDGER, (granted.object_id,)), [("reserved", reserve)])
        assert await _spent(db, project) == spent + reserve, "spent_kcu missed the reservation"
        queued = await _call(c, g, "allocations.request", **args, on_capacity="queue")
        fx.owned.append(queued.object_id)
        held = await _row(db, _ALLOCATION, queued.object_id)
        assert queued.status == held["state"] == "requested", "request at the cap was not queued"
        assert held["resource_id"] is None and held["lease_expiry"] is None
        assert await _rows(db, _LEDGER, (queued.object_id,)) == [], "a queued request was billed"
        for allocation_id in (queued.object_id, granted.object_id):
            await fx.release(allocation_id)
        credit = [("reserved", reserve), ("reconciled", -reserve)]
        assert_ledger(await _rows(db, _LEDGER, (granted.object_id,)), credit)
        assert await _spent(db, project) == spent, "a never-active release kept its debit"
        return {"granted": True, "queued": True, "reserved": str(reserve), "owned": fx.owned}


async def _list_body(c: HttpCaller, g: Grants, *, db: str, project: str) -> dict[str, object]:
    async with _fixture(c, project, db) as fx:
        released = (await fx.request()).object_id
        await fx.release(released)
        await fx.request()
        await fx.request(queue=True)
        rows = await _rows(
            db,
            "SELECT id, state FROM allocations WHERE project = %s "
            "ORDER BY created_at DESC, id DESC",
            (project,),
        )
        filters: tuple[dict[str, object], ...] = (
            {"project": project},
            {"project": project, "state": "granted"},
            {"project": project, "state": "requested"},
            {"project": project, "state": "released"},
            {"project": project, "state": "expired"},
            {"project": project + "-absent"},
        )
        for request in filters:
            expected = [
                str(r["id"])
                for r in rows
                if request["project"] == project and request.get("state", r["state"]) == r["state"]
            ]
            assert_page(await _pages(c, g, request), expected)
            env = await _call(c, g, "allocations.list", request=request)
            assert_page([i.object_id for i in env.items], expected)
            for item in env.items:
                row = next(r for r in rows if str(r["id"]) == item.object_id)
                assert item.status == row["state"], "listed state disagrees with the record"
        return {"filters": len(filters), "records": len(rows), "owned": fx.owned}


async def _pages(c: HttpCaller, g: Grants, request: Mapping[str, object]) -> list[str]:
    ids: list[str] = []
    cursors: set[str] = set()
    current = dict(request)
    for _ in range(50):
        env = await _call(c, g, "allocations.list", request={**current, "limit": 1})
        ids.extend(item.object_id for item in env.items)
        cursor = env.data["next_cursor"]
        assert bool(cursor) == env.data["truncated"], "noncanonical paging metadata"
        if cursor is None:
            return ids
        assert isinstance(cursor, str) and cursor not in cursors, "page repeated its cursor"
        cursors.add(cursor)
        current["cursor"] = cursor
    raise AssertionError("allocations.list exceeded the bounded fixture's 50 pages")


def _ts(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


async def _renew_body(c: HttpCaller, g: Grants, *, db: str, project: str) -> dict[str, object]:
    async with _fixture(c, project, db) as fx:
        allocation = (await fx.request()).object_id
        before = await _row(db, _ALLOCATION, allocation)
        hour = await _reserved(db, allocation, Decimal(1))
        env = await _call(c, g, "allocations.renew", allocation_id=allocation, extend=1)
        after = await _row(db, _ALLOCATION, allocation)
        check_renewal(before["lease_expiry"], after["lease_expiry"], timedelta(hours=1))
        assert _ts(env.data["lease_expiry"]) == after["lease_expiry"], "answer disagrees with row"
        rows = await _rows(db, _LEDGER, (allocation,))
        assert_ledger(rows, [("reserved", hour), ("reserved", hour)])
        env = await _call(c, g, "allocations.renew", allocation_id=allocation, extend=10000)
        clamped = await _row(db, _ALLOCATION, allocation)
        delta = clamped["lease_expiry"] - after["lease_expiry"]
        check_clamp(delta, _ts(env.data["server_time"]), clamped["lease_expiry"])
        rate = hour  # rate × 1 h, quantized once per row exactly as admission does
        unit = await _reserved(db, allocation, Decimal(delta.total_seconds()) / 3600)
        assert_ledger(
            await _rows(db, _LEDGER, (allocation,)),
            [("reserved", rate), ("reserved", rate), ("reserved", unit)],
        )
        await fx.release(allocation)
        stale = await _answer(c, g, "allocations.renew", allocation_id=allocation, extend=1)
        assert stale.error_category == ErrorCategory.STALE_HANDLE.value, "terminal renew allowed"
        settled = await _rows(db, _LEDGER, (allocation,))
        assert len(settled) == 4, "a stale-handle renew wrote a ledger row"
        return {"renewed_h": 1, "clamped": True, "stale_handle": True, "owned": fx.owned}


def check_renewal(before: datetime, after: datetime, extend: timedelta) -> None:
    assert after - before == extend, f"lease moved by {after - before}, not {extend}"


def check_clamp(delta: timedelta, server_time: datetime, expiry: datetime) -> None:
    """A 10 000 h request is clamped: the lease grew, but by less and within the request."""
    assert timedelta(0) < delta < timedelta(hours=10000), f"renew was not clamped: {delta}"
    assert expiry <= server_time + timedelta(hours=10000), "lease passed the requested span"


async def _wait_body(c: HttpCaller, g: Grants, *, db: str, project: str) -> dict[str, object]:
    async with _fixture(c, project, db) as fx:
        holder = (await fx.request()).object_id
        queued = (await fx.request(queue=True)).object_id
        first = await _call(c, g, "allocations.wait", allocation_id=queued, timeout_s=0)
        assert first.status == "requested" and first.data.get("queue_position") == 1
        await fx.release(holder)
        deadline = time.monotonic() + _WAIT_DEADLINE_S
        env = first
        while env.status == "requested":
            assert time.monotonic() < deadline, "queued request was not promoted within 180 s"
            env = await _call(c, g, "allocations.wait", allocation_id=queued, timeout_s=60)
        row = await _row(db, _ALLOCATION, queued)
        assert env.status == row["state"] == "granted", f"queued request became {env.status}"
        assert row["resource_id"] is not None and env.data["resource_id"] == str(row["resource_id"])
        # A queued row keeps no window: promotion reserves the operator's default lease.
        default = configured_lease_bounds().default_hours
        reserve = await _reserved(db, queued, default)
        assert_ledger(await _rows(db, _LEDGER, (queued,)), [("reserved", reserve)])
        return {
            "promoted": True,
            "promoted_window_h": str(default),
            "promotion_call_by_test": False,
            "owned": fx.owned,
        }


async def _budget_body(c: HttpCaller, g: Grants, *, db: str, project: str) -> dict[str, object]:
    seeded = await _row(db, "SELECT limit_kcu, spent_kcu FROM budgets WHERE project = %s", project)
    restore = partial(_set_budget, c, project, str(seeded["limit_kcu"]))
    async with _fixture(c, project, db) as fx, _restoring(restore):
        # Price one grant, then free it: the lane host may admit one allocation at a time.
        allocation = (await fx.request()).object_id
        estimate = await _reserved(db, allocation, Decimal(1))
        await fx.release(allocation)
        spent = await _spent(db, project)
        boundary = spent + estimate
        for limit, admitted in ((boundary - _QUANTUM, False), (boundary, True)):
            env = await _call(c, g, "accounting.set_budget", project=project, limit_kcu=str(limit))
            assert_kcu(env.data["limit_kcu"], limit, "answered limit")
            row = await _row(
                db, "SELECT limit_kcu, spent_kcu FROM budgets WHERE project = %s", project
            )
            assert Decimal(row["limit_kcu"]) == limit and Decimal(row["spent_kcu"]) == spent
            await check_admission(fx, admitted)
        return {"boundary": str(boundary), "owned": fx.owned}


async def _set_budget(c: HttpCaller, project: str, limit: str) -> None:
    setup = HttpCaller("direct", c.base_url, c.issuer)
    await _call(setup, _admin(project), "accounting.set_budget", project=project, limit_kcu=limit)


async def _set_quota(c: HttpCaller, project: str, caps: Mapping[str, object]) -> None:
    setup = HttpCaller("direct", c.base_url, c.issuer)
    await _call(setup, _admin(project), "accounting.set_quota", project=project, **caps)


@asynccontextmanager
async def _restoring(restore: Callable[[], Awaitable[None]]) -> AsyncIterator[None]:
    """Run ``restore`` on exit; its failure is noted on a primary failure, not raised over it."""
    try:
        yield
    except BaseException as primary:
        try:
            await restore()
        except Exception as exc:  # noqa: BLE001 - noted on the primary failure
            primary.add_note(f"seeded limit restore failed: {type(exc).__name__}")
        raise
    await restore()


async def check_admission(fx: _Fixture, admitted: bool) -> None:
    """A one-hour request is granted, or denied for budget with an ``unmet`` budget entry."""
    args = {"project": fx.project, "resource": {"mode": "kind"}, "window": 1, **_SIZE}
    env = await _answer(fx.setup, _contributor(fx.project), "allocations.request", **args)
    if admitted:
        assert env.error_category is None and env.status == "granted", "boundary was refused"
        fx.owned.append(env.object_id)
        await fx.release(env.object_id)
        return
    assert env.error_category == ErrorCategory.ALLOCATION_DENIED.value, "over-budget admitted"
    unmet = env.data.get("unmet")
    entries = unmet if isinstance(unmet, list) else []
    budget = [u for u in entries if isinstance(u, dict) and u.get("gate") == "budget"]
    assert budget, "budget denial names no unmet budget gate"


async def _quota_body(c: HttpCaller, g: Grants, *, db: str, project: str) -> dict[str, object]:
    seeded = await _row(db, _CAPS, project)
    restore = partial(_set_quota, c, project, seeded)
    async with _fixture(c, project, db) as fx, _restoring(restore):
        wanted = {
            "max_concurrent_allocations": 1,
            "max_concurrent_systems": 0,
            "max_pending_allocations": 1,
        }
        env = await _call(c, g, "accounting.set_quota", project=project, **wanted)
        row = await _row(db, _CAPS, project)
        assert dict(row) == wanted and {k: env.data[k] for k in wanted} == wanted
        granted = (await fx.request()).object_id
        args = {"project": project, "resource": {"mode": "kind"}, "window": 1, **_SIZE}
        over = await _answer(fx.setup, _contributor(project), "allocations.request", **args)
        assert over.error_category == ErrorCategory.QUOTA_EXCEEDED.value, "cap 1 admitted 2"
        await fx.request(queue=True)
        full = await _answer(
            fx.setup, _contributor(project), "allocations.request", **args, on_capacity="queue"
        )
        assert full.error_category == ErrorCategory.QUOTA_EXCEEDED.value, "pending cap passed"
        await check_system_quota(fx, granted)
        return {"caps": wanted, "owned": fx.owned}


async def check_system_quota(fx: _Fixture, allocation_id: str) -> None:
    """``systems.provision`` at a zero System cap is refused before any System or job exists."""
    name, entry = lane_image()
    # The profile restates the allocation's size: a conflicting size is refused before quota.
    profile = catalog_profile(entry, name, f"{fx.project}-unread", _PROBE_SIZING)
    env = await _answer(
        fx.setup,
        Grants(f"{fx.project}-operator", (fx.project,), {fx.project: "operator"}),
        "systems.provision",
        allocation_id=allocation_id,
        profile=profile,
    )
    assert env.error_category == ErrorCategory.QUOTA_EXCEEDED.value, "System cap 0 admitted"
    systems = await _rows(fx.db, "SELECT id FROM systems WHERE project = %s", (fx.project,))
    jobs = await _rows(
        fx.db, "SELECT id FROM jobs WHERE authorizing->>'project' = %s", (fx.project,)
    )
    assert systems == [] and jobs == [], "a refused provision left a System or job"


async def _system_allocation(db: str, system_id: str) -> dict[str, Any]:
    system = await _row(db, "SELECT allocation_id, accel FROM systems WHERE id = %s", system_id)
    allocation = await _row(db, _ALLOCATION, str(system["allocation_id"]))
    return {**allocation, "accel": system["accel"]}


async def _ledger_sum(db: str, where: LiteralString, key: str) -> Decimal:
    rows = await _rows(db, "SELECT COALESCE(SUM(kcu_delta), 0) AS s FROM ledger " + where, (key,))
    return Decimal(rows[0]["s"])


async def _usage_body(c: HttpCaller, g: Grants, *, db: str, guest: Guest) -> dict[str, object]:
    project = guest.project
    allocation = await _system_allocation(db, guest.system_id)
    async with _owned(c, project, db) as inv:
        run = await _create(inv, inv.caller, _contributor(project), guest.system_id)
        attributed = await _ledger_sum(db, "WHERE allocation_id = %s", str(allocation["id"]))
        assert attributed > 0, "the System's allocation has no reservation to attribute"
        env = await _call(
            c, g, "accounting.usage", target={"kind": "investigation", "investigation_id": inv.id}
        )
        assert_kcu(env.data["investigation_kcu"], attributed, "investigation spend")
        await check_project_usage(db, project, env.data)
        env = await _call(c, g, "accounting.usage", target={"kind": "project", "project": project})
        await check_project_usage(db, project, env.data)
        return {"investigation_kcu": str(attributed), "owned": [inv.id, run]}


async def check_project_usage(db: str, project: str, data: Mapping[str, object]) -> None:
    budget = await _row(db, "SELECT limit_kcu, spent_kcu FROM budgets WHERE project = %s", project)
    total = await _ledger_sum(db, "WHERE project = %s", project)
    assert data["project"] == project
    assert_kcu(data["spent_kcu"], Decimal(budget["spent_kcu"]), "spent")
    assert Decimal(budget["spent_kcu"]) == total, "spent_kcu is not the ledger's sum"
    remaining = quantize(Decimal(budget["limit_kcu"]) - Decimal(budget["spent_kcu"]))
    assert_kcu(data["budget_remaining"], remaining, "budget remaining")
    assert_kcu(data["shared_kcu"], Decimal(0), "shared spend")
    classes = await _rows(
        db,
        "SELECT cost_class, SUM(kcu_delta) AS s FROM ledger WHERE project = %s GROUP BY cost_class",
        (project,),
    )
    by_class = cast(Mapping[str, object], data["by_cost_class"])
    assert {k: Decimal(str(v)) for k, v in by_class.items()} == {
        str(r["cost_class"]): Decimal(r["s"]) for r in classes
    }, "by_cost_class disagrees with the ledger"


async def _report_body(c: HttpCaller, g: Grants, *, db: str, project: str) -> dict[str, object]:
    async with _fixture(c, project, db) as fx:
        start = datetime.now(UTC) - timedelta(seconds=1)
        released = (await fx.request()).object_id
        await fx.release(released)
        await fx.request()
        await asyncio.sleep(1.1)
        end = datetime.now(UTC)
        await asyncio.sleep(1.1)
        window = [start.isoformat(), end.isoformat()]
        before = await _window_sums(db, start, end)
        request = {
            "scope": "granted-set",
            "projects": [project],
            "group_by": "principal",
            "window": window,
        }
        env = await _call(c, g, "accounting.report", request=request)
        check_rollup(env, before, {project})
        everyone = await _call(
            c,
            _auditor(project),
            "accounting.report",
            request={"scope": "all-projects", "window": window},
        )
        rolled = await _window_sums(db, start, end, by_principal=False)
        check_rollup(everyone, rolled, None)
        return {"window_projects": len(rolled), "owned": fx.owned}


async def _window_sums(
    db: str, start: datetime, end: datetime, *, by_principal: bool = True
) -> dict[str, tuple[Decimal, Decimal]]:
    """Σreserved and Σreconciled per project[:principal] for ledger rows in ``[start, end)``."""
    rows = await _rows(
        db,
        "SELECT l.project, a.principal, l.event_type, SUM(l.kcu_delta) AS s FROM ledger l "
        "JOIN allocations a ON a.id = l.allocation_id WHERE l.ts >= %s AND l.ts < %s "
        "GROUP BY l.project, a.principal, l.event_type",
        (start, end),
    )
    sums: dict[str, tuple[Decimal, Decimal]] = {}
    for row in rows:
        key = f"{row['project']}:{row['principal']}" if by_principal else str(row["project"])
        reserved, reconciled = sums.get(key, (Decimal(0), Decimal(0)))
        if row["event_type"] == "reserved":
            reserved += Decimal(row["s"])
        else:
            reconciled += Decimal(row["s"])
        sums[key] = (reserved, reconciled)
    return sums


def check_rollup(
    env: ToolResponse, expected: Mapping[str, tuple[Decimal, Decimal]], projects: set[str] | None
) -> None:
    """Each rollup row and the totals equal independent windowed ledger sums."""
    rows = {i.object_id: i.data for i in env.items}
    wanted = {
        k: v for k, v in expected.items() if projects is None or k.split(":", 1)[0] in projects
    }
    missing = sorted(set(wanted) - set(rows))
    assert not missing, f"rollup omits projects with window spend: {missing}"
    for key, (reserved, reconciled) in wanted.items():
        data = rows[key]
        assert_kcu(data["reserved"], quantize(reserved), f"{key} reserved")
        assert_kcu(data["reconciled"], quantize(reconciled), f"{key} reconciled")
        assert_kcu(data["variance"], quantize(reconciled - reserved), f"{key} variance")
    for name in ("reserved", "reconciled"):
        total = sum((Decimal(str(d[name])) for d in rows.values()), Decimal(0))
        assert_kcu(env.data[f"total_{name}"], quantize(total), f"total {name}")


_SECTIONS: dict[str, LiteralString] = {
    "inventory": "SELECT s.id AS system_id, s.domain_name AS name, s.project, s.state, "
    "r.kind AS resource_kind, COALESCE(a.requested_vcpus, sh.vcpus) AS vcpus, "
    "COALESCE(a.requested_memory_gb * 1024, sh.memory_mb) AS memory_mb, "
    "COALESCE(a.requested_disk_gb, sh.disk_gb) AS disk_gb FROM systems s "
    "JOIN allocations a ON a.id = s.allocation_id JOIN resources r ON r.id = a.resource_id "
    "LEFT JOIN system_shapes sh ON sh.name = s.shape WHERE s.project = %(p)s "
    "ORDER BY s.created_at DESC, s.id DESC",
    "leases": "SELECT id AS allocation_id, project, principal, state, lease_expiry, "
    "CASE WHEN state IN ('granted', 'active') AND lease_expiry IS NOT NULL "
    "AND lease_expiry > %(as_of)s THEN 'active' ELSE 'stale' END AS status FROM allocations "
    "WHERE state IN ('granted', 'active', 'expired') AND project = %(p)s "
    "ORDER BY lease_expiry DESC NULLS LAST, id DESC",
    "images": "SELECT provider, name, arch, format, visibility, owner, state FROM image_catalog "
    "WHERE visibility = 'public' OR (visibility = 'private' AND owner = %(p)s) "
    "ORDER BY provider, name, arch",
    "activity": "SELECT id AS run_id, project, system_id, state, created_at FROM runs "
    "WHERE project = %(p)s AND created_at < %(as_of)s ORDER BY created_at DESC, id DESC",
}


_SECTION_CAP = 500


async def _generate_body(c: HttpCaller, g: Grants, *, db: str, guest: Guest) -> dict[str, object]:
    project = guest.project
    async with _owned(c, project, db) as inv:
        run = await _create(inv, inv.caller, _contributor(project), guest.system_id)
        request = {"scope": "granted-set", "projects": [project], "formats": ["csv", "xlsx"]}
        env = await _call(c, g, "reports.generate", request=request)
        _REPORTS.append(str(env.data["report_id"]))
        assert "spreadsheet_unavailable" not in env.data, "report store degraded to inline-only"
        as_of = _ts(env.data["as_of"])
        workbook = await _download(str(env.refs["xlsx"]))
        sheets = openpyxl.load_workbook(io.BytesIO(workbook), read_only=True)
        compared = []
        for item in env.items:
            key = item.object_id
            expected = await _section_rows(db, key, project, as_of)
            truncated = len(expected) > _SECTION_CAP
            expected = expected[:_SECTION_CAP]
            assert item.data["truncated"] == truncated, f"{key} truncation flag is wrong"
            body = (await _download(str(env.refs[f"csv:{key}"]))).decode()
            sheet = list(sheets[key].iter_rows(values_only=True))
            check_section(key, body, sheet, expected, truncated)
            inline = json.loads(str(item.data["rows_json"]))
            assert_prefix(key, inline, expected, bool(item.data["inline_truncated"]))
            compared.append({"section": key, "rows": len(expected)})
        return {"sections": compared, "owned": [inv.id, run]}


_REPORTS: list[str] = []


async def _section_rows(db: str, key: str, project: str, as_of: datetime) -> list[list[str]]:
    if key == "costs":
        sums = await _window_sums(db, datetime.min.replace(tzinfo=UTC), as_of)
        return [
            [p, principal, cell(r), cell(rc), cell(quantize(rc - r))]
            for k, (r, rc) in sorted(sums.items())
            for p, principal in [k.split(":", 1)]
            if p == project
        ]
    rows = await _rows(db, _SECTIONS[key], {"p": project, "as_of": as_of})
    return [[cell(v) for v in row.values()] for row in rows]


def check_section(
    key: str, body: str, sheet: list[tuple[object, ...]], expected: list[list[str]], cut: bool
) -> None:
    """The CSV and the workbook sheet each hold exactly ``expected`` under one header."""
    lines = list(csv.reader(io.StringIO(body)))
    sheet_rows = [[cell(v) for v in row] for row in sheet]
    if cut:
        lines, sheet_rows = lines[1:], sheet_rows[1:]
    wanted = [normalized(key, row) for row in expected]
    assert [normalized(key, r) for r in lines[1:]] == wanted, f"{key} CSV rows differ"
    assert [normalized(key, r) for r in sheet_rows[1:]] == wanted, f"{key} XLSX rows differ"
    assert lines[0] == sheet_rows[0], f"{key} CSV and XLSX headers differ"


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0) as http:
        response = await http.get(url)
    assert response.status_code == 200, f"report download answered HTTP {response.status_code}"
    return response.content


async def _release_body(c: HttpCaller, g: Grants, *, db: str, guest: Guest) -> dict[str, object]:
    allocation = await _system_allocation(db, guest.system_id)
    allocation_id, resource = str(allocation["id"]), str(allocation["resource_id"])
    viewer = _viewer(guest.project)
    held = await _in_use(c, viewer, resource)
    env = await _call(c, g, "allocations.release", allocation_id=allocation_id)
    assert env.status == "released", f"release answered {env.status}"
    job = await _await_teardown(db, guest.system_id)
    await await_system_state(guest.op, "release", guest.system_id, "torn_down")
    row = await _row(db, _ALLOCATION, allocation_id)
    assert row["state"] == "released" and row["active_ended_at"] is not None
    active = (row["active_ended_at"] - row["active_started_at"]).total_seconds()
    rate = expected_rate(
        await _coeff(db, resource),
        allocation["accel"],
        int(row["requested_vcpus"]),
        int(row["requested_memory_gb"]),
    )
    ledger = await _rows(db, _LEDGER, (allocation_id,))
    reserved = sum((Decimal(r["kcu_delta"]) for r in ledger[:-1]), Decimal(0))
    assert_ledger(
        ledger,
        [(str(r["event_type"]), Decimal(r["kcu_delta"])) for r in ledger[:-1]]
        + [("reconciled", expected_reconcile(rate, active, reserved))],
    )
    total = await _ledger_sum(db, "WHERE project = %s", guest.project)
    assert await _spent(db, guest.project) == total, "spent_kcu is not the ledger's sum"
    after = await _in_use(c, viewer, resource)
    assert after == held - 1, f"capacity in use is {after}, not {held - 1} after release"
    return {
        "teardown_job": job,
        "system": "torn_down",
        "allocation": "released",
        "authority_role": "installed-revision-only",
    }


async def _await_teardown(db: str, system_id: str) -> str:
    """The reconciler's orphan-teardown job for ``system_id``, once it succeeded."""
    deadline = time.monotonic() + _TEARDOWN_DEADLINE_S
    key = f"{system_id}:teardown"
    while True:
        jobs = await _rows(db, "SELECT id, state FROM jobs WHERE dedup_key = %s", (key,))
        if jobs and jobs[0]["state"] in {"succeeded", "failed", "canceled"}:
            assert jobs[0]["state"] == "succeeded", f"orphan teardown ended {jobs[0]['state']}"
            return str(jobs[0]["id"])
        assert time.monotonic() < deadline, "the reconciler's orphan teardown did not finish"
        await asyncio.sleep(5.0)


Body = Callable[..., Awaitable[dict[str, object]]]


_SERVER_BODIES: dict[str, Body] = {
    "accounting.estimate": _estimate_body,
    "accounting.report": _report_body,
    "accounting.set_budget": _budget_body,
    "accounting.set_quota": _quota_body,
    "allocations.list": _list_body,
    "allocations.renew": _renew_body,
    "allocations.request": _request_body,
    "allocations.wait": _wait_body,
}


_SYSTEM_BODIES: dict[str, Body] = {
    "accounting.usage": _usage_body,
    "reports.generate": _generate_body,
    "allocations.release": _release_body,
}


def floor_grants(tool: str, project: str) -> Grants:
    """The minimum role a functional call of ``tool`` needs."""
    if tool in _VIEWER_FLOOR:
        return _viewer(project)
    if tool in _ADMIN_FLOOR:
        return _admin(project)
    return _contributor(project)


def valid_args(tool: str, project: str) -> dict[str, object]:
    """Schema-valid arguments naming ``project`` or an absent allocation."""
    if tool in _BY_ALLOCATION:
        args: dict[str, object] = {"allocation_id": _ABSENT_ID}
        return args | ({"extend": 1} if tool == "allocations.renew" else {})
    if tool == "allocations.list":
        return {"request": {"project": project}}
    if tool == "allocations.request":
        return {"project": project, "resource": {"mode": "kind"}, "window": 1, **_SIZE}
    if tool == "accounting.estimate":
        return {"project": project, "request": {"vcpus": 1, "memory_gb": 1, "window": 1}}
    if tool == "accounting.usage":
        return {"target": {"kind": "project", "project": project}}
    if tool in {"accounting.report", "reports.generate"}:
        return {"request": {"scope": "granted-set", "projects": [project]}}
    if tool == "accounting.set_budget":
        return {"project": project, "limit_kcu": "1"}
    return {"project": project, "max_concurrent_allocations": 1, "max_concurrent_systems": 1}


def rejection_for(tool: str, boundary: Boundary, project: str) -> Rejection:
    """The call ``boundary`` must reject, with the grants and categories it accepts."""
    args = valid_args(tool, project)
    if boundary == "validation":
        bad = {"request": {"limit": "many"}} if tool == "allocations.list" else {}
        return Rejection(bad, floor_grants(tool, project))
    if boundary == "authentication":
        # The issued-token control must pass authentication yet write nothing: below the floor.
        return Rejection(args, _below_floor(tool, project))
    if boundary == "project-isolation":
        if tool in _BY_ALLOCATION:
            return Rejection(
                args, _stranger(), frozenset({ErrorCategory.NOT_FOUND.value}), absent_twin=args
            )
        return Rejection(args, _stranger())
    return Rejection(args, _below_floor(tool, project))


def _below_floor(tool: str, project: str) -> Grants:
    """A member of ``project`` one role short of what ``tool`` needs."""
    if tool in _VIEWER_FLOOR:
        return _member(project)
    if tool in _ADMIN_FLOOR:
        return _contributor(project)
    return _viewer(project)


async def _denied(run: CellRun, c: HttpCaller, project: str, db: str) -> None:
    boundary, tool = boundary_of(run.cell), run.cell.operation
    rejection = rejection_for(tool, boundary, project)
    protected = partial(accounting_state, db, project)
    await seed_metering(db, project)
    if boundary in {"authentication", "validation"}:
        await prove_rejection(run, c, boundary, rejection, protected)
        return
    target: list[str] = []

    @asynccontextmanager
    async def setup() -> AsyncIterator[Mapping[str, object]]:
        async with _fixture(c, project, db) as fx:
            target.append((await fx.request()).object_id)
            yield {"allocation_id": target[0]} if tool in _BY_ALLOCATION else {}

    if (tool, boundary) in FILTERED_LIST_BOUNDARIES:

        async def expected() -> str:
            return str((await _row(db, _ALLOCATION, target[0]))["id"])

        rejection = replace(rejection, filtered=FilteredListControl(_viewer(project), expected))
    await prove_rejection(run, c, boundary, rejection, protected, setup=setup)


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db: str) -> None:
    cell, tool = run.cell, run.cell.operation
    c = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
    project = "cov-" + secrets.token_hex(4)
    run.artifacts.append(run.writer.artifact({"fixture_project": project, "operation": tool}))
    try:
        if cell.kind != "functional":
            await _denied(run, c, project, db)
        elif tool in _SYSTEM_BODIES:
            await _on_system(run, c, base_url, issuer, db, project, _SYSTEM_BODIES[tool])
        else:
            # One concurrent allocation, so a second request queues instead of being granted.
            await seed_metering(db, project, max_allocations=1)
            await _seed_pending(db, project, 1)
            body = partial(_SERVER_BODIES[tool], db=db, project=project)
            await prove_functional(
                run, c, floor_grants(tool, project), body, partial(accounting_state, db, project)
            )
    except BaseException as primary:
        if _REPORTS:
            try:
                run.artifacts.append(run.writer.artifact(await _retained_reports(db)))
            except Exception as exc:  # noqa: BLE001 - noted on the primary failure
                primary.add_note(f"retained report artifacts unrecorded: {type(exc).__name__}")
        raise
    if _REPORTS:
        run.artifacts.append(run.writer.artifact(await _retained_reports(db)))


async def _retained_reports(db: str) -> dict[str, object]:
    """Report artifacts left for the ADR-0212 retention sweep; their collection is not proven."""
    ids = list(_REPORTS)
    _REPORTS.clear()
    rows = await _rows(
        db,
        "SELECT id, owner_id, object_key, created_at FROM artifacts "
        "WHERE owner_kind = 'reports' AND owner_id::text = ANY(%s) ORDER BY id",
        (ids,),
    )
    assert {str(r["owner_id"]) for r in rows} == set(ids), "a report left no artifact rows"
    return {
        "policy_retained_report_artifacts": [
            {
                "artifact": str(r["id"]),
                "report": str(r["owner_id"]),
                "key": str(r["object_key"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ],
        "post_retention_collection_proven": False,
    }


async def _on_system(
    run: CellRun,
    c: HttpCaller,
    base_url: str,
    issuer: OidcIssuer,
    db: str,
    project: str,
    body: Body,
) -> None:
    """Run ``body`` on a ready lane System; its frame owns setup and the cleanup proof."""
    fixture = CellRun(run.cell, run.writer)
    grants = floor_grants(run.cell.operation, project)

    async def framed(guest: Guest) -> dict[str, object]:
        return await body(c, grants, db=db, guest=guest)

    try:
        await on_lane_system(fixture, base_url, issuer, db, project=project, body=framed)
    finally:
        run.artifacts.extend(fixture.artifacts)
        run.artifacts.extend(fixture.assertions.values())
        run.observed |= fixture.observed
    run.assertions["effect"] = fixture.assertions["effect"]
    run.assertions["cleanup"] = fixture.assertions["cleanup"]
    if run.cell.operation == "allocations.release":
        proof = json.loads((run.writer.root / "artifacts" / run.assertions["cleanup"]).read_bytes())
        assert proof.get("released_by") == "body", "the frame, not the cell, released the System"
