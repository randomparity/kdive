"""tool_invocation usage writer + CHECK constraint (#506, ADR-0148)."""

from __future__ import annotations

import asyncio

import psycopg

from kdive.security.usage import UsageEvent, record_usage


def test_record_usage_writes_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            rid = await record_usage(
                conn,
                UsageEvent(
                    principal="alice",
                    agent_session="s1",
                    project="proj-a",
                    tool="jobs.get",
                    outcome="ok",
                    actor="agent",
                    client_id=None,
                ),
            )
            await conn.commit()
            cur = await conn.execute(
                "SELECT principal, tool, outcome, project, actor FROM tool_invocation "
                "WHERE id = %s",
                (rid,),
            )
            row = await cur.fetchone()
        assert row == ("alice", "jobs.get", "ok", "proj-a", "agent")

    asyncio.run(_run())


def test_record_usage_allows_null_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            rid = await record_usage(
                conn,
                UsageEvent(
                    principal="bob",
                    agent_session=None,
                    project=None,
                    tool="projects.list",
                    outcome="denied",
                    actor="operator-cli",
                    client_id="cli-1",
                ),
            )
            await conn.commit()
            cur = await conn.execute(
                "SELECT project, outcome FROM tool_invocation WHERE id = %s", (rid,)
            )
            row = await cur.fetchone()
        assert row == (None, "denied")

    asyncio.run(_run())


def test_record_usage_rejects_bad_outcome(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            try:
                await record_usage(
                    conn,
                    UsageEvent(
                        principal="a",
                        agent_session=None,
                        project=None,
                        tool="t",
                        outcome="bogus",
                        actor="agent",
                        client_id=None,
                    ),
                )
                await conn.commit()
                raise AssertionError("expected a CHECK violation on the outcome enum")
            except psycopg.errors.CheckViolation:
                pass

    asyncio.run(_run())
