"""Service-level behavior tests for allocation lease renewal (ADR-0036 §3)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import ALLOCATIONS, BUDGETS, RESOURCES
from kdive.domain.accounting.records import Budget
from kdive.domain.capacity.state import AllocationState, ResourceStatus
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.security.audit import args_digest
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.allocation.renew import _extension_estimate, renew

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_CTX = RequestContext(
    principal="alice", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
)


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed(conn: psycopg.AsyncConnection, *, limit: str = "1000000") -> Resource:
    await BUDGETS.upsert(
        conn,
        Budget(project="proj", limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT),
    )
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={CONCURRENT_ALLOCATION_CAP_KEY: 10, "vcpus": 64, "memory_mb": 65536},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _alloc(
    conn: psycopg.AsyncConnection,
    resource: Resource,
    *,
    lease_expiry: datetime,
    state: AllocationState = AllocationState.GRANTED,
    requested_vcpus: int | None = 2,
    requested_memory_gb: int | None = 4,
) -> Allocation:
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            agent_session="s",
            project="proj",
            resource_id=resource.id,
            state=state,
            lease_expiry=lease_expiry,
            requested_vcpus=requested_vcpus,
            requested_memory_gb=requested_memory_gb,
            requested_disk_gb=10,
        ),
    )


async def _lease(conn: psycopg.AsyncConnection, alloc_id: UUID) -> datetime | None:
    cur = await conn.execute("SELECT lease_expiry FROM allocations WHERE id = %s", (alloc_id,))
    row = await cur.fetchone()
    return row[0] if row else None


def test_renew_extends_returned_and_persisted_lease(migrated_url: str) -> None:
    async def _run() -> tuple[bool, datetime | None, datetime | None, datetime]:
        async with _conn(migrated_url) as conn:
            res = await _seed(conn)
            base = datetime.now(UTC) + timedelta(hours=2)
            alloc = await _alloc(conn, res, lease_expiry=base)
            outcome = await renew(conn, _CTX, allocation_id=alloc.id, extend="1")
            persisted = await _lease(conn, alloc.id)
            returned = outcome.allocation.lease_expiry if outcome.allocation else None
        return outcome.renewed, returned, persisted, base

    renewed, returned, persisted, base = asyncio.run(_run())
    assert renewed is True
    # The extension shows in BOTH the returned model (model_copy) and the persisted row.
    assert returned == base + timedelta(hours=1)
    assert persisted == base + timedelta(hours=1)


def test_renew_audit_fields(migrated_url: str) -> None:
    async def _run() -> tuple[tuple[str, str, str, str] | None, UUID]:
        async with _conn(migrated_url) as conn:
            res = await _seed(conn)
            base = datetime.now(UTC) + timedelta(hours=2)
            alloc = await _alloc(conn, res, lease_expiry=base)
            await renew(conn, _CTX, allocation_id=alloc.id, extend="1")
            cur = await conn.execute(
                "SELECT tool, object_kind, args_digest, project FROM audit_log "
                "WHERE object_id = %s AND transition LIKE 'renew:%%'",
                (alloc.id,),
            )
            audit = await cur.fetchone()
        return audit, alloc.id

    audit, alloc_id = asyncio.run(_run())
    assert audit == (
        "allocations.renew",
        "allocations",
        args_digest({"allocation_id": str(alloc_id)}),
        "proj",
    )


def test_renew_non_positive_extend_is_config_error(migrated_url: str) -> None:
    async def _run() -> tuple[bool, ErrorCategory | None, bool]:
        async with _conn(migrated_url) as conn:
            res = await _seed(conn)
            alloc = await _alloc(conn, res, lease_expiry=datetime.now(UTC) + timedelta(hours=2))
            outcome = await renew(conn, _CTX, allocation_id=alloc.id, extend="0")
        return outcome.renewed, outcome.category, bool(outcome.details)

    renewed, category, has_details = asyncio.run(_run())
    assert renewed is False
    assert category is ErrorCategory.CONFIGURATION_ERROR
    assert has_details


def test_renew_over_budget_denies(migrated_url: str) -> None:
    async def _run() -> tuple[bool, ErrorCategory | None, datetime | None, datetime]:
        async with _conn(migrated_url) as conn:
            res = await _seed(conn, limit="0.0001")
            base = datetime.now(UTC) + timedelta(hours=2)
            alloc = await _alloc(conn, res, lease_expiry=base)
            outcome = await renew(conn, _CTX, allocation_id=alloc.id, extend="1")
            persisted = await _lease(conn, alloc.id)
        return outcome.renewed, outcome.category, persisted, base

    renewed, category, persisted, base = asyncio.run(_run())
    assert renewed is False
    assert category is ErrorCategory.ALLOCATION_DENIED
    assert persisted == base  # window unchanged on denial


def test_renew_terminal_is_stale_handle(migrated_url: str) -> None:
    async def _run() -> tuple[bool, ErrorCategory | None]:
        async with _conn(migrated_url) as conn:
            res = await _seed(conn)
            alloc = await _alloc(
                conn,
                res,
                lease_expiry=datetime.now(UTC) + timedelta(hours=2),
                state=AllocationState.RELEASED,
            )
            outcome = await renew(conn, _CTX, allocation_id=alloc.id, extend="1")
        return outcome.renewed, outcome.category

    renewed, category = asyncio.run(_run())
    assert renewed is False
    assert category is ErrorCategory.STALE_HANDLE


def test_renew_missing_size_fails_closed(migrated_url: str) -> None:
    # requested_vcpus is None but memory is present: the size check must fail closed
    # (the `or` guard, not `and`), so pricing never runs on a partial snapshot.
    async def _run() -> tuple[bool, ErrorCategory | None, bool]:
        async with _conn(migrated_url) as conn:
            res = await _seed(conn)
            alloc = await _alloc(
                conn,
                res,
                lease_expiry=datetime.now(UTC) + timedelta(hours=2),
                requested_vcpus=None,
                requested_memory_gb=4,
            )
            outcome = await renew(conn, _CTX, allocation_id=alloc.id, extend="1")
        return outcome.renewed, outcome.category, bool(outcome.details)

    renewed, category, has_details = asyncio.run(_run())
    assert renewed is False
    assert category is ErrorCategory.CONFIGURATION_ERROR
    assert has_details


def test_extension_estimate_missing_size_message_and_details() -> None:
    # The no-size guard runs before any DB access, so this needs no connection. Pin the
    # message and details so a blanked message or a mangled details key/value is caught.
    alloc = Allocation(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="alice",
        project="proj",
        resource_id=uuid4(),
        state=AllocationState.GRANTED,
        requested_vcpus=None,
        requested_memory_gb=4,
    )

    async def _run() -> None:
        with pytest.raises(CategorizedError) as exc:
            await _extension_estimate(cast(psycopg.AsyncConnection, object()), alloc, Decimal("1"))
        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert str(exc.value) == f"allocation {alloc.id} has no persisted size to renew"
        assert exc.value.details == {"allocation_id": str(alloc.id)}

    asyncio.run(_run())


def test_renew_small_fractional_extension_succeeds(migrated_url: str) -> None:
    # A sub-hour extension (0.5h) is a real positive added window; a `> 0` -> `> 1`
    # regression on the clamp threshold would wrongly reject it.
    async def _run() -> tuple[bool, datetime | None, datetime]:
        async with _conn(migrated_url) as conn:
            res = await _seed(conn)
            base = datetime.now(UTC) + timedelta(hours=2)
            alloc = await _alloc(conn, res, lease_expiry=base)
            outcome = await renew(conn, _CTX, allocation_id=alloc.id, extend="0.5")
            persisted = await _lease(conn, alloc.id)
        return outcome.renewed, persisted, base

    renewed, persisted, base = asyncio.run(_run())
    assert renewed is True
    assert persisted == base + timedelta(minutes=30)


def test_renew_at_cap_is_config_error(migrated_url: str) -> None:
    # The lease already sits at the 24h max: the clamp yields a zero billable window, which
    # fails closed rather than charging nothing.
    async def _run() -> tuple[bool, ErrorCategory | None, datetime | None, datetime]:
        async with _conn(migrated_url) as conn:
            res = await _seed(conn)
            base = datetime.now(UTC) + timedelta(hours=48)  # already past the 24h cap
            alloc = await _alloc(conn, res, lease_expiry=base)
            outcome = await renew(conn, _CTX, allocation_id=alloc.id, extend="1")
            persisted = await _lease(conn, alloc.id)
        return outcome.renewed, outcome.category, persisted, base

    renewed, category, persisted, base = asyncio.run(_run())
    assert renewed is False
    assert category is ErrorCategory.CONFIGURATION_ERROR
    assert persisted == base
