"""Service-level tests for Investigation read helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import InvestigationState
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, RoleDenied
from kdive.services.investigations.common import (
    InvestigationErrorReason,
    InvestigationServiceError,
)
from kdive.services.investigations.read import (
    fetch_investigation_rows,
    get_investigation_record,
    list_investigation_rows,
)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx(
    *,
    projects: tuple[str, ...] = ("proj",),
    roles: dict[str, Role] | None = None,
) -> RequestContext:
    return RequestContext(
        principal="reader",
        agent_session="session-1",
        projects=projects,
        roles=roles if roles is not None else {"proj": Role.VIEWER},
    )


async def _insert_investigation(
    conn: AsyncConnection,
    *,
    uid: UUID,
    project: str,
    title: str,
    state: InvestigationState = InvestigationState.OPEN,
    created_at: datetime,
) -> None:
    await conn.execute(
        "INSERT INTO investigations "
        "(id, principal, project, title, state, created_at, updated_at) "
        "VALUES (%s, 'reader', %s, %s, %s, %s, %s)",
        (uid, project, title, state.value, created_at, created_at),
    )


def test_get_investigation_record_authorizes_project_membership(migrated_url: str) -> None:
    async def scenario() -> None:
        inv_id = uuid4()
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _insert_investigation(
                    conn,
                    uid=inv_id,
                    project="proj",
                    title="visible",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )

            record = await get_investigation_record(pool, _ctx(), inv_id, raw_id="raw-id")
            assert record.id == inv_id
            assert record.title == "visible"

            with pytest.raises(InvestigationServiceError) as missing:
                await get_investigation_record(pool, _ctx(), uuid4(), raw_id="missing")
            assert missing.value.object_id == "missing"
            assert missing.value.reason is InvestigationErrorReason.NOT_FOUND

            with pytest.raises(InvestigationServiceError) as wrong_project:
                await get_investigation_record(
                    pool,
                    _ctx(projects=("other",), roles={"other": Role.VIEWER}),
                    inv_id,
                    raw_id="hidden",
                )
            assert wrong_project.value.object_id == "hidden"
            assert wrong_project.value.reason is InvestigationErrorReason.NOT_FOUND

            with pytest.raises(RoleDenied):
                await get_investigation_record(
                    pool,
                    _ctx(roles={}),
                    inv_id,
                    raw_id=str(inv_id),
                )

    asyncio.run(scenario())


def test_list_investigation_rows_filters_viewer_projects_and_explicit_project(
    migrated_url: str,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _insert_investigation(
                    conn, uid=uuid4(), project="proj", title="mine", created_at=now
                )
                await _insert_investigation(
                    conn,
                    uid=uuid4(),
                    project="other",
                    title="also-mine",
                    created_at=now + timedelta(seconds=1),
                )
                await _insert_investigation(
                    conn,
                    uid=uuid4(),
                    project="hidden",
                    title="not-mine",
                    created_at=now + timedelta(seconds=2),
                )
            ctx = _ctx(
                projects=("proj", "other", "hidden"),
                roles={"proj": Role.VIEWER, "other": Role.CONTRIBUTOR},
            )

            rows = await list_investigation_rows(
                pool, ctx, project=None, state=None, limit=10, after=None
            )
            narrowed = await list_investigation_rows(
                pool, ctx, project="other", state=None, limit=10, after=None
            )
            forbidden_narrow = await list_investigation_rows(
                pool, ctx, project="hidden", state=None, limit=10, after=None
            )

        assert {row["title"] for row in rows} == {"mine", "also-mine"}
        assert [row["title"] for row in narrowed] == ["also-mine"]
        assert forbidden_narrow == []

    asyncio.run(scenario())


def test_list_investigation_rows_passes_state_limit_and_after_through(migrated_url: str) -> None:
    """`list_investigation_rows` forwards state, limit, and after to the keyset fetch."""

    async def scenario() -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        open_a, open_b, open_c = uuid4(), uuid4(), uuid4()
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _insert_investigation(
                    conn, uid=open_a, project="proj", title="open-a", created_at=base
                )
                await _insert_investigation(
                    conn,
                    uid=open_b,
                    project="proj",
                    title="open-b",
                    created_at=base + timedelta(seconds=1),
                )
                await _insert_investigation(
                    conn,
                    uid=open_c,
                    project="proj",
                    title="open-c",
                    created_at=base + timedelta(seconds=2),
                )
                # Newest overall, but CLOSED: it must never appear under a state=OPEN filter.
                await _insert_investigation(
                    conn,
                    uid=uuid4(),
                    project="proj",
                    title="closed-newest",
                    state=InvestigationState.CLOSED,
                    created_at=base + timedelta(seconds=3),
                )

            first = await list_investigation_rows(
                pool, _ctx(), project=None, state=InvestigationState.OPEN, limit=2, after=None
            )
            cursor = (first[-1]["created_at"], first[-1]["id"])
            paged = await list_investigation_rows(
                pool, _ctx(), project=None, state=InvestigationState.OPEN, limit=2, after=cursor
            )

        # state filter drops the newer CLOSED row; limit caps the page at 2 (newest-first).
        assert [row["id"] for row in first] == [open_c, open_b]
        assert all(row["state"] == InvestigationState.OPEN.value for row in first)
        # after cursor seeks strictly past the first page — no overlap.
        assert [row["id"] for row in paged] == [open_a]

    asyncio.run(scenario())


def test_fetch_investigation_rows_applies_state_filter_and_keyset_paging(
    migrated_url: str,
) -> None:
    async def scenario() -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        old_open = uuid4()
        mid_open = uuid4()
        new_open = uuid4()
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            await _insert_investigation(
                conn,
                uid=old_open,
                project="proj",
                title="old-open",
                created_at=base,
            )
            await _insert_investigation(
                conn,
                uid=mid_open,
                project="proj",
                title="mid-open",
                created_at=base + timedelta(seconds=1),
            )
            await _insert_investigation(
                conn,
                uid=new_open,
                project="proj",
                title="new-open",
                created_at=base + timedelta(seconds=2),
            )
            await _insert_investigation(
                conn,
                uid=uuid4(),
                project="proj",
                title="newer-closed",
                state=InvestigationState.CLOSED,
                created_at=base + timedelta(seconds=3),
            )
            await _insert_investigation(
                conn,
                uid=uuid4(),
                project="other",
                title="other-open",
                created_at=base + timedelta(seconds=4),
            )

            first_page = await fetch_investigation_rows(
                conn,
                ("proj",),
                InvestigationState.OPEN,
                limit=2,
                after=None,
            )
            cursor = (first_page[-1]["created_at"], first_page[-1]["id"])
            second_page = await fetch_investigation_rows(
                conn,
                ("proj",),
                InvestigationState.OPEN,
                limit=2,
                after=cursor,
            )

        assert [row["id"] for row in first_page] == [new_open, mid_open]
        assert [row["title"] for row in first_page] == ["new-open", "mid-open"]
        assert [row["id"] for row in second_page] == [old_open]

    asyncio.run(scenario())
