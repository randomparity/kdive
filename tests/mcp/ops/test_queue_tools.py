"""Queue-control `ops.*` tool tests (#138, ADR-0062).

Handlers are called directly with an injected pool + RequestContext (the repo's unit
contract). Coverage maps to the #138 acceptance bullets:

* ``ops.set_queue_paused`` writes ``queue_paused`` to the requested state (ADR-0459);
  ``platform_operator`` gating enforced; success and (role-holding) denial audited.
* ``ops.jobs_list`` returns cross-project queue depth + per-job state; ``platform_operator``
  gating enforced; a state filter is validated.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, InstallPayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.ops import queue as ops_queue
from kdive.security.authz.rbac import PlatformRole
from tests.mcp.json_data import data_int
from tests.support.worker_fence import register_worker


def _ctx(
    *,
    platform_roles: frozenset[PlatformRole] = frozenset(),
    projects: tuple[str, ...] = (),
    principal: str = "op-1",
) -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-1",
        projects=projects,
        roles={},
        platform_roles=platform_roles,
    )


_OPERATOR = frozenset({PlatformRole.PLATFORM_OPERATOR})


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _authorizing(project: str) -> Authorizing:
    return Authorizing(principal="p", agent_session=None, project=project)


def _build_payload() -> InstallPayload:
    return InstallPayload(run_id=str(uuid4()))


async def _paused(url: str) -> bool:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        return await queue.is_queue_paused(conn)


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT principal, platform_role, tool, scope, args_digest "
            "FROM platform_audit_log ORDER BY ts"
        )
        return list(await cur.fetchall())


async def _count_platform_audit(url: str) -> int:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        cur = await conn.execute("SELECT count(*) FROM platform_audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_set_paused_true_sets_flag_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.set_queue_paused(
                pool, _ctx(platform_roles=_OPERATOR), paused=True
            )
        assert resp.status == "paused"
        assert resp.data["queue_paused"] is True
        assert await _paused(migrated_url) is True
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator" and rows[0][2] == "ops.set_queue_paused"

    asyncio.run(_run())


def test_set_paused_flag_and_audit_commit_atomically(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed audit write must roll back the flag flip: never a paused queue with no row.
    async def _run() -> None:
        async def boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("audit db error")

        monkeypatch.setattr(ops_queue.audit, "record_platform", boom)
        async with _pool(migrated_url) as pool:
            with pytest.raises(RuntimeError, match="audit db error"):
                await ops_queue.set_queue_paused(pool, _ctx(platform_roles=_OPERATOR), paused=True)
        assert await _paused(migrated_url) is False  # rolled back with the audit
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_set_paused_false_clears_flag_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await ops_queue.set_queue_paused(pool, _ctx(platform_roles=_OPERATOR), paused=True)
            resp = await ops_queue.set_queue_paused(
                pool, _ctx(platform_roles=_OPERATOR), paused=False
            )
        assert resp.status == "running"
        assert resp.data["queue_paused"] is False
        assert await _paused(migrated_url) is False
        rows = await _platform_audit_rows(migrated_url)
        assert [r[2] for r in rows] == ["ops.set_queue_paused"] * 2
        # One tool name now covers both directions, so the audit trail can only tell a
        # pause from a resume by its recorded args.
        assert rows[0][4] != rows[1][4]

    asyncio.run(_run())


@pytest.mark.parametrize("paused", [True, False])
def test_set_paused_is_idempotent_in_the_target_state(migrated_url: str, paused: bool) -> None:
    # A target-state write, not a toggle: re-asserting the state a second time leaves the
    # flag where the first call put it, and audits the operator's intent both times.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            first = await ops_queue.set_queue_paused(
                pool, _ctx(platform_roles=_OPERATOR), paused=paused
            )
            second = await ops_queue.set_queue_paused(
                pool, _ctx(platform_roles=_OPERATOR), paused=paused
            )
        assert first.status == second.status
        assert second.data["queue_paused"] is paused
        assert await _paused(migrated_url) is paused
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 2  # a no-op re-assert is still the operator's intent
        assert rows[0][4] == rows[1][4]

    asyncio.run(_run())


def test_set_paused_denied_for_project_only_token_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.set_queue_paused(pool, _ctx(projects=("proj-a",)), paused=True)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        # The blocker is actionable: the envelope names the grant to go ask for (ADR-0490).
        assert resp.data["missing_roles"] == ["platform_operator"]
        assert await _paused(migrated_url) is False  # flag untouched
        assert await _count_platform_audit(migrated_url) == 0  # no write-amplification

    asyncio.run(_run())


@pytest.mark.parametrize("paused", [True, False])
def test_set_paused_denied_for_auditor_is_audited(migrated_url: str, paused: bool) -> None:
    # platform_auditor does NOT satisfy the operator gate, but holds a platform role.
    # Neither branch of `paused` is privileged: both deny at the same shared gate.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await ops_queue.set_queue_paused(pool, ctx, paused=paused)
        assert resp.status == "error"
        assert resp.suggested_next_actions == ["session.whoami"]
        assert "ops.set_queue_paused" not in resp.suggested_next_actions  # ADR-0471, #1596
        assert await _paused(migrated_url) is False
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1 and rows[0][1] == "platform_auditor"
        assert rows[0][2] == "ops.set_queue_paused"

    asyncio.run(_run())


def test_jobs_list_returns_cross_project_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await queue.enqueue(
                    conn, JobKind.INSTALL, _build_payload(), _authorizing("proj-a"), "dk-a"
                )
                await queue.enqueue(
                    conn, JobKind.INSTALL, _build_payload(), _authorizing("proj-b"), "dk-b"
                )
            resp = await ops_queue.jobs_list(pool, _ctx(platform_roles=_OPERATOR))
        assert resp.status == "ok"
        depth = {
            key.removeprefix("depth_"): data_int(resp, key)
            for key, value in resp.data.items()
            if key.startswith("depth_")
        }
        assert depth == {"queued": 2}  # both projects counted, cross-project
        jobs = [item.data for item in resp.items]
        assert {j["project"] for j in jobs} == {"proj-a", "proj-b"}
        assert all("payload" not in j for j in jobs)  # untrusted payload not surfaced
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1 and rows[0][3] == "all-projects"

    asyncio.run(_run())


def test_jobs_list_filters_by_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await queue.enqueue(
                    conn, JobKind.INSTALL, _build_payload(), _authorizing("proj-a"), "dk-q"
                )
                running = await queue.enqueue(
                    conn, JobKind.INSTALL, _build_payload(), _authorizing("proj-a"), "dk-r"
                )
                await register_worker(conn, "w1")
                await conn.execute(
                    "UPDATE jobs SET state = 'running', worker_id = 'w1' WHERE id = %s",
                    (running.id,),
                )
            resp = await ops_queue.jobs_list(
                pool, _ctx(platform_roles=_OPERATOR), states=["running"]
            )
        jobs = [item.data for item in resp.items]
        assert [j["state"] for j in jobs] == ["running"]  # filtered per-job rows
        depth = {
            key.removeprefix("depth_"): data_int(resp, key)
            for key, value in resp.data.items()
            if key.startswith("depth_")
        }
        assert depth == {"queued": 1, "running": 1}  # depth still spans all states

    asyncio.run(_run())


def test_jobs_list_pages_without_hiding_overflow(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                seeded = [
                    await queue.enqueue(
                        conn,
                        JobKind.INSTALL,
                        _build_payload(),
                        _authorizing("proj-a"),
                        f"dk-{index}",
                    )
                    for index in range(3)
                ]
            first = await ops_queue.jobs_list(pool, _ctx(platform_roles=_OPERATOR), limit=2)
            cursor = first.data["next_cursor"]
            assert isinstance(cursor, str)
            second = await ops_queue.jobs_list(
                pool, _ctx(platform_roles=_OPERATOR), limit=2, cursor=cursor
            )

        assert first.data["truncated"] is True
        assert second.data["truncated"] is False
        assert second.data["next_cursor"] is None
        returned = [item.object_id for item in [*first.items, *second.items]]
        assert returned == [str(job.id) for job in reversed(seeded)]

    asyncio.run(_run())


def test_jobs_list_rejects_invalid_cursor(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            return await ops_queue.jobs_list(
                pool, _ctx(platform_roles=_OPERATOR), cursor="not-a-cursor"
            )

    response = asyncio.run(_run())
    assert response.status == "error"
    assert response.error_category == "configuration_error"
    assert response.data["reason"] == "invalid_cursor"


def test_jobs_list_renders_failed_job_with_category(migrated_url: str) -> None:
    # A failed job carries an error_category; the per-job item must thread it so the
    # category-iff-failure envelope invariant (ADR-0019) renders instead of raising (#582).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                failed = await queue.enqueue(
                    conn, JobKind.INSTALL, _build_payload(), _authorizing("proj-a"), "dk-f"
                )
                await conn.execute(
                    "UPDATE jobs SET state = 'failed', error_category = 'build_failure' "
                    "WHERE id = %s",
                    (failed.id,),
                )
            resp = await ops_queue.jobs_list(pool, _ctx(platform_roles=_OPERATOR))
        assert resp.status == "ok"
        items = {item.object_id: item for item in resp.items}
        item = items[str(failed.id)]
        assert item.status == "failed"
        assert item.error_category == "build_failure"
        assert item.retryable is False  # derived from the category (ADR-0118)
        assert item.data["state"] == "failed"

    asyncio.run(_run())


def test_jobs_list_degrades_failed_job_missing_category(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    # The schema permits a failed job with a null error_category; rendering must degrade
    # to a categorized failure rather than crash the whole list, and log the malformed
    # row so the silent normalization is observable (#582).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                failed = await queue.enqueue(
                    conn, JobKind.INSTALL, _build_payload(), _authorizing("proj-a"), "dk-n"
                )
                await conn.execute(
                    "UPDATE jobs SET state = 'failed', error_category = NULL WHERE id = %s",
                    (failed.id,),
                )
            resp = await ops_queue.jobs_list(pool, _ctx(platform_roles=_OPERATOR))
        assert resp.status == "ok"  # the list renders despite the malformed row
        item = {i.object_id: i for i in resp.items}[str(failed.id)]
        assert item.status == "failed"
        assert item.error_category == "infrastructure_failure"
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any(str(failed.id) in r.getMessage() for r in warnings)

    with caplog.at_level(logging.WARNING, logger="kdive.mcp.tools.ops.queue"):
        asyncio.run(_run())


def test_jobs_list_rejects_unknown_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.jobs_list(pool, _ctx(platform_roles=_OPERATOR), states=["bogus"])
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_jobs_list_denied_for_project_only_token(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.jobs_list(pool, _ctx(projects=("proj-a",)))
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_response_is_serializable(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.jobs_list(pool, _ctx(platform_roles=_OPERATOR))
        assert isinstance(resp, ToolResponse)
        json.dumps(resp.model_dump())  # the envelope round-trips to JSON

    asyncio.run(_run())


def test_admin_satisfies_operator_gate(migrated_url: str) -> None:
    # platform_admin does not imply platform_operator (separate axes); a pure admin is denied.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
            resp = await ops_queue.set_queue_paused(pool, ctx, paused=True)
        assert resp.status == "error"  # operator gate not satisfied by admin

    asyncio.run(_run())
