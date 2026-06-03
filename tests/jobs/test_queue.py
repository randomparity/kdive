"""Tests for the connection-scoped queue operations (ADR-0018)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _count_jobs(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute("SELECT count(*) FROM jobs")
    row = await cur.fetchone()
    assert row is not None  # COUNT(*) always returns one row
    return row[0]


def test_enqueue_inserts_queued_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            job = await queue.enqueue(conn, JobKind.BUILD, {"x": 1}, {"principal": "alice"}, "dk-1")
            assert isinstance(job, Job)
            assert job.state is JobState.QUEUED
            assert job.attempt == 0
            assert job.payload == {"x": 1}
            assert job.authorizing == {"principal": "alice"}
            assert job.dedup_key == "dk-1"
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_same_dedup_key_returns_same_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-dup")
            second = await queue.enqueue(conn, JobKind.PROVISION, {"y": 2}, {"p": "b"}, "dk-dup")
            assert second.id == first.id
            assert second.kind is JobKind.BUILD  # the existing row, unchanged
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_distinct_dedup_keys_make_distinct_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            a = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-a")
            b = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-b")
            assert a.id != b.id
            assert await _count_jobs(conn) == 2

    asyncio.run(_run())


def test_enqueue_rejects_max_attempts_below_one(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ValueError, match="max_attempts"):
                await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-0", max_attempts=0)

    asyncio.run(_run())
