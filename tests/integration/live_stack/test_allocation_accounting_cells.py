"""Behavioral checks for #3098's independent assertions; no live stack required."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import uuid4

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import JsonValue, ToolResponse
from scripts.coverage_campaign.contract import FILTERED_LIST_BOUNDARIES, build_contract
from tests.integration._seed import seed_granted_allocation, seed_project_limits
from tests.integration.conftest import open_pool
from tests.integration.live_stack import allocation_accounting_cells as carrier
from tests.integration.live_stack import tool_cells
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.tool_cells import HttpCaller

_Statement = tuple[LiteralString, tuple[object, ...]]


_NODE = (
    "tests/integration/test_allocation_accounting_tool_cells_live.py"
    "::test_allocation_accounting_tool_cell"
)


def test_complete_owner_binding_set() -> None:
    cells = [c for c in build_contract().cells if c.owner == 3098]
    assert len(cells) == 220 and len({c.scenario_id for c in cells}) == 55
    assert {c.operation for c in cells} == set(carrier.TOOLS)
    assert {c.node_id for c in cells} == {_NODE}
    assert sum(c.kind == "functional" for c in cells) == 44
    filtered = [
        c
        for c in cells
        if c.kind == "rejection"
        and (c.operation, tool_cells.boundary_of(c)) in FILTERED_LIST_BOUNDARIES
    ]
    assert len(filtered) == 8 and {c.operation for c in filtered} == {"allocations.list"}
    assert set(carrier._SERVER_BODIES) | set(carrier._SYSTEM_BODIES) == set(carrier.TOOLS)


def test_expected_kcu_follows_the_documented_formula() -> None:
    rate = carrier.expected_rate(Decimal(1), "kvm", 2, 4)
    assert rate == Decimal(3)
    assert carrier.expected_rate(Decimal(1), "tcg", 2, 4) == Decimal(12)
    assert carrier.expected_rate(Decimal("1.5"), None, 1, 1) == Decimal("1.875")
    assert carrier.quantize(Decimal("0.00005")) == Decimal("0.0000")  # banker's rounding
    assert carrier.quantize(Decimal("0.00015")) == Decimal("0.0002")
    # Never active: the whole reservation is credited back.
    assert carrier.expected_reconcile(rate, 0, Decimal(3)) == Decimal(-3)
    assert carrier.expected_reconcile(rate, 1800.0, Decimal(3)) == Decimal("-1.5000")


def test_estimate_check_rejects_every_mispriced_field() -> None:
    rate = carrier.expected_rate(Decimal(1), "tcg", 2, 4)
    good = {
        "accel": "tcg",
        "accel_factor": "4.0",
        "estimate_kcu": "36.0000",
        "rate_kcu_per_hr": "12.0000",
        "breakdown_vcpu_kcu_per_hr": "8.0000",
        "breakdown_memory_kcu_per_hr": "4.0000",
    }
    carrier.check_estimate(good, rate, "tcg", Decimal(3), Decimal(1))
    for name, value in (
        ("accel", "kvm"),
        ("accel_factor", "1.0"),
        ("estimate_kcu", "36.0001"),
        ("rate_kcu_per_hr", "3.0000"),
        ("breakdown_vcpu_kcu_per_hr", "2.0000"),
        ("breakdown_memory_kcu_per_hr", "1.0000"),
    ):
        with pytest.raises(AssertionError):
            carrier.check_estimate({**good, name: value}, rate, "tcg", Decimal(3), Decimal(1))


def test_ledger_check_rejects_missing_extra_and_wrong_rows() -> None:
    rows = [{"event_type": "reserved", "kcu_delta": "1.0000"}]
    carrier.assert_ledger(rows, [("reserved", Decimal(1))])
    for expected in (
        [],
        [("reserved", Decimal(1)), ("reconciled", Decimal(-1))],
        [("reserved", Decimal("1.0001"))],
        [("reconciled", Decimal(1))],
    ):
        with pytest.raises(AssertionError, match="ledger rows"):
            carrier.assert_ledger(rows, expected)


def test_lease_checks_detect_a_wrong_delta_and_an_unclamped_renew() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    carrier.check_renewal(now, now + timedelta(hours=1), timedelta(hours=1))
    with pytest.raises(AssertionError, match="lease moved"):
        carrier.check_renewal(now, now + timedelta(hours=2), timedelta(hours=1))
    carrier.check_clamp(timedelta(hours=20), now, now + timedelta(hours=24))
    for delta in (timedelta(0), timedelta(hours=10000)):
        with pytest.raises(AssertionError, match="not clamped"):
            carrier.check_clamp(delta, now, now + delta)


def test_page_check_detects_order_missing_duplicate_and_extra() -> None:
    carrier.assert_page(["b", "a"], ["b", "a"])
    for actual in (["a", "b"], ["b"], ["b", "b"], ["b", "a", "c"]):
        with pytest.raises(AssertionError):
            carrier.assert_page(actual, ["b", "a"])


def _rollup(rows: dict[str, tuple[str, str, str]], totals: tuple[str, str]) -> ToolResponse:
    items = [
        ToolResponse.success(k, "ok", data={"reserved": r, "reconciled": c, "variance": v})
        for k, (r, c, v) in rows.items()
    ]
    data = {"total_reserved": totals[0], "total_reconciled": totals[1]}
    return ToolResponse.collection("report", "ok", items, data=data)


def test_rollup_check_compares_rows_totals_and_coverage() -> None:
    expected = {"p:alice": (Decimal(2), Decimal(-2))}
    good = _rollup({"p:alice": ("2.0000", "-2.0000", "-4.0000")}, ("2.0000", "-2.0000"))
    carrier.check_rollup(good, expected, {"p"})
    for bad in (
        _rollup({"p:alice": ("2.0001", "-2.0000", "-4.0001")}, ("2.0001", "-2.0000")),
        _rollup({"p:alice": ("2.0000", "-2.0000", "0.0000")}, ("2.0000", "-2.0000")),
        _rollup({"p:alice": ("2.0000", "-2.0000", "-4.0000")}, ("3.0000", "-2.0000")),
        _rollup({}, ("0", "0")),
    ):
        with pytest.raises(AssertionError):
            carrier.check_rollup(bad, expected, {"p"})
    # Another project's window spend is not the granted set's.
    carrier.check_rollup(good, {**expected, "q:bob": (Decimal(1), Decimal(0))}, {"p"})


def test_section_check_compares_csv_xlsx_and_inline_rows() -> None:
    expected = [["p", "alice", "2.0000", "0", "-2.0000"]]
    body = "project,principal,reserved,reconciled,variance\np,alice,2.0000,0.0000,-2.0000\n"
    sheet: list[tuple[object, ...]] = [
        ("project", "principal", "reserved", "reconciled", "variance"),
        ("p", "alice", "2.0000", "0.0000", "-2.0000"),
    ]
    carrier.check_section("costs", body, sheet, expected, cut=False)
    with pytest.raises(AssertionError, match="CSV rows differ"):
        carrier.check_section(
            "costs", body.replace("2.0000,", "2.0001,", 1), sheet, expected, False
        )
    with pytest.raises(AssertionError, match="XLSX rows differ"):
        carrier.check_section("costs", body, sheet[:1], expected, cut=False)
    truncated = "-- truncated --\n" + body
    carrier.check_section("costs", truncated, [("-- truncated --",), *sheet], expected, cut=True)
    inline = [{"project": "p", "principal": "alice", "reserved": "2", "reconciled": "0",
               "variance": "-2"}]  # fmt: skip
    carrier.assert_prefix("costs", inline, expected, truncated=False)
    with pytest.raises(AssertionError, match="inline_truncated"):
        carrier.assert_prefix("costs", inline, expected, truncated=True)
    carrier.assert_prefix("costs", [], expected, truncated=True)
    with pytest.raises(AssertionError, match="prefix"):
        carrier.assert_prefix("costs", [{**inline[0], "principal": "bob"}], expected, False)
    leases = [["a1", "p", "alice", "granted", "", "active"]]
    shown = [{"id": "a1", "project": "p", "principal": "alice", "state": "granted",
              "lease_expiry": None, "status": "active"}]  # fmt: skip
    carrier.assert_prefix("leases", shown, leases, truncated=False)


@pytest.mark.parametrize("tool", sorted(carrier.TOOLS))
def test_rejections_follow_each_tools_role_floor(tool: str) -> None:
    project = "cov-p"
    floor = carrier.floor_grants(tool, project).roles[project]
    below = carrier.rejection_for(tool, "authorization", project)
    order = ["", "viewer", "contributor", "admin"]
    assert order.index(below.grants.roles.get(project, "")) == order.index(floor) - 1
    assert below.categories == {ErrorCategory.AUTHORIZATION_DENIED.value}
    isolated = carrier.rejection_for(tool, "project-isolation", project)
    assert project not in isolated.grants.projects
    if tool in carrier._BY_ALLOCATION:
        assert isolated.categories == {ErrorCategory.NOT_FOUND.value}
        assert isolated.absent_twin == isolated.args
    else:
        assert isolated.categories == {ErrorCategory.AUTHORIZATION_DENIED.value}
        assert isolated.absent_twin is None
    # The authentication control's issued-token call must be refused by role, writing nothing.
    assert carrier.rejection_for(tool, "authentication", project).grants == below.grants
    assert carrier.rejection_for(tool, "validation", project).args != carrier.valid_args(
        tool, project
    )


def _seed_accounting(url: str, statements: list[_Statement]) -> None:
    async def run() -> None:
        async with open_pool(url) as pool, pool.connection() as conn:
            for sql, params in statements:
                await conn.execute(sql, params)

    asyncio.run(run())


def test_accounting_snapshot_sees_live_state_but_not_history(migrated_url: str) -> None:
    async def seed() -> str:
        async with open_pool(migrated_url) as pool:
            await seed_project_limits(pool, project="proj")
            return await seed_granted_allocation(pool, project="proj")

    allocation = asyncio.run(seed())

    def state() -> dict[str, list[object]]:
        return asyncio.run(carrier.accounting_state(migrated_url, "proj"))

    def ledger(alloc: str) -> _Statement:
        return (
            "INSERT INTO ledger (id, project, allocation_id, cost_class, event_type, kcu_delta) "
            "VALUES (%s, 'proj', %s, 'local', 'reserved', 1)",
            (uuid4(), alloc),
        )

    changes: list[_Statement] = [
        ledger(allocation),
        ("UPDATE budgets SET spent_kcu = spent_kcu + 1 WHERE project = 'proj'", ()),
        ("UPDATE budgets SET limit_kcu = limit_kcu + 1 WHERE project = 'proj'", ()),
        ("UPDATE quotas SET max_pending_allocations = 3 WHERE project = 'proj'", ()),
        ("UPDATE allocations SET state = 'released' WHERE id = %s", (allocation,)),
    ]
    for change in changes:
        before = state()
        _seed_accounting(migrated_url, [change])
        assert state() != before, f"the snapshot missed {change[0]}"
    before = state()
    _seed_accounting(
        migrated_url,
        [
            ledger(allocation),  # the allocation is released now: history
            ("UPDATE quotas SET updated_at = now() - interval '1 day' WHERE project = 'proj'", ()),
            ("UPDATE budgets SET updated_at = now() - interval '1 day' WHERE project = 'proj'", ()),
        ],
    )
    assert state() == before, "history or an updated_at-only change moved the snapshot"


def _cell_run(tmp_path: Path, tool: str) -> CellRun:
    cell = next(c for c in build_contract().cells if c.operation == tool and c.kind == "functional")
    return CellRun(cell, EvidenceWriter(tmp_path))


@pytest.mark.parametrize("released_by,ok", [("body", True), ("frame", False)])
def test_release_cell_requires_the_body_to_have_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, released_by: str, ok: bool
) -> None:
    async def frame(fixture: CellRun, *_args: Any, body: Any, **_kwargs: Any) -> None:
        fixture.prove("effect", {"released": True})
        fixture.prove("cleanup", {"released_by": released_by})

    monkeypatch.setattr(carrier, "on_lane_system", frame)
    run = _cell_run(tmp_path, "allocations.release")
    caller, issuer = cast(HttpCaller, None), cast(OidcIssuer, None)
    scenario = carrier._on_system(run, caller, "", issuer, "", "cov-p", carrier._release_body)
    if ok:
        asyncio.run(scenario)
        assert set(run.assertions) == {"effect", "cleanup"}
    else:
        with pytest.raises(AssertionError, match="not the cell, released"):
            asyncio.run(scenario)


def test_fixture_release_continues_past_a_failed_release(monkeypatch: pytest.MonkeyPatch) -> None:
    released: list[str] = []

    async def row(_db: str, _query: str, key: str) -> dict[str, object]:
        return {"state": "granted"}

    async def release(self: carrier._Fixture, allocation_id: str) -> None:
        if allocation_id == "a":
            raise ConnectionError("transport")
        released.append(allocation_id)

    async def rows(_db: str, _query: str, _params: object = ()) -> list[dict[str, object]]:
        return [{"id": "b"}, {"id": "stray"}]

    monkeypatch.setattr(carrier, "_row", row)
    monkeypatch.setattr(carrier, "_rows", rows)
    monkeypatch.setattr(carrier._Fixture, "release", release)
    fixture = carrier._Fixture("p", "db", cast(HttpCaller, None), ["a", "b"])
    assert asyncio.run(fixture.release_all()) == ["a: ConnectionError"]
    # A live allocation the fixture never recorded (an over-admitted probe) is swept too.
    assert released == ["b", "stray"]


def test_restore_failure_is_noted_on_the_primary_failure() -> None:
    restored: list[bool] = []

    async def restore() -> None:
        restored.append(True)
        raise ConnectionError("transport")

    async def failing() -> None:
        async with carrier._restoring(restore):
            raise AssertionError("primary")

    with pytest.raises(AssertionError, match="primary") as caught:
        asyncio.run(failing())
    assert restored == [True]
    assert caught.value.__notes__ == ["seeded limit restore failed: ConnectionError"]

    async def passing() -> None:
        async with carrier._restoring(restore):
            pass

    with pytest.raises(ConnectionError):
        asyncio.run(passing())


def _admission_fixture(monkeypatch: pytest.MonkeyPatch, answer: ToolResponse) -> list[str]:
    released: list[str] = []

    async def reply(*_args: object, **_kwargs: object) -> ToolResponse:
        return answer

    async def release(self: carrier._Fixture, allocation_id: str) -> None:
        released.append(allocation_id)

    monkeypatch.setattr(carrier, "_answer", reply)
    monkeypatch.setattr(carrier._Fixture, "release", release)
    return released


def test_budget_boundary_probe_fails_when_the_product_over_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    granted = ToolResponse.success("over", "granted")
    released = _admission_fixture(monkeypatch, granted)
    fixture = carrier._Fixture("p", "db", cast(HttpCaller, None))
    with pytest.raises(AssertionError, match="over-budget admitted"):
        asyncio.run(carrier.check_admission(fixture, admitted=False))
    # At the boundary the same grant is the expected answer and is released.
    asyncio.run(carrier.check_admission(fixture, admitted=True))
    assert released == ["over"]


def test_budget_boundary_probe_needs_a_budget_unmet_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    unmet: list[JsonValue] = [{"gate": "budget", "required_limit_kcu": "1"}]
    denied = ToolResponse.failure("x", ErrorCategory.ALLOCATION_DENIED, data={"unmet": unmet})
    _admission_fixture(monkeypatch, denied)
    fixture = carrier._Fixture("p", "db", cast(HttpCaller, None))
    asyncio.run(carrier.check_admission(fixture, admitted=False))
    with pytest.raises(AssertionError, match="boundary was refused"):
        asyncio.run(carrier.check_admission(fixture, admitted=True))
    quota = ToolResponse.failure(
        "x", ErrorCategory.ALLOCATION_DENIED, data={"unmet": [{"gate": "quota"}]}
    )
    _admission_fixture(monkeypatch, quota)
    with pytest.raises(AssertionError, match="no unmet budget gate"):
        asyncio.run(carrier.check_admission(fixture, admitted=False))


def test_accounting_snapshot_compares_kcu_by_value_not_scale(migrated_url: str) -> None:
    # A never-active release credits its whole reservation: spent_kcu returns to the same value
    # with the ledger's four-decimal scale, which a row-text digest would read as a change.
    async def seed() -> None:
        async with open_pool(migrated_url) as pool:
            await seed_project_limits(pool, project="proj", limit_kcu=1000)

    asyncio.run(seed())
    _seed_accounting(
        migrated_url,
        [("UPDATE budgets SET spent_kcu = 0, limit_kcu = 1000 WHERE project = 'proj'", ())],
    )
    before = asyncio.run(carrier.accounting_state(migrated_url, "proj"))
    _seed_accounting(
        migrated_url,
        [
            ("UPDATE budgets SET spent_kcu = spent_kcu + 1.2500 WHERE project = 'proj'", ()),
            ("UPDATE budgets SET spent_kcu = spent_kcu - 1.2500 WHERE project = 'proj'", ()),
            ("UPDATE budgets SET limit_kcu = 1000.0000 WHERE project = 'proj'", ()),
        ],
    )
    assert asyncio.run(carrier.accounting_state(migrated_url, "proj")) == before
