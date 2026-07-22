"""Budget/quota admission-gate tests (ADR-0007 §4-6, ADR-0040). Real Postgres.

`admit` is called directly on an injected autocommit connection with seeded
budget/quota/resource rows. These cover the M1 invariants the M0 host-cap tests do not:
the per-project quota and budget checks, the reserve-at-grant debit, request
idempotency, and the all-or-nothing denial (no row on any failing check).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.accounting.cost import Selector
from kdive.domain.accounting.records import Budget, Quota
from kdive.domain.capacity.state import AllocationState, ResourceStatus
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.mcp.auth import RequestContext
from kdive.services.allocation import idempotency as allocation_idempotency
from kdive.services.allocation.admission.core import (
    BUDGET_DENIAL_REASON,
    AllocationRequest,
    admission_gate,
    admit,
    funding_unmet,
    price_window_and_estimate,
    quota_status,
)

_DT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))
SEL = Selector(vcpus=2, memory_gb=4, cost_class="local")
# coeff(local)=1.0; rate = 1.0*(1.0*2 + 0.25*4) = 3.0 kcu/hr.


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_resource(
    conn: psycopg.AsyncConnection,
    *,
    cap: int = 5,
    vcpus: int = 64,
    memory_mb: int = 65536,
    disk_gb: int = 500,
    guest_arches: dict[str, object] | None = None,
) -> Resource:
    capabilities: dict[str, object] = {
        CONCURRENT_ALLOCATION_CAP_KEY: cap,
        "vcpus": vcpus,
        "memory_mb": memory_mb,
        "disk_gb": disk_gb,
    }
    if guest_arches is not None:
        capabilities["guest_arches"] = guest_arches
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities=capabilities,
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _seed_budget(conn: psycopg.AsyncConnection, *, limit: str) -> None:
    await BUDGETS.upsert(
        conn, Budget(project="proj", limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT)
    )


async def _seed_quota(
    conn: psycopg.AsyncConnection, *, allocs: int = 10, systems: int = 10
) -> None:
    await QUOTAS.upsert(
        conn,
        Quota(
            project="proj",
            max_concurrent_allocations=allocs,
            max_concurrent_systems=systems,
            updated_at=_DT,
        ),
    )


async def _spent(conn: psycopg.AsyncConnection) -> Decimal:
    budget = await BUDGETS.get(conn, "proj")
    assert budget is not None
    return budget.spent_kcu


async def _spent_or_none(conn: psycopg.AsyncConnection) -> Decimal | None:
    budget = await BUDGETS.get(conn, "proj")
    return budget.spent_kcu if budget is not None else None


async def _count(conn: psycopg.AsyncConnection, table: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def _admit(conn: psycopg.AsyncConnection, **kw: object):  # type: ignore[no-untyped-def]
    return admit(
        conn,
        AllocationRequest(
            ctx=CTX,
            resource=kw.pop("resource"),  # ty: ignore[invalid-argument-type]
            project="proj",
            selector=kw.pop("selector", SEL),  # ty: ignore[invalid-argument-type]
            window=kw.pop("window", 2),
            idempotency_key=kw.pop("idempotency_key", None),  # ty: ignore[invalid-argument-type]
        ),
    )


def test_grant_reserves_tcg_arch_at_emulation_rate(migrated_url: str) -> None:
    # A ppc64le request against a host that advertises tcg for it reserves at 4× the native
    # rate and persists the requested arch for the queued-promotion path (ADR-0362).
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(
                conn,
                guest_arches={
                    "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"}
                },
            )
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            outcome = await admit(
                conn,
                AllocationRequest(
                    ctx=CTX, resource=res, project="proj", selector=SEL, window=2, arch="ppc64le"
                ),
            )
            assert outcome.granted is True
            assert outcome.allocation is not None
            assert outcome.allocation.requested_arch == "ppc64le"
            # native rate 3.0 × A(tcg)=4 × window 2h = 24.0000 reserved.
            assert await _spent(conn) == Decimal("24.0000")

    asyncio.run(_run())


def test_grant_reserves_estimate_and_writes_one_ledger_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is True
            assert outcome.allocation is not None
            alloc = outcome.allocation
            assert alloc.state is AllocationState.GRANTED
            assert alloc.requested_vcpus == 2 and alloc.requested_memory_gb == 4
            assert alloc.active_started_at is None
            assert alloc.lease_expiry is not None
            # rate 3.0 × window 2h = 6.0000 reserved.
            assert await _spent(conn) == Decimal("6.0000")
            assert await _count(conn, "ledger") == 1
            assert await _count(conn, "allocations") == 1
            # one ->granted admission audit row.
            assert await _count(conn, "audit_log") == 1

    asyncio.run(_run())


def test_within_budget_is_false_without_budget_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            assert await allocation_idempotency.within_budget(conn, "proj", Decimal("1")) is False

    asyncio.run(_run())


def test_within_budget_compares_remaining_budget(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn, limit="10")
            await conn.execute("UPDATE budgets SET spent_kcu = %s WHERE project = %s", (6, "proj"))

            exact = await allocation_idempotency.within_budget(conn, "proj", Decimal("4"))
            too_much = await allocation_idempotency.within_budget(conn, "proj", Decimal("4.0001"))

        assert exact is True
        assert too_much is False

    asyncio.run(_run())


def test_over_budget_denies_with_no_rows(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="5")  # estimate 6.0 > 5
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0
            assert await _count(conn, "audit_log") == 0
            assert await _spent(conn) == Decimal(0)

    asyncio.run(_run())


def test_exactly_at_budget_grants(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="6")  # estimate 6.0 == remaining 6
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is True
            assert await _spent(conn) == Decimal("6.0000")

    asyncio.run(_run())


def test_budget_only_unmet_carries_figures(migrated_url: str) -> None:
    # #833/#838: a budget-only denial enumerates the budget gate in data["unmet"] with absolute
    # figures so the caller can size accounting.set_budget instead of guessing.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="5")  # estimate 6.0 > remaining 5
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.reason == BUDGET_DENIAL_REASON
            unmet = outcome.details["unmet"]
            assert [e["gate"] for e in unmet] == ["budget"]
            budget = unmet[0]
            assert Decimal(budget["required_kcu"]) == Decimal("6")
            assert Decimal(budget["required_limit_kcu"]) == Decimal("6")
            assert Decimal(budget["limit_kcu"]) == Decimal("5")
            assert Decimal(budget["spent_kcu"]) == Decimal("0")
            assert Decimal(budget["remaining_kcu"]) == Decimal("5")

    asyncio.run(_run())


def test_budget_only_unmet_omits_figures_without_budget_row(migrated_url: str) -> None:
    # #833/#838: a project with no budget row is fail-closed; the limit/spent/remaining figures
    # are omitted rather than reported as zero, but both required figures are still named.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_quota(conn)  # quota present, budget absent
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.reason == BUDGET_DENIAL_REASON
            unmet = outcome.details["unmet"]
            assert [e["gate"] for e in unmet] == ["budget"]
            budget = unmet[0]
            assert Decimal(budget["required_kcu"]) == Decimal("6")
            assert Decimal(budget["required_limit_kcu"]) == Decimal("6")
            assert "limit_kcu" not in budget
            assert "spent_kcu" not in budget
            assert "remaining_kcu" not in budget

    asyncio.run(_run())


def test_both_funding_gates_unmet_aggregate(migrated_url: str) -> None:
    # #833: a fresh project trips quota AND budget; the synchronous denial enumerates both in
    # one envelope (top-level category stays the gate's primary, quota), no durable write.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)  # neither quota nor budget seeded
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.QUOTA_EXCEEDED
            assert [e["gate"] for e in outcome.details["unmet"]] == ["quota", "budget"]
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0
            assert await _spent_or_none(conn) is None

    asyncio.run(_run())


def test_quota_only_unmet_lists_quota(migrated_url: str) -> None:
    # #833: quota absent, budget generous → only the quota gate is enumerated.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")  # budget fine, quota absent
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.QUOTA_EXCEEDED
            unmet = outcome.details["unmet"]
            assert [e["gate"] for e in unmet] == ["quota"]
            assert unmet[0] == {"gate": "quota", "current": 0, "required": 1}

    asyncio.run(_run())


async def _occupy_one(conn: psycopg.AsyncConnection, resource_id: UUID) -> None:
    await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource_id,
            state=AllocationState.GRANTED,
        ),
    )


def test_quota_unmet_at_exact_limit_lists_limit_budget_met_at_exact_remaining(
    migrated_url: str,
) -> None:
    # Boundary test: quota count == limit denies (>=, not >) and the entry names the finite
    # limit; budget with remaining == estimate is still met (>=, not >), so the aggregate lists
    # the quota gate ONLY.
    async def _run() -> list[dict[str, object]]:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=10)
            await _occupy_one(conn, res.id)  # one occupying allocation -> count == 1
            await _seed_quota(conn, allocs=1)  # limit == count == 1
            await _seed_budget(conn, limit="6")  # remaining 6 == estimate (rate 3 x window 2h)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.QUOTA_EXCEEDED
            return outcome.details["unmet"]

    unmet = asyncio.run(_run())
    assert unmet == [{"gate": "quota", "current": 1, "required": 2, "limit": 1}]


def test_host_cap_denial_has_no_unmet(migrated_url: str) -> None:
    # #833: a host-capacity denial is a runtime (queueable) denial, not a funding gate, so it
    # carries no aggregated unmet list and keeps the existing cap/in_use envelope.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn, allocs=10)
            first = await _admit(conn, resource=res)
            assert first.granted is True
            second = await _admit(conn, resource=res)
            assert second.granted is False
            assert second.reason == "at_capacity"
            assert "unmet" not in second.details

    asyncio.run(_run())


def test_admission_gate_denial_stays_bare_no_unmet(migrated_url: str) -> None:
    # #833/ADR-0255: enrichment lives only in the synchronous admit() path. The shared gate the
    # promotion sweep replays returns a bare denial, pinning its routing contract against a
    # refactor that moved the aggregate read into the gate.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_quota(conn, allocs=10)
            await _seed_budget(conn, limit="5")  # budget short → budget denial
            req = AllocationRequest(ctx=CTX, resource=res, project="proj", selector=SEL, window=2)
            _, estimate = await price_window_and_estimate(conn, req)
            gate = await admission_gate(conn, req, estimate=estimate)
            assert gate.denial is not None
            assert gate.denial.reason == BUDGET_DENIAL_REASON
            assert "unmet" not in gate.denial.details

    asyncio.run(_run())


def test_budget_snapshot_returns_limit_and_spent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn, limit="10")
            await conn.execute("UPDATE budgets SET spent_kcu = %s WHERE project = %s", (6, "proj"))
            snapshot = await allocation_idempotency.budget_snapshot(conn, "proj")
            assert snapshot == (Decimal("10"), Decimal("6"))

    asyncio.run(_run())


def test_budget_snapshot_is_none_without_budget_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            assert await allocation_idempotency.budget_snapshot(conn, "proj") is None

    asyncio.run(_run())


def test_quota_status_none_limit_without_row(migrated_url: str) -> None:
    # #833: no quota row → limit None (fail-closed), count 0; the count is still reported so a
    # funding denial can name the current occupancy even with no row.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            assert await quota_status(conn, "proj") == (None, 0)

    asyncio.run(_run())


def test_quota_status_reports_limit_and_occupying(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn, allocs=5)
            granted = await _admit(conn, resource=res)
            assert granted.granted is True
            assert await quota_status(conn, "proj") == (5, 1)

    asyncio.run(_run())


def test_funding_unmet_lists_both_for_fresh_project(migrated_url: str) -> None:
    # #833: a project with neither row trips both funding gates; the aggregate enumerates both
    # so the caller provisions quota and budget at once. Neither carries limit figures.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            unmet = await funding_unmet(conn, "proj", Decimal("6"))
            assert [e["gate"] for e in unmet] == ["quota", "budget"]
            quota, budget = unmet
            assert quota == {"gate": "quota", "current": 0, "required": 1}
            assert budget == {
                "gate": "budget",
                "required_kcu": "6",
                "required_limit_kcu": "6",
            }

    asyncio.run(_run())


def test_funding_unmet_quota_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn, limit="100")  # budget fine, quota absent
            unmet = await funding_unmet(conn, "proj", Decimal("6"))
            assert [e["gate"] for e in unmet] == ["quota"]
            assert unmet[0] == {"gate": "quota", "current": 0, "required": 1}

    asyncio.run(_run())


def test_funding_unmet_budget_only_with_row_figures(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_quota(conn, allocs=10)  # quota fine
            await _seed_budget(conn, limit="5")  # remaining 5 < estimate 6
            unmet = await funding_unmet(conn, "proj", Decimal("6"))
            assert [e["gate"] for e in unmet] == ["budget"]
            assert unmet[0] == {
                "gate": "budget",
                "required_kcu": "6",
                "required_limit_kcu": "6",
                "limit_kcu": "5",
                "spent_kcu": "0",
                "remaining_kcu": "5",
            }

    asyncio.run(_run())


def test_funding_unmet_budget_required_limit_includes_prior_spend(migrated_url: str) -> None:
    # #833: the absolute figure to set is spent + estimate, not the estimate alone — so a
    # project with prior spend does not get denied a second time after sizing the budget.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_quota(conn, allocs=10)
            await _seed_budget(conn, limit="100")
            await conn.execute("UPDATE budgets SET spent_kcu = %s WHERE project = %s", (98, "proj"))
            unmet = await funding_unmet(conn, "proj", Decimal("6"))  # remaining 2 < 6
            assert [e["gate"] for e in unmet] == ["budget"]
            assert unmet[0]["required_limit_kcu"] == "104"  # 98 + 6
            assert unmet[0]["remaining_kcu"] == "2"

    asyncio.run(_run())


def test_funding_unmet_empty_when_both_satisfied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_quota(conn, allocs=10)
            await _seed_budget(conn, limit="100")
            assert await funding_unmet(conn, "proj", Decimal("6")) == []

    asyncio.run(_run())


def test_no_budget_row_denies_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_quota(conn)  # quota present, budget absent
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED
            assert await _count(conn, "allocations") == 0

    asyncio.run(_run())


def test_no_quota_row_denies_quota_exceeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")  # budget present, quota absent
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.QUOTA_EXCEEDED
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0

    asyncio.run(_run())


def test_at_alloc_quota_denies_quota_exceeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn, allocs=1)
            first = await _admit(conn, resource=res, idempotency_key=None)
            assert first.granted is True
            second = await _admit(conn, resource=res, idempotency_key=None)
            assert second.granted is False
            assert second.category is ErrorCategory.QUOTA_EXCEEDED
            assert await _count(conn, "allocations") == 1  # second wrote nothing
            assert await _spent(conn) == Decimal("6.0000")  # only the first reserved

    asyncio.run(_run())


def test_over_caps_selector_is_config_error_no_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, vcpus=2, memory_mb=4096)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res, selector=Selector(vcpus=8, memory_gb=4))
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0

    asyncio.run(_run())


def test_bad_window_is_config_error_no_negative_reserve(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res, window=-3)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "ledger") == 0
            assert await _spent(conn) == Decimal(0)

    asyncio.run(_run())


def test_replayed_idempotency_key_returns_original_no_double_charge(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            first = await _admit(conn, resource=res, idempotency_key="k1")
            assert first.granted is True
            assert first.allocation is not None
            replay = await _admit(conn, resource=res, idempotency_key="k1")
            assert replay.granted is True
            assert replay.allocation is not None
            assert replay.allocation.id == first.allocation.id
            # no second grant, ledger row, or spent bump.
            assert await _count(conn, "allocations") == 1
            assert await _count(conn, "ledger") == 1
            assert await _spent(conn) == Decimal("6.0000")

    asyncio.run(_run())


def test_resolve_replay_returns_none_without_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            replay = await allocation_idempotency.resolve_replay(
                conn,
                principal=CTX.principal,
                key="missing",
                kind="allocations.request",
                operation_label="allocation request",
            )

        assert replay is None

    asyncio.run(_run())


def test_resolve_replay_returns_original_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            first = await _admit(conn, resource=res, idempotency_key="replay")
            assert first.allocation is not None

            replay = await allocation_idempotency.resolve_replay(
                conn,
                principal=CTX.principal,
                key="replay",
                kind="allocations.request",
                operation_label="allocation request",
            )

        assert replay is not None
        assert replay.id == first.allocation.id

    asyncio.run(_run())


def test_resolve_replay_missing_allocation_reference_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            missing_id = uuid4()
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        "stale",
                        CTX.principal,
                        "proj",
                        "allocations.request",
                        Jsonb({"allocation_id": str(missing_id)}),
                    ),
                )
            with pytest.raises(RuntimeError, match=str(missing_id)):
                await allocation_idempotency.resolve_replay(
                    conn,
                    principal=CTX.principal,
                    key="stale",
                    kind="allocations.request",
                    operation_label="allocation request",
                )

    asyncio.run(_run())


def test_record_key_duplicate_maps_to_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            allocation_id = uuid4()
            await allocation_idempotency.record_key(
                conn,
                principal=CTX.principal,
                key="dup",
                project="proj",
                kind="allocations.request",
                allocation_id=allocation_id,
            )
            with pytest.raises(CategorizedError) as caught:
                await allocation_idempotency.record_key(
                    conn,
                    principal=CTX.principal,
                    key="dup",
                    project="proj",
                    kind="allocations.request",
                    allocation_id=allocation_id,
                )

        assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert caught.value.details == {"principal": CTX.principal}
        assert str(caught.value) == f"idempotency key ({CTX.principal}, dup) is already in use"

    asyncio.run(_run())


def test_same_key_reused_across_projects_is_config_error(migrated_url: str) -> None:
    # A key that already names a grant in another project cannot resolve here — returning
    # the foreign allocation would be a cross-project replay. Fail closed.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            await BUDGETS.upsert(
                conn,
                Budget(
                    project="proj2", limit_kcu=Decimal("100"), spent_kcu=Decimal(0), updated_at=_DT
                ),
            )
            await QUOTAS.upsert(
                conn,
                Quota(
                    project="proj2",
                    max_concurrent_allocations=10,
                    max_concurrent_systems=10,
                    updated_at=_DT,
                ),
            )
            ctx = RequestContext(principal="alice", agent_session="s", projects=("proj", "proj2"))
            first = await admit(
                conn,
                AllocationRequest(
                    ctx=ctx,
                    resource=res,
                    project="proj",
                    selector=SEL,
                    window=2,
                    idempotency_key="dup",
                ),
            )
            assert first.granted is True
            clash = await admit(
                conn,
                AllocationRequest(
                    ctx=ctx,
                    resource=res,
                    project="proj2",
                    selector=SEL,
                    window=2,
                    idempotency_key="dup",
                ),
            )
            assert clash.granted is False
            assert clash.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "allocations") == 1  # no grant for proj2

    asyncio.run(_run())


def test_same_key_two_principals_are_isolated(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            await QUOTAS.upsert(
                conn,
                Quota(
                    project="proj2",
                    max_concurrent_allocations=10,
                    max_concurrent_systems=10,
                    updated_at=_DT,
                ),
            )
            await BUDGETS.upsert(
                conn,
                Budget(
                    project="proj2", limit_kcu=Decimal("100"), spent_kcu=Decimal(0), updated_at=_DT
                ),
            )
            alice = RequestContext(principal="alice", agent_session="s", projects=("proj2",))
            bob = RequestContext(principal="bob", agent_session="s", projects=("proj2",))
            a = await admit(
                conn,
                AllocationRequest(
                    ctx=alice,
                    resource=res,
                    project="proj2",
                    selector=SEL,
                    window=2,
                    idempotency_key="shared",
                ),
            )
            b = await admit(
                conn,
                AllocationRequest(
                    ctx=bob,
                    resource=res,
                    project="proj2",
                    selector=SEL,
                    window=2,
                    idempotency_key="shared",
                ),
            )
            assert a.granted and b.granted
            assert a.allocation is not None and b.allocation is not None
            # Same key, different principals → two distinct grants (not a replay).
            assert a.allocation.id != b.allocation.id
            assert await _count(conn, "allocations") == 2

    asyncio.run(_run())


def test_request_does_not_replay_a_renew_key(migrated_url: str) -> None:
    # The mirror of test_key_reused_across_request_kind_is_rejected (renew side): a
    # (principal, key) already stored under the *renew* kind must not resolve as a request
    # replay. Returning the renew's allocation as a "grant" would be a cross-kind replay
    # (the same key cannot mean a grant and a renew). admit must fail closed, with no second
    # grant, ledger row, or spend.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            # An existing allocation a prior renew (under key "dup") targeted.
            original = await _admit(conn, resource=res, idempotency_key="orig")
            assert original.granted is True
            assert original.allocation is not None
            allocs_before = await _count(conn, "allocations")
            ledger_before = await _count(conn, "ledger")
            spent_before = await _spent(conn)
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        "dup",
                        CTX.principal,
                        "proj",
                        "allocations.renew",
                        Jsonb({"allocation_id": str(original.allocation.id)}),
                    ),
                )
            clash = await _admit(conn, resource=res, idempotency_key="dup")
            assert clash.granted is False
            assert clash.category is ErrorCategory.CONFIGURATION_ERROR
            assert clash.allocation is None
            assert await _count(conn, "allocations") == allocs_before
            assert await _count(conn, "ledger") == ledger_before
            assert await _spent(conn) == spent_before

    asyncio.run(_run())


def test_host_cap_denies_allocation_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn, allocs=10)
            first = await _admit(conn, resource=res)
            assert first.granted is True
            second = await _admit(conn, resource=res)
            assert second.granted is False
            assert second.category is ErrorCategory.ALLOCATION_DENIED
            assert second.reason == "at_capacity"

    asyncio.run(_run())


def test_bad_host_cap_fails_closed_no_row(migrated_url: str) -> None:
    # The budget/quota checks pass; the M0 host-cap resolve then fails closed on an
    # invalid cap — no allocation/ledger/audit row, and no reserve debit, must survive.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            res.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] = "not-an-int"
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0
            assert await _spent(conn) == Decimal(0)

    asyncio.run(_run())


def test_estimate_too_large_fails_closed_no_row(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An extreme clamp-max × a max-size selector overflows the kcu quantizer; admit must
    # return a typed configuration_error denial, never let the exception escape.
    monkeypatch.setenv("KDIVE_LEASE_MAX", "1e30")

    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, vcpus=2_000_000_000, memory_mb=64_000_000_000)
            await _seed_budget(conn, limit="1e40")
            await _seed_quota(conn)
            outcome = await _admit(
                conn,
                resource=res,
                selector=Selector(vcpus=2_000_000_000, memory_gb=0),
                window="1e30",
            )
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0

    asyncio.run(_run())
