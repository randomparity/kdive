"""Postgres advisory locks (ADR-0005, ADR-0016, ADR-0095).

`advisory_xact_lock` serializes per-Allocation / per-System operations using the
single-bigint `pg_advisory_xact_lock` — a lock space disjoint from the migration
runner's two-int lock (ADR-0015), so application and migration locks never contend.
The lock releases when the surrounding transaction ends; the helper fails fast when
no transaction is open to hold it.

`SessionAdvisoryLock` is the **session-scoped** leadership claim the reconciler's
console-collector hosting loop needs (ADR-0095): a `pg_advisory_lock` held on a
dedicated long-lived connection that survives transaction boundaries and is released
**by Postgres when the holding backend exits** — the property the single-leader
split-brain guard relies on. That release trails the holder's client-side connection
close by a short interval rather than landing with it, so an observer can still see a
dead leader's lock briefly. Its key space is salted apart from the transaction-scoped
scope-lock space so leadership never collides with a per-object op.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus


class LockScope(StrEnum):
    """The advisory-lock scopes the platform serializes on (ADR-0016, ADR-0040).

    Operations that hold more than one scope at once acquire them in the fixed global
    total order ``PROJECT → RESOURCE → ALLOCATION → SYSTEM → INVESTIGATION → RUN`` to avoid
    deadlock; e.g. ``allocations.request`` takes ``PROJECT`` then ``RESOURCE`` (ADR-0040 §1)
    and ``runs.create`` takes ``SYSTEM`` then ``INVESTIGATION`` (ADR-0027).

    ``PROJECT`` is keyed by the ``project`` string; every other scope is keyed by an object
    :class:`~uuid.UUID` — including ``RESOURCE`` when it locks a resource object (by
    ``resource.id``, co-held in the ``PROJECT → RESOURCE`` order above). The one exception is the
    reconcile / ``inventory.clear_override`` per-identity lock: a ``RESOURCE``-scope lock keyed by a
    ``"{kind}:{name}"`` string and always held alone, outside the co-hold total order.
    """

    PROJECT = "project"
    ALLOCATION = "allocation"
    SYSTEM = "system"
    RESOURCE = "resource"
    INVESTIGATION = "investigation"
    RUN = "run"
    INVENTORY = "inventory"


def _lock_key(scope: LockScope, key: UUID | str) -> int:
    """Derive a deterministic signed 64-bit advisory-lock key from ``(scope, key)``.

    The digest folds an unbounded key space onto 64 bits: a collision over-serializes
    two unrelated keys (safe — never under-serializes). A ``0x00`` separator keeps the
    scope and key boundaries unambiguous for the NUL-free identifiers used here
    (object UUIDs and ``project`` strings).
    """
    digest = hashlib.blake2b(digest_size=8)
    digest.update(scope.value.encode())
    digest.update(b"\x00")
    digest.update(str(key).encode())
    return int.from_bytes(digest.digest(), "big", signed=True)


@asynccontextmanager
async def advisory_xact_lock(
    conn: AsyncConnection, scope: LockScope, key: UUID | str
) -> AsyncIterator[None]:
    """Hold a transaction-scoped advisory lock for ``(scope, key)`` over the block.

    Blocks until any current holder's transaction ends, then yields. The lock is
    released by the caller's transaction commit/rollback, not on block exit.

    Args:
        conn: An async connection with an open (or about-to-open) transaction.
        scope: The lock scope.
        key: The object id (a :class:`~uuid.UUID`) the lock protects, or the
            ``project`` string for :attr:`LockScope.PROJECT`.

    Raises:
        RuntimeError: After acquiring, the connection is not in a transaction, so the
            lock already auto-released (e.g. an autocommit connection used without
            ``conn.transaction()``).
    """
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(scope, key),))
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError(
            "advisory_xact_lock must run inside an open transaction; the lock "
            "auto-released because no transaction is in progress (ADR-0005). Wrap the "
            "call in `async with conn.transaction()` or use a non-autocommit connection."
        )
    yield


def require_top_level_transaction(conn: AsyncConnection, operation: str) -> None:
    """Raise unless ``conn`` is transaction-free, so the next ``transaction()`` really commits.

    ``conn.transaction()`` opens a real transaction only on an **idle** connection; on one that is
    already in a transaction it opens a ``SAVEPOINT`` instead, and releasing a savepoint commits
    nothing and releases no ``pg_advisory_xact_lock``. A non-autocommit connection enters that state
    invisibly — one bare ``execute`` outside any ``transaction()`` block starts a transaction that
    lives until the pool returns the connection.

    So a block that means "commit this, and hold a lock only while I do" silently becomes "defer
    this to my caller's commit, and hold the lock until then" if anything ran a statement first.
    For a mint that has to be visible to another process *before* a multi-GiB write, and for a lock
    ADR-0244 forbids holding across that write, those are opposite behaviours with no observable
    difference at the call site — which is why this is checked rather than documented.

    Args:
        conn: The connection about to open a transaction.
        operation: What the caller is doing, for the message.

    Raises:
        RuntimeError: ``conn`` is already in a transaction (or in a failed one).
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError(
            f"{operation} needs a transaction-free connection: this one is already in a "
            "transaction, so `conn.transaction()` would open a savepoint, commit nothing on "
            "release, and hold any advisory xact lock until the caller's own commit. Commit or "
            "close the enclosing transaction first, or move this call ahead of whatever opened it."
        )


async def try_advisory_xact_lock(conn: AsyncConnection, scope: LockScope, key: UUID | str) -> bool:
    """Take the transaction-scoped lock for ``(scope, key)`` if it is free, else report ``False``.

    The non-blocking sibling of :func:`advisory_xact_lock`, for a caller whose correct response to
    contention is to do nothing and try again later rather than to wait. The reconciler's upload
    orphan sweep is that caller (ADR-0502): a held owner lock means a writer is active on that
    owner, so the sweep skips the key and the next pass re-derives it — where blocking would stall a
    reconciler pass that has no deadline behind whatever the holder is doing.

    Not a context manager, because there is nothing to release on exit: a granted lock is held to
    the surrounding transaction's end, exactly like the blocking helper, and a refused one was never
    held. Call it inside ``async with conn.transaction()`` and branch on the result.

    Args:
        conn: An async connection with an open transaction.
        scope: The lock scope.
        key: The object id (a :class:`~uuid.UUID`) the lock protects, or the ``project`` string
            for :attr:`LockScope.PROJECT`.

    Returns:
        Whether this transaction now holds the lock.

    Raises:
        RuntimeError: After the attempt, the connection is not in a transaction — so a granted
            lock has already auto-released and a ``True`` would be a lie. Checked after rather
            than before for the same reason the blocking helper does: the transaction is only
            observably open once a statement has run in it.
    """
    cur = await conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (_lock_key(scope, key),))
    row = await cur.fetchone()
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError(
            "try_advisory_xact_lock must run inside an open transaction; any lock it took has "
            "already auto-released because no transaction is in progress (ADR-0005). Wrap the "
            "call in `async with conn.transaction()` or use a non-autocommit connection."
        )
    return bool(row is not None and row[0])


# The leadership name for the single reconciler that hosts console collectors (ADR-0095).
CONSOLE_HOSTING_LEADER = "console-hosting-leader"

# The single global inventory-reconcile pass name (ADR-0112). A whole reconcile pass spans
# multiple transactions (the batched upsert + per-row prunes), so it serializes on a
# session-scoped lock (released by Postgres if the holding connection drops), not an xact lock.
INVENTORY_RECONCILE = "inventory-reconcile"

# Salt that keeps the session-lock key space disjoint from the transaction-scoped
# scope-lock space: both fold onto the single-bigint advisory space, but a leadership
# claim must never serialize against a per-object op that happened to hash equal.
_SESSION_LOCK_SALT = b"session-advisory-lock\x00"


def _session_lock_key(name: str) -> int:
    """Derive a deterministic signed 64-bit session advisory-lock key from ``name``.

    Salted apart from :func:`_lock_key` so a session leadership claim and a
    transaction-scoped scope lock can never collide in the shared advisory space.
    """
    digest = hashlib.blake2b(digest_size=8)
    digest.update(_SESSION_LOCK_SALT)
    digest.update(name.encode())
    return int.from_bytes(digest.digest(), "big", signed=True)


def _advisory_lock_oids(key: int) -> tuple[int, int]:
    """Split a single-bigint advisory key into the ``(classid, objid)`` ``pg_locks`` exposes.

    Postgres splits a single-bigint advisory lock into two oid columns: ``classid`` is the high
    32 bits and ``objid`` the low 32 bits, both unsigned. Deriving them here avoids signed-int4
    bit-math in SQL for keys whose high bit is set (blake2b can produce a negative int8).
    """
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    return (unsigned >> 32) & 0xFFFF_FFFF, unsigned & 0xFFFF_FFFF


async def session_advisory_lock_held(conn: AsyncConnection, name: str) -> bool:
    """Report whether **any** backend currently holds the named session advisory lock.

    Unlike :meth:`SessionAdvisoryLock.is_held` — which scopes to this connection's own backend
    pid — this scans ``pg_locks`` across all backends, so a worker can tell a live leader from a
    dead one without contending for leadership itself.

    The scan is scoped to ``conn``'s **own database** (#1532; ADR-0429 introduced this probe
    as cross-*backend* and did not consider the cluster). Advisory locks are per-database: two
    backends in different databases of one cluster take the same key without contending, so a
    holder elsewhere on the cluster is not a leader of *this* deployment and must not read as
    one. ``pg_locks`` is cluster-wide and exposes every database's rows, so the filter is
    required — without it a second kdive deployment on the same cluster reports as a live
    leader here.

    ``False`` therefore means no live holder in this database as of the probe. It is not an
    instantaneous read of the holder's liveness: Postgres frees the lock when the holding
    *backend* exits, which trails the holder's client-side connection close, so a dead leader
    can still read as held for a while afterwards. That lag has no guaranteed upper bound —
    do not size a timeout off an observed value. Only granted locks are counted: a would-be
    acquirer blocked on ``pg_advisory_lock`` leaves an ungranted row that must not read as a
    live holder.
    """
    classid, objid = _advisory_lock_oids(_session_lock_key(name))
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM pg_locks "
            "WHERE locktype = 'advisory' AND classid = %s AND objid = %s "
            "  AND objsubid = 1 AND granted "
            "  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
            (classid, objid),
        )
        row = await cur.fetchone()
    return bool(row[0]) if row is not None else False


@asynccontextmanager
async def session_advisory_lock(conn: AsyncConnection, name: str) -> AsyncIterator[None]:
    """Hold a **session-scoped** advisory lock named ``name`` for the whole ``with`` block.

    Unlike :func:`advisory_xact_lock`, this lock survives transaction boundaries: it is
    acquired with ``pg_advisory_lock`` and released with ``pg_advisory_unlock`` in a
    ``finally``, so a multi-transaction operation (e.g. an inventory reconcile pass whose
    upsert and per-row prunes are separate transactions) serializes for its entire duration.
    The key space is salted apart from the transaction-scoped scope-lock space, so it never
    collides with a per-object op.

    The lock blocks until any current holder releases, then yields. The connection must not
    be in autocommit-with-implicit-transaction surprise: ``pg_advisory_lock`` taken outside a
    transaction is held at session scope, which is exactly the intent here.

    Args:
        conn: The connection that runs the whole guarded pass.
        name: The lock name (e.g. :data:`INVENTORY_RECONCILE`).
    """
    key = _session_lock_key(name)
    await conn.execute("SELECT pg_advisory_lock(%s)", (key,))
    try:
        yield
    finally:
        await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


class SessionAdvisoryLock:
    """A session-scoped ``pg_advisory_lock`` leadership claim on one dedicated connection.

    Unlike :func:`advisory_xact_lock`, this lock is **not** released at transaction end —
    it lives until :meth:`release` is called or the holding connection drops (which
    Postgres detects, releasing the lock with no notice to the dead holder). That is
    precisely the leadership semantics the console-hosting loop needs (ADR-0095), and the
    split-brain hazard the hosting loop's lock-loss guard handles: a standby can acquire
    the lock as soon as the old leader's backend exits, which trails the connection dying
    by a short interval rather than landing with it.

    The connection must be dedicated to leadership (outside the repair pool, ADR-0095):
    holding a pooled connection for the process life would pin the pool's only connection
    and starve the reconciler's repairs.
    """

    def __init__(self, conn: AsyncConnection, name: str) -> None:
        self._conn = conn
        self._key = _session_lock_key(name)
        self._classid, self._objid = _advisory_lock_oids(self._key)

    async def try_acquire(self) -> bool:
        """Try to claim leadership without blocking; ``True`` iff this call won it.

        ``pg_try_advisory_lock`` never waits — a non-leader replica gets ``False``
        immediately and hosts nothing, rather than blocking behind the live leader.
        """
        async with self._conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (self._key,))
            row = await cur.fetchone()
        return bool(row[0]) if row is not None else False

    async def release(self) -> None:
        """Release the leadership lock if this connection holds it (idempotent)."""
        await self._conn.execute("SELECT pg_advisory_unlock(%s)", (self._key,))

    async def is_held(self) -> bool:
        """Report whether **this** connection currently holds the leadership lock.

        Reads ``pg_locks`` for this connection's backend pid, so it observes a release
        caused by a dropped connection that the dead holder could never report itself.
        """
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                "  AND classid = %s AND objid = %s AND objsubid = 1",
                (self._classid, self._objid),
            )
            row = await cur.fetchone()
        return bool(row[0]) if row is not None else False
