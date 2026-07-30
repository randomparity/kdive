"""Service-level behavior tests for queued allocation promotion."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.accounting.cost import Selector
from kdive.domain.accounting.records import Budget, Quota
from kdive.domain.capacity.state import AllocationState, ResourceStatus
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.mcp.auth import RequestContext
from kdive.security.audit import args_digest
from kdive.services.allocation import promotion
from kdive.services.allocation.admission.core import (
    AFFINITY_DENIAL_REASON,
    BUDGET_DENIAL_REASON,
    AdmissionOutcome,
    AllocationRequest,
    admit,
)
from kdive.services.allocation.promotion import (
    _is_budget_terminate,
    _request_from_queued,
    _selector_from_snapshot,
    _still_aged_requested,
    promote_pending,
    reap_queue_timeouts,
)

_ARCH = "ppc64le"

# (principal, agent_session, project, tool, object_kind, args_digest)
_AuditRow = tuple[str, str | None, str, str, str, str]

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_PROMOTION_LOGGER = "kdive.services.allocation.promotion"


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _resource(conn: psycopg.AsyncConnection, *, pool: str = "local-libvirt") -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={
                CONCURRENT_ALLOCATION_CAP_KEY: 1,
                "vcpus": 64,
                "memory_mb": 65536,
                "disk_gb": 500,
            },
            pool=pool,
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _quota(conn: psycopg.AsyncConnection, *, limit: str = "1000000") -> None:
    await BUDGETS.upsert(
        conn,
        Budget(project="proj", limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT),
    )
    await QUOTAS.upsert(
        conn,
        Quota(
            project="proj",
            max_concurrent_allocations=1_000_000,
            max_concurrent_systems=1_000_000,
            max_pending_allocations=100,
            updated_at=_DT,
        ),
    )


async def _granted(conn: psycopg.AsyncConnection, resource_id: UUID) -> Allocation:
    return await ALLOCATIONS.insert(
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


def _snapshot_alloc(**overrides: Any) -> Allocation:
    """An in-memory REQUESTED allocation carrying persisted request-snapshot fields."""
    fields: dict[str, Any] = {
        "id": uuid4(),
        "created_at": _DT,
        "updated_at": _DT,
        "principal": "bob",
        "agent_session": "bob-sess",
        "project": "proj",
        "resource_id": None,
        "state": AllocationState.REQUESTED,
        "requested_vcpus": 1,
        "requested_memory_gb": 1,
        "requested_disk_gb": 10,
    }
    fields.update(overrides)
    return Allocation(**fields)


def _bare_resource(*, cost_class: str = "local") -> Resource:
    return Resource(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=ResourceKind.LOCAL_LIBVIRT,
        capabilities={CONCURRENT_ALLOCATION_CAP_KEY: 1},
        pool="local-libvirt",
        cost_class=cost_class,
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
    )


async def _queued(
    conn: psycopg.AsyncConnection,
    resource: Resource,
    *,
    created_offset: timedelta = timedelta(0),
    requested_pool: str | None = None,
) -> UUID:
    by_id = resource.id if requested_pool is None else None
    outcome = await admit(
        conn,
        AllocationRequest(
            ctx=RequestContext(principal="bob", agent_session="bob-sess", projects=("proj",)),
            resource=resource,
            project="proj",
            selector=Selector(vcpus=1, memory_gb=0, cost_class="local"),
            window=1,
            on_capacity="queue",
            disk_gb=10,
            requested_kind=None,
            requested_resource_id=by_id,
            requested_pool=requested_pool,
        ),
    )
    assert outcome.allocation is not None
    alloc_id = outcome.allocation.id
    if created_offset != timedelta(0):
        await conn.execute(
            "UPDATE allocations SET created_at = now() + %s WHERE id = %s",
            (created_offset, alloc_id),
        )
    return alloc_id


async def _state(conn: psycopg.AsyncConnection, alloc_id: UUID) -> str:
    cur = await conn.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _failure_category(conn: psycopg.AsyncConnection, alloc_id: UUID) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT failure_category FROM allocations WHERE id = %s", (alloc_id,))
        row = await cur.fetchone()
    return row[0] if row else None


async def _audit_row(
    conn: psycopg.AsyncConnection, alloc_id: UUID, transition: str
) -> tuple[str, str | None, str, str, str, str]:
    """The (principal, agent_session, project, tool, object_kind, args_digest) audit row."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, agent_session, project, tool, object_kind, args_digest "
            "FROM audit_log WHERE object_id = %s AND transition = %s",
            (alloc_id, transition),
        )
        row = await cur.fetchone()
    assert row is not None
    return row


async def _lease_expiry(conn: psycopg.AsyncConnection, alloc_id: UUID) -> datetime | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT lease_expiry FROM allocations WHERE id = %s", (alloc_id,))
        row = await cur.fetchone()
    return row[0] if row else None


def test_promote_pending_grants_after_capacity_frees(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> tuple[
        int, str, UUID | None, datetime | None, datetime, tuple[_AuditRow, UUID, UUID]
    ]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)

            before = datetime.now(UTC)
            with caplog.at_level(logging.INFO, logger=_PROMOTION_LOGGER):
                promoted = await promote_pending(conn)
            state = await _state(conn, queued)
            cur = await conn.execute("SELECT resource_id FROM allocations WHERE id = %s", (queued,))
            row = await cur.fetchone()
            resource_id = row[0] if row else None
            lease = await _lease_expiry(conn, queued)
            audit = await _audit_row(conn, queued, "requested->granted")
        return promoted, state, resource_id, lease, before, (audit, resource.id, queued)

    promoted, state, resource_id, lease, before, extra = asyncio.run(_run())
    audit, resource_uuid, queued_id = extra
    assert (promoted, state) == (1, "granted")
    # The grant is stamped onto the freed host with a lease well into the future (promotion
    # resolves the default window): a `now - window` (past) or `window / seconds` (~now)
    # regression, or a dropped stamp, all miss this hour-plus lower bound.
    assert resource_id == resource_uuid
    assert lease is not None
    assert lease - before >= timedelta(hours=1)
    # The grant audit carries the queued row's original actor + the resolved target args.
    assert audit == (
        "bob",
        "bob-sess",
        "proj",
        "allocations.request",
        "allocations",
        args_digest({"resource_id": str(resource_uuid), "project": "proj"}),
    )
    assert any(
        f"reconciler: promoted queued allocation {queued_id} -> granted on resource {resource_uuid}"
        == record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    )


def test_promote_pending_grants_by_pool_to_freed_member(migrated_url: str) -> None:
    async def _run() -> tuple[int, str, str | None]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn, pool="big")
            await _quota(conn)
            holder = await _granted(conn, resource.id)  # fills the single host slot
            queued = await _queued(conn, resource, requested_pool="big")
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)

            promoted = await promote_pending(conn)
            state = await _state(conn, queued)
            cur = await conn.execute("SELECT resource_id FROM allocations WHERE id = %s", (queued,))
            row = await cur.fetchone()
            resource_id = str(row[0]) if row and row[0] is not None else None
        return promoted, state, resource_id

    promoted, state, resource_id = asyncio.run(_run())
    assert promoted == 1
    assert state == "granted"
    assert resource_id is not None  # stamped onto the freed pool member


def test_promote_pending_budget_denial_fails_without_retry(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> tuple[int, int, str, _AuditRow, tuple[UUID, UUID]]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)
            await conn.execute("UPDATE budgets SET limit_kcu = 0 WHERE project = 'proj'")

            with caplog.at_level(logging.INFO, logger=_PROMOTION_LOGGER):
                first = await promote_pending(conn)
            second = await promote_pending(conn)
            state = await _state(conn, queued)
            audit = await _audit_row(conn, queued, "requested->failed")
        return first, second, state, audit, (resource.id, queued)

    first, second, state, audit, ids = asyncio.run(_run())
    resource_uuid, queued_id = ids
    assert (first, second, state) == (0, 0, "failed")
    # The terminate audit runs under the service principal but keeps the queued row's
    # agent_session, and carries the full reason/target args (a dropped session, wrong tool,
    # wrong object_kind, or None args all diverge from this).
    assert audit == (
        "system:reconciler",
        "bob-sess",
        "proj",
        "allocations.request",
        "allocations",
        args_digest(
            {"reason": "budget_exceeded", "project": "proj", "resource_id": str(resource_uuid)}
        ),
    )
    assert any(
        f"reconciler: queued allocation {queued_id} -> failed (budget_exceeded) at promotion"
        == record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    )


def test_promote_pending_affinity_denial_is_not_budget_failure(migrated_url: str) -> None:
    async def _run() -> tuple[int, str, int]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await conn.execute(
                "UPDATE resources SET owner_project = 'other' WHERE id = %s",
                (resource.id,),
            )
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)

            promoted = await promote_pending(conn)
            state = await _state(conn, queued)
            cur = await conn.execute(
                "SELECT count(*) FROM audit_log "
                "WHERE object_id = %s AND transition = 'requested->failed'",
                (queued,),
            )
            failed_audits = await cur.fetchone()
            assert failed_audits is not None
        return promoted, state, int(failed_audits[0])

    assert asyncio.run(_run()) == (0, "requested", 0)


def test_promote_pending_one_candidate_failure_does_not_starve_siblings(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    # The oldest candidate raises (its persisted sizing snapshot is corrupt); the sweep must
    # log-and-continue so the younger placeable request is still promoted this pass. A
    # break-on-error regression would leave the sibling queued.
    async def _run() -> tuple[int, str, str, list[str], UUID]:
        async with _conn(migrated_url) as conn:
            first = await _resource(conn)
            second = await _resource(conn)
            await _quota(conn)
            h1 = await _granted(conn, first.id)  # fill first host's single slot
            broken = await _queued(conn, first, created_offset=timedelta(minutes=-10))
            h2 = await _granted(conn, second.id)  # fill second host's single slot
            healthy = await _queued(conn, second, created_offset=timedelta(minutes=-5))
            # Corrupt the oldest row's sizing snapshot so _selector_from_snapshot raises.
            await conn.execute(
                "UPDATE allocations SET requested_vcpus = NULL WHERE id = %s", (broken,)
            )
            for holder in (h1, h2):  # free both hosts so promotion can place
                await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
                await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)
            with caplog.at_level(logging.WARNING, logger=_PROMOTION_LOGGER):
                promoted = await promote_pending(conn)
            broken_state = await _state(conn, broken)
            healthy_state = await _state(conn, healthy)
            warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        return promoted, broken_state, healthy_state, warnings, broken

    promoted, broken_state, healthy_state, warnings, broken = asyncio.run(_run())
    assert (promoted, broken_state, healthy_state) == (1, "requested", "granted")
    assert f"reconciler: promoting allocation {broken} failed; retry next pass" in warnings


def test_budget_terminate_requires_budget_reason() -> None:
    affinity_denial = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.ALLOCATION_DENIED,
        reason=AFFINITY_DENIAL_REASON,
        queueable=False,
    )
    budget_denial = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.ALLOCATION_DENIED,
        reason=BUDGET_DENIAL_REASON,
        queueable=False,
    )

    assert not _is_budget_terminate(affinity_denial)
    assert _is_budget_terminate(budget_denial)


def test_budget_terminate_only_when_not_queueable() -> None:
    # A queueable ALLOCATION_DENIED with the budget reason is a wait, not a terminate: the
    # `not queueable` guard is load-bearing.
    queueable_budget = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.ALLOCATION_DENIED,
        reason=BUDGET_DENIAL_REASON,
        queueable=True,
    )
    assert not _is_budget_terminate(queueable_budget)


def test_budget_terminate_defensive_getattr_on_partial_objects() -> None:
    # The reads are defensive getattrs (denial: object). A denial lacking `queueable` defaults
    # to not-queueable (terminate); a bare object with no category/reason is never a terminate.
    partial = SimpleNamespace(category=ErrorCategory.ALLOCATION_DENIED, reason=BUDGET_DENIAL_REASON)
    assert _is_budget_terminate(partial)
    assert not _is_budget_terminate(object())


def test_selector_from_snapshot_carries_cost_class() -> None:
    alloc = _snapshot_alloc(requested_vcpus=3, requested_memory_gb=6)
    selector = _selector_from_snapshot(alloc, _bare_resource(cost_class="premium"))
    assert selector == Selector(vcpus=3, memory_gb=6, cost_class="premium")


def test_selector_from_snapshot_missing_sizing_fails_closed() -> None:
    alloc = _snapshot_alloc(requested_vcpus=None, requested_memory_gb=None)
    with pytest.raises(CategorizedError) as exc:
        _selector_from_snapshot(alloc, _bare_resource())
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "queued allocation is missing requested sizing snapshot"
    assert exc.value.details == {
        "allocation_id": str(alloc.id),
        "missing": ["requested_vcpus", "requested_memory_gb"],
    }


def test_request_from_queued_threads_every_persisted_input() -> None:
    target_id = uuid4()
    alloc = _snapshot_alloc(
        requested_vcpus=4,
        requested_memory_gb=8,
        requested_disk_gb=40,
        shape="gpu-large",
        requested_pcie_specs=["8086:1572"],
        requested_kind=ResourceKind.LOCAL_LIBVIRT,
        requested_resource_id=target_id,
        requested_pool="big",
        requested_arch=_ARCH,
        principal="carol",
        agent_session="carol-sess",
        project="projX",
    )
    resource = _bare_resource(cost_class="premium")
    request = _request_from_queued(alloc, resource)
    assert request.ctx.principal == "carol"
    assert request.ctx.agent_session == "carol-sess"
    assert request.ctx.projects == ("projX",)
    assert request.resource is resource
    assert request.project == "projX"
    assert request.selector == Selector(vcpus=4, memory_gb=8, cost_class="premium")
    assert request.window is None
    assert request.disk_gb == 40
    assert request.shape == "gpu-large"
    assert request.pcie_specs == ("8086:1572",)
    assert request.requested_kind is ResourceKind.LOCAL_LIBVIRT
    assert request.requested_resource_id == target_id
    assert request.requested_pool == "big"
    assert request.arch == _ARCH


def test_reap_queue_timeouts_fails_only_aged_requested_rows(migrated_url: str) -> None:
    async def _run() -> tuple[int, str, str]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            await _granted(conn, resource.id)
            aged = await _queued(conn, resource, created_offset=timedelta(hours=-48))
            young = await _queued(conn, resource, created_offset=timedelta(minutes=-5))

            reaped = await reap_queue_timeouts(conn, timedelta(hours=24))
            aged_state = await _state(conn, aged)
            young_state = await _state(conn, young)
        return reaped, aged_state, young_state

    assert asyncio.run(_run()) == (1, "failed", "requested")


def test_reap_queue_timeouts_counts_every_reaped_row(migrated_url: str) -> None:
    # Two aged rows must both be reaped and counted; a `reaped = 1` regression would report 1.
    async def _run() -> int:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            await _granted(conn, resource.id)  # fill the slot so both rows enqueue
            await _queued(conn, resource, created_offset=timedelta(hours=-48))
            await _queued(conn, resource, created_offset=timedelta(hours=-49))
            return await reap_queue_timeouts(conn, timedelta(hours=24))

    assert asyncio.run(_run()) == 2


def test_still_aged_requested_absent_row_is_false(migrated_url: str) -> None:
    # The locked re-read must fail closed on a missing row (never reap a row it cannot see):
    # a `row is None -> True` regression would report an absent row as still-aged-requested.
    async def _run() -> bool:
        async with _conn(migrated_url) as conn:
            return await _still_aged_requested(conn, uuid4(), timedelta(hours=24))

    assert asyncio.run(_run()) is False


def test_budget_terminate_writes_allocation_denied_failure_category(migrated_url: str) -> None:
    async def _run() -> str | None:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)
            await conn.execute("UPDATE budgets SET limit_kcu = 0 WHERE project = 'proj'")

            await promote_pending(conn)
            return await _failure_category(conn, queued)

    assert asyncio.run(_run()) == "allocation_denied"


def test_reap_queue_timeouts_writes_queue_timeout_failure_category(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> tuple[str | None, _AuditRow, list[str], UUID]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            await _granted(conn, resource.id)
            aged = await _queued(conn, resource, created_offset=timedelta(hours=-48))

            with caplog.at_level(logging.INFO, logger=_PROMOTION_LOGGER):
                await reap_queue_timeouts(conn, timedelta(hours=24))
            category = await _failure_category(conn, aged)
            audit = await _audit_row(conn, aged, "requested->failed")
            infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        return category, audit, infos, aged

    category, audit, infos, aged = asyncio.run(_run())
    assert category == "queue_timeout"
    # The reap audit runs under the service principal, keeps the row's agent_session, and uses
    # the distinct reap tool + queue_timeout reason.
    assert audit == (
        "system:reconciler",
        "bob-sess",
        "proj",
        "reconciler.reap_queue_timeout",
        "allocations",
        args_digest({"reason": "queue_timeout", "project": "proj"}),
    )
    assert f"reconciler: queued allocation {aged} -> failed (queue_timeout)" in infos


# --- The savepoint trap (ADR-0506) ---------------------------------------------------------
#
# Every test above runs the sweeps on an autocommit connection, where the trap cannot appear:
# there `conn.transaction()` is a real transaction no matter what ran before it. The
# reconciler's `create_pool` sets no `autocommit`, so `_run_repair_plan` hands each pass a
# **non-autocommit** connection — the one shape on which a statement issued before a
# `conn.transaction()` silently demotes it to a savepoint that commits nothing and releases no
# `pg_advisory_xact_lock`. These tests use that connection.


@asynccontextmanager
async def _pooled_conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """A non-autocommit connection, as ``create_pool`` hands each reconciler repair."""
    conn = await psycopg.AsyncConnection.connect(url, autocommit=False)
    try:
        yield conn
    finally:
        await conn.close()


async def _backend_pid(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute("SELECT pg_backend_pid()")
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _advisory_locks_held(observer: psycopg.AsyncConnection, pid: int) -> int:
    """Granted advisory locks held by backend ``pid``, counted from *another* connection.

    Counting from the sweep's own connection would issue the statement that opens the
    transaction under test, so the probe has to be external.
    """
    cur = await observer.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s AND granted",
        (pid,),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _two_queued_on_freed_hosts(conn: psycopg.AsyncConnection) -> tuple[UUID, UUID]:
    """Two queued requests, one per single-slot host, with both slots freed again."""
    first = await _resource(conn)
    second = await _resource(conn)
    await _quota(conn)
    holders = [await _granted(conn, first.id), await _granted(conn, second.id)]
    queued = (
        await _queued(conn, first, created_offset=timedelta(minutes=-10)),
        await _queued(conn, second, created_offset=timedelta(minutes=-5)),
    )
    for holder in holders:
        await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
        await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)
    return queued


def test_promote_pending_enters_each_candidate_holding_no_advisory_lock(migrated_url: str) -> None:
    # The out-of-order acquisition this pins: under the savepoint trap a candidate's
    # PROJECT/RESOURCE/ALLOCATION locks are never released, so candidate 2 takes PROJECT while
    # still holding candidate 1's RESOURCE and ALLOCATION — the reverse of the order a
    # concurrent `admit` takes them in (PROJECT then RESOURCE), which is an ABBA deadlock.
    async def _run() -> tuple[int, list[int]]:
        async with _pooled_conn(migrated_url) as conn, _conn(migrated_url) as observer:
            await _two_queued_on_freed_hosts(conn)
            pid = await _backend_pid(conn)
            await conn.commit()  # the setup is the caller's transaction, not the sweep's

            held_on_entry: list[int] = []
            real_promote_one = promotion._promote_one

            async def _recording(c: psycopg.AsyncConnection, alloc_id: UUID) -> Allocation | None:
                held_on_entry.append(await _advisory_locks_held(observer, pid))
                return await real_promote_one(c, alloc_id)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(promotion, "_promote_one", _recording)
                promoted = await promote_pending(conn)
            return promoted, held_on_entry

    promoted, held_on_entry = asyncio.run(_run())
    # Both candidates must actually have run, or the per-entry list would be vacuously clean.
    assert promoted == 2
    assert held_on_entry == [0, 0]


def test_promote_pending_commits_each_grant_before_the_pass_returns(migrated_url: str) -> None:
    # Read from a second connection while the sweep's is still open: under the trap every
    # grant sits in one transaction the sweep never commits, so an observer sees `requested`
    # — falsifying the module docstring's "each in its own committed transaction".
    async def _run() -> tuple[int, list[str]]:
        async with _pooled_conn(migrated_url) as conn, _conn(migrated_url) as observer:
            queued = await _two_queued_on_freed_hosts(conn)
            await conn.commit()
            promoted = await promote_pending(conn)
            states = [await _state(observer, alloc_id) for alloc_id in queued]
        return promoted, states

    assert asyncio.run(_run()) == (2, ["granted", "granted"])


def test_promote_pending_rejects_a_connection_already_in_a_transaction(migrated_url: str) -> None:
    # The mutation that proves the entry guard fires: one bare `execute` ahead of the sweep,
    # which is exactly how a caller dirties a pooled connection.
    async def _run() -> tuple[str, list[str]]:
        async with _pooled_conn(migrated_url) as conn, _conn(migrated_url) as observer:
            queued = await _two_queued_on_freed_hosts(conn)
            await conn.commit()
            await conn.execute("SELECT 1")
            with pytest.raises(RuntimeError) as excinfo:
                await promote_pending(conn)
            states = [await _state(observer, alloc_id) for alloc_id in queued]
        return str(excinfo.value), states

    message, states = asyncio.run(_run())
    assert "would open a savepoint" in message
    assert states == ["requested", "requested"]  # refused, not run under the trap


def test_promote_pending_stops_when_a_candidate_leaves_its_transaction_open(
    migrated_url: str,
) -> None:
    # The per-candidate guard, distinct from the entry guard. `_promote_one` is wrapped to
    # reproduce the pre-fix shape exactly — its `SELECT project` ran before the
    # `conn.transaction()` — so candidate 1's locks are still held when candidate 2 starts.
    # The pass must abort rather than take PROJECT on top of them.
    async def _run() -> tuple[str, int, str]:
        async with _pooled_conn(migrated_url) as conn, _conn(migrated_url) as observer:
            queued = await _two_queued_on_freed_hosts(conn)
            pid = await _backend_pid(conn)
            await conn.commit()
            real_promote_one = promotion._promote_one

            async def _dirtying(c: psycopg.AsyncConnection, alloc_id: UUID) -> Allocation | None:
                await c.execute("SELECT 1")  # where the pre-fix project read ran
                return await real_promote_one(c, alloc_id)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(promotion, "_promote_one", _dirtying)
                with pytest.raises(RuntimeError) as excinfo:
                    await promote_pending(conn)
            leaked_locks = await _advisory_locks_held(observer, pid)
            first_state = await _state(observer, queued[0])
        return str(excinfo.value), leaked_locks, first_state

    message, leaked_locks, first_state = asyncio.run(_run())
    assert "would open a savepoint" in message
    # PROJECT + RESOURCE + ALLOCATION from candidate 1, still held when candidate 2 was
    # refused. A `leaked_locks == 0` here would mean the guard fired on a harmless state.
    assert leaked_locks == 3
    assert first_state == "requested"  # its grant is uncommitted, as the trap implies


def test_reap_queue_timeouts_commits_each_row_before_the_pass_returns(migrated_url: str) -> None:
    async def _run() -> tuple[int, list[str]]:
        async with _pooled_conn(migrated_url) as conn, _conn(migrated_url) as observer:
            resource = await _resource(conn)
            await _quota(conn)
            await _granted(conn, resource.id)  # fill the slot so both rows enqueue
            aged = (
                await _queued(conn, resource, created_offset=timedelta(hours=-48)),
                await _queued(conn, resource, created_offset=timedelta(hours=-49)),
            )
            await conn.commit()
            reaped = await reap_queue_timeouts(conn, timedelta(hours=24))
            states = [await _state(observer, alloc_id) for alloc_id in aged]
        return reaped, states

    assert asyncio.run(_run()) == (2, ["failed", "failed"])


def test_reap_queue_timeouts_rejects_a_connection_already_in_a_transaction(
    migrated_url: str,
) -> None:
    async def _run() -> tuple[str, str]:
        async with _pooled_conn(migrated_url) as conn, _conn(migrated_url) as observer:
            resource = await _resource(conn)
            await _quota(conn)
            await _granted(conn, resource.id)
            aged = await _queued(conn, resource, created_offset=timedelta(hours=-48))
            await conn.commit()
            await conn.execute("SELECT 1")
            with pytest.raises(RuntimeError) as excinfo:
                await reap_queue_timeouts(conn, timedelta(hours=24))
            state = await _state(observer, aged)
        return str(excinfo.value), state

    message, state = asyncio.run(_run())
    assert "would open a savepoint" in message
    assert state == "requested"
