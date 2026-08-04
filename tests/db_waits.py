"""Database wait-state probes for concurrency tests."""

from __future__ import annotations

import asyncio
import time

import psycopg

from kdive.db.locks import (
    _advisory_lock_oids,
    _session_lock_key,
    session_advisory_lock_held,
)


async def wait_until_backend_waiting(
    observer: psycopg.AsyncConnection,
    waiter_pid: int,
    *,
    locktype: str | None = None,
    timeout_s: float = 5.0,
) -> None:
    """Poll pg_locks until a backend is blocked on a database lock."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await _has_waiting_lock(observer, waiter_pid=waiter_pid, locktype=locktype):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("backend never began waiting on the expected database lock")


async def wait_until_any_backend_waiting(
    observer: psycopg.AsyncConnection,
    *,
    locktype: str | None = None,
    timeout_s: float = 5.0,
) -> None:
    """Poll until a backend is blocked on a lock held by the observer backend."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await _has_waiting_lock(observer, waiter_pid=None, locktype=locktype):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no backend began waiting on the expected lock held by the observer")


async def wait_until_session_lock_released(
    observer: psycopg.AsyncConnection,
    name: str,
    *,
    timeout_s: float = 5.0,
) -> None:
    """Poll pg_locks until no backend holds the named session advisory lock.

    Postgres frees a session advisory lock when the holding *backend* exits, which is a
    distinct moment from the holder's client-side ``close()`` returning: the socket close,
    the backend exit, and ``pg_locks`` reflecting the release are three separate events.
    An observer on another backend must therefore wait for the release rather than assert
    it has already landed.

    On timeout the error names the surviving holders — pid, database, and backend state —
    because a bare "still held" is not enough to tell a genuine reap regression from
    interference by an unrelated backend.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if not await session_advisory_lock_held(observer, name):
            return
        if time.monotonic() >= deadline:
            holders = await _session_lock_holders(observer, name)
            raise AssertionError(
                f"session advisory lock {name!r} still held after {timeout_s}s; "
                f"holders (pid, database, state): {holders}"
            )
        await asyncio.sleep(0.02)


async def _session_lock_holders(
    observer: psycopg.AsyncConnection, name: str
) -> list[tuple[int | None, str | None, str | None]]:
    """Every backend on the cluster holding the named session advisory lock, for diagnostics.

    Deliberately *not* scoped to the observer's database, unlike the probe itself: when the
    wait times out, a holder in another database is one of the explanations worth seeing.
    """
    classid, objid = _advisory_lock_oids(_session_lock_key(name))
    cur = await observer.execute(
        "SELECT l.pid, d.datname, a.state FROM pg_locks l "
        "LEFT JOIN pg_database d ON d.oid = l.database "
        "LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
        "WHERE l.locktype = 'advisory' AND l.classid = %s AND l.objid = %s "
        "  AND l.objsubid = 1 AND l.granted",
        (classid, objid),
    )
    return await cur.fetchall()


async def _has_waiting_lock(
    observer: psycopg.AsyncConnection,
    *,
    waiter_pid: int | None,
    locktype: str | None,
) -> bool:
    if waiter_pid is not None and locktype is not None:
        cur = await observer.execute(
            "SELECT 1 FROM pg_locks WHERE NOT granted AND pid = %s AND locktype = %s LIMIT 1",
            (waiter_pid, locktype),
        )
    elif waiter_pid is not None:
        cur = await observer.execute(
            "SELECT 1 FROM pg_locks WHERE NOT granted AND pid = %s LIMIT 1",
            (waiter_pid,),
        )
    elif locktype is not None:
        cur = await observer.execute(
            "SELECT 1 FROM pg_locks l WHERE NOT l.granted "
            "AND pg_backend_pid() = ANY(pg_blocking_pids(l.pid)) "
            "AND l.locktype = %s LIMIT 1",
            (locktype,),
        )
    else:
        cur = await observer.execute(
            "SELECT 1 FROM pg_locks l WHERE NOT l.granted "
            "AND pg_backend_pid() = ANY(pg_blocking_pids(l.pid)) LIMIT 1"
        )
    return await cur.fetchone() is not None
