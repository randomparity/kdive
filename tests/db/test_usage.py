"""tool_invocation usage writer + CHECK constraint (#506, ADR-0148)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

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
                "SELECT principal, agent_session, project, tool, outcome, actor, client_id "
                "FROM tool_invocation WHERE id = %s",
                (rid,),
            )
            row = await cur.fetchone()
        assert row == ("alice", "s1", "proj-a", "jobs.get", "ok", "agent", None)

    asyncio.run(_run())


def test_record_usage_round_trips_args_digest(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            rid = await record_usage(
                conn,
                UsageEvent(
                    principal="alice",
                    agent_session="s1",
                    project=None,
                    tool="runs.build",
                    outcome="ok",
                    actor="agent",
                    client_id=None,
                    args_digest="deadbeef",
                ),
            )
            await conn.commit()
            cur = await conn.execute(
                "SELECT args_digest FROM tool_invocation WHERE id = %s", (rid,)
            )
            row = await cur.fetchone()
        assert row == ("deadbeef",)

    asyncio.run(_run())


def test_record_usage_defaults_args_digest_to_null(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            rid = await record_usage(
                conn,
                UsageEvent(
                    principal="a",
                    agent_session=None,
                    project=None,
                    tool="t",
                    outcome="ok",
                    actor="agent",
                    client_id=None,
                ),
            )
            await conn.commit()
            cur = await conn.execute(
                "SELECT args_digest FROM tool_invocation WHERE id = %s", (rid,)
            )
            row = await cur.fetchone()
        assert row == (None,)

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
                "SELECT principal, agent_session, project, tool, outcome, actor, client_id "
                "FROM tool_invocation WHERE id = %s",
                (rid,),
            )
            row = await cur.fetchone()
        assert row == ("bob", None, None, "projects.list", "denied", "operator-cli", "cli-1")

    asyncio.run(_run())


def test_record_usage_rejects_bad_outcome(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
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

    asyncio.run(_run())
