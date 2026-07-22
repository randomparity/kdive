"""Behavior tests for redacted artifact listing service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.artifacts import Sensitivity
from kdive.mcp.auth import RequestContext
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.services.artifacts.listing import (
    SystemArtifactPage,
    latest_run_console_artifact_id,
    list_redacted_system_artifacts,
)
from tests.mcp._seed import seed_crashed_system, seed_run_on_system

_DT = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx(
    *, projects: tuple[str, ...] = ("proj",), role: Role | None = Role.VIEWER
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


async def _artifact(
    pool: AsyncConnectionPool,
    system_id: str,
    name: str,
    *,
    sensitivity: Sensitivity = Sensitivity.REDACTED,
    created_offset: timedelta = timedelta(0),
    run_id: str | None = None,
) -> str:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO artifacts "
            "(created_at, updated_at, owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class, run_id) VALUES (%s, %s, 'systems', %s, %s, 'e', %s, 'console', %s) "
            "RETURNING id",
            (
                _DT + created_offset,
                _DT + created_offset,
                system_id,
                f"k/systems/{system_id}/{name}",
                sensitivity.value,
                run_id,
            ),
        )
        row = await cur.fetchone()
    assert row is not None
    return str(row["id"])


def test_listing_returns_authorized_redacted_artifacts_newest_first(migrated_url: str) -> None:
    async def _run() -> tuple[SystemArtifactPage, str, str]:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            older = await _artifact(pool, system_id, "older", created_offset=timedelta(minutes=1))
            await _artifact(pool, system_id, "raw", sensitivity=Sensitivity.SENSITIVE)
            await _artifact(pool, system_id, "quarantine", sensitivity=Sensitivity.QUARANTINED)
            newer = await _artifact(pool, system_id, "newer", created_offset=timedelta(minutes=2))

            page = await list_redacted_system_artifacts(pool, _ctx(), system_id=system_id)

        return page, newer, older

    page, newer_id, older_id = asyncio.run(_run())
    assert [item.id for item in page.items] == [newer_id, older_id]
    assert page.items[0].object_key.endswith("/newer")
    assert page.items[1].object_key.endswith("/older")
    assert page.truncated is False
    assert page.next_key is None


def test_listing_hides_invalid_missing_and_foreign_system_ids(migrated_url: str) -> None:
    async def _run() -> tuple[SystemArtifactPage, SystemArtifactPage, SystemArtifactPage]:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool, project="other")
            invalid = await list_redacted_system_artifacts(pool, _ctx(), system_id="not-a-uuid")
            missing = await list_redacted_system_artifacts(
                pool,
                _ctx(),
                system_id="00000000-0000-0000-0000-000000000000",
            )
            foreign = await list_redacted_system_artifacts(pool, _ctx(), system_id=system_id)
        return invalid, missing, foreign

    empty = SystemArtifactPage([], False, None)
    assert asyncio.run(_run()) == (empty, empty, empty)


def test_listing_keyset_paginates_capped_system_scope(migrated_url: str) -> None:
    """A System-scoped list over its limit reports truncation and pages the remainder (ADR-0374)."""

    async def _run() -> tuple[list[str], list[SystemArtifactPage]]:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            newest_to_oldest = [
                await _artifact(pool, system_id, f"a{i}", created_offset=timedelta(minutes=5 - i))
                for i in range(5)
            ]
            pages: list[SystemArtifactPage] = []
            after = None
            for _ in range(3):
                page = await list_redacted_system_artifacts(
                    pool, _ctx(), system_id=system_id, limit=2, after=after
                )
                pages.append(page)
                after = page.next_key
        return newest_to_oldest, pages

    ids_newest_first, pages = asyncio.run(_run())
    first, second, last = pages
    assert [item.id for item in first.items] == ids_newest_first[:2]
    assert first.truncated is True and first.next_key is not None
    # The continuation page seeks strictly past the prior page — no overlap, order preserved.
    assert [item.id for item in second.items] == ids_newest_first[2:4]
    assert second.truncated is True
    # The final page drains the remainder and reports no further pages.
    assert [item.id for item in last.items] == ids_newest_first[4:]
    assert last.truncated is False and last.next_key is None


def test_listing_exact_limit_page_is_not_truncated(migrated_url: str) -> None:
    """A page filled to exactly ``limit`` with no further row reports no truncation.

    Pins ``truncated = len(rows) > limit`` (a ``>=`` mutant would over-report truncation and
    hand back a dangling ``next_key`` for an empty next page).
    """

    async def _run() -> SystemArtifactPage:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            for i in range(2):
                await _artifact(pool, system_id, f"a{i}", created_offset=timedelta(minutes=i + 1))
            return await list_redacted_system_artifacts(pool, _ctx(), system_id=system_id, limit=2)

    page = asyncio.run(_run())
    assert len(page.items) == 2
    assert page.truncated is False
    assert page.next_key is None


def test_listing_limit_one_keeps_single_row_and_reports_truncation(migrated_url: str) -> None:
    """``limit=1`` keeps exactly one row and still detects a further page.

    Pins the ``max(1, limit)`` slice and truncation caps: a ``max(2, limit)`` mutant would keep
    two rows or under-report truncation when ``limit == 1``.
    """

    async def _run() -> SystemArtifactPage:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            for i in range(3):
                await _artifact(pool, system_id, f"a{i}", created_offset=timedelta(minutes=i + 1))
            return await list_redacted_system_artifacts(pool, _ctx(), system_id=system_id, limit=1)

    page = asyncio.run(_run())
    assert len(page.items) == 1
    assert page.truncated is True
    assert page.next_key is not None


def test_listing_next_key_is_last_kept_rows_created_at_and_id(migrated_url: str) -> None:
    """``next_key`` carries the *last kept* row's ``(created_at, id)`` — not any earlier row.

    With three kept rows the last-row index (``-1``) is distinct from index ``+1``, so this pins
    both ``next_key`` tuple components against a wrong-index mutant.
    """

    async def _run() -> tuple[SystemArtifactPage, list[str]]:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            ids_newest_first = [
                await _artifact(pool, system_id, f"a{i}", created_offset=timedelta(minutes=4 - i))
                for i in range(4)
            ]
            page = await list_redacted_system_artifacts(pool, _ctx(), system_id=system_id, limit=3)
            return page, ids_newest_first

    page, ids_newest_first = asyncio.run(_run())
    assert [item.id for item in page.items] == ids_newest_first[:3]
    assert page.truncated is True
    assert page.next_key is not None
    last_kept_offset = timedelta(minutes=4 - 2)  # third-newest of a0..a3
    assert page.next_key == (_DT + last_kept_offset, UUID(ids_newest_first[2]))


def test_latest_console_resolves_newest_correlated_console(migrated_url: str) -> None:
    """`latest_run_console_artifact_id` resolves the newest Run-correlated console (ADR-0374)."""

    async def _run() -> tuple[str | None, str]:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id=None)
            await _artifact(
                pool, system_id, f"console-{run_id}", created_offset=timedelta(0), run_id=run_id
            )
            newest = await _artifact(
                pool,
                system_id,
                "console-part-0-000001",
                created_offset=timedelta(seconds=20),
                run_id=run_id,
            )
            # A newer console on the same System but a different Run must NOT win.
            await _artifact(
                pool,
                system_id,
                "console-part-9-000000",
                created_offset=timedelta(seconds=30),
                run_id=None,
            )
            async with pool.connection() as conn:
                resolved = await latest_run_console_artifact_id(conn, run_id)
        return resolved, newest

    resolved, newest = asyncio.run(_run())
    assert resolved == newest


def test_latest_console_none_when_no_correlated_console(migrated_url: str) -> None:
    async def _run() -> str | None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id=None)
            async with pool.connection() as conn:
                return await latest_run_console_artifact_id(conn, run_id)

    assert asyncio.run(_run()) is None


def test_listing_requires_viewer_role_for_project_member(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            with pytest.raises(AuthorizationError):
                await list_redacted_system_artifacts(pool, _ctx(role=None), system_id=system_id)

    asyncio.run(_run())
