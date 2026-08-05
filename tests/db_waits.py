"""Database wait-state probes for concurrency tests."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future
from typing import Any

import psycopg

from kdive.db.locks import (
    _advisory_lock_oids,
    _session_lock_key,
    session_advisory_lock_held,
)

DEFAULT_WAIT_TIMEOUT_S = 5.0
"""How long every probe here waits for a backend to reach the expected wait state.

Nothing bounds how long a Postgres backend takes to *register* a lock wait in `pg_locks`.
The lock ordering under test is deterministic; only the moment the wait becomes observable
is not, and under a saturated parallel suite against a shared container a multi-second
scheduling stall is permitted. So the budget is set once, generously, here — a caller that
picks its own tighter literal turns that latency into a spurious failure.
"""

_POLL_INTERVAL_S = 0.02


async def wait_until_backend_waiting(
    observer: psycopg.AsyncConnection,
    waiter_pid: int,
    *,
    locktype: str | None = None,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
) -> None:
    """Poll pg_locks until a backend is blocked on a database lock."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await _has_waiting_lock(observer, waiter_pid=waiter_pid, locktype=locktype):
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise AssertionError("backend never began waiting on the expected database lock")


async def wait_until_any_backend_waiting(
    observer: psycopg.AsyncConnection,
    *,
    locktype: str | None = None,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
) -> None:
    """Poll until a backend is blocked on a lock held by the observer backend."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await _has_waiting_lock(observer, waiter_pid=None, locktype=locktype):
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise AssertionError("no backend began waiting on the expected lock held by the observer")


def wait_until_blocked_by(
    observer: psycopg.Connection,
    *,
    waiter_pid: int,
    blocker_pid: int,
    future: Future[Any],
    expectation: str,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
) -> None:
    """Poll until `blocker_pid` appears in `waiter_pid`'s blocking set; the sync analogue.

    `future` is the in-flight call that is supposed to block. It is not merely decoration:
    a call that *raised* — a refused connection, a role error — never reaches the lock at
    all, and polling for a wait edge it will never publish just burns the whole budget and
    then reports "did not block", discarding the real cause. So a finished future ends the
    wait immediately and its exception (or its return value) becomes the reported reason.

    On a genuine timeout the error names the waiter's pid and its `pg_stat_activity` and
    `pg_locks` rows, because a bare "did not block" cannot tell a real ordering regression
    from a backend that was simply still being scheduled.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if _is_blocked_by(observer, waiter_pid=waiter_pid, blocker_pid=blocker_pid):
            return
        if future.done():
            error = future.exception()
            if error is not None:
                raise AssertionError(
                    f"{expectation}; the call raised before it could block: {error!r}"
                ) from error
            raise AssertionError(
                f"{expectation}; the call returned {future.result()!r} without ever blocking"
            )
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{expectation}; waiter pid {waiter_pid} was not blocked by pid {blocker_pid} "
                f"after {timeout_s}s; pg_stat_activity (state, wait_event_type, wait_event, "
                f"query): {_waiter_activity(observer, waiter_pid)}; pg_locks (locktype, mode, "
                f"granted, blocking pids): {_waiter_locks(observer, waiter_pid)}"
            )
        time.sleep(_POLL_INTERVAL_S)


async def wait_until_session_lock_released(
    observer: psycopg.AsyncConnection,
    name: str,
    *,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
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
        await asyncio.sleep(_POLL_INTERVAL_S)


def _is_blocked_by(observer: psycopg.Connection, *, waiter_pid: int, blocker_pid: int) -> bool:
    return observer.execute(
        "SELECT %s = ANY(pg_blocking_pids(%s))", (blocker_pid, waiter_pid)
    ).fetchone() == (True,)


def _waiter_activity(
    observer: psycopg.Connection, waiter_pid: int
) -> tuple[str | None, str | None, str | None, str | None] | None:
    """The waiter's backend state, for a timeout report."""
    return observer.execute(
        "SELECT state, wait_event_type, wait_event, query FROM pg_stat_activity WHERE pid = %s",
        (waiter_pid,),
    ).fetchone()


def _waiter_locks(
    observer: psycopg.Connection, waiter_pid: int
) -> list[tuple[str | None, str | None, bool, list[int]]]:
    """Every lock the waiter holds or wants, ungranted first, with who blocks each."""
    return observer.execute(
        "SELECT l.locktype, l.mode, l.granted, pg_blocking_pids(l.pid) FROM pg_locks l "
        "WHERE l.pid = %s ORDER BY l.granted, l.locktype",
        (waiter_pid,),
    ).fetchall()


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
