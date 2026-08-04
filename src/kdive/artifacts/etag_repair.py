"""Point a committed ``artifacts`` row at the etag its object actually holds (ADR-0519, #1725).

A handler that PUTs outside the advisory lock and registers inside it can find, at registration
time, that a peer attempt already committed a row for its key — and that its own PUT has since
overwritten the object that row describes. The row then names bytes the object no longer holds.

The repair must write an **observed** etag, not the one this attempt wrote. Which PUT landed last
and which attempt reaches its locked phase last are independent orderings: an attempt whose PUT
landed *first* can still be the last to take the lock, and if it wrote its own etag it would
replace a correct row value with a stale one — introducing the very drift the repair exists to
remove. Only the store knows what the object holds, so this asks it.

The guarantee that buys is bounded but real: every value this writes was true of the object when
it was read. It is *not* a guarantee that the row ends up correct. The repair is not atomic, and
concurrent repairs can still land out of order — stat A sees X, a PUT makes it Y, stat B sees Y,
B's update commits, then A's commits and leaves the row at the stale X. A single repair racing a
PUT goes stale the same way. What is excluded is a row being set to an etag no version of the
object ever carried, which is what assuming the caller's own etag would do.

It deliberately runs outside the caller's lock, because a stat is object-store I/O and keeping
that out of a locked span is the whole point of ADR-0519.
"""

from __future__ import annotations

import asyncio
import logging
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection

from kdive.store.objectstore import ObjectStore

_log = logging.getLogger(__name__)

_REFRESH_ETAG_SQL: LiteralString = "UPDATE artifacts SET etag = %s WHERE id = %s"


async def reconcile_row_etag(
    conn: AsyncConnection,
    store: ObjectStore,
    *,
    row_id: UUID,
    object_key: str,
    row_etag: str,
) -> None:
    """Update ``row_id``'s etag to the one ``object_key`` currently carries; never raises.

    A no-op when the row already agrees with the object, when the object is gone, or when the
    stat fails — in each case the row is left exactly as the caller found it, which is no worse
    than not having tried. A failed stat is logged rather than raised: the caller is returning a
    real result (or raising its own guard's error) and a metadata repair must not displace it.

    Call **after** the caller's advisory lock is released.

    Args:
        conn: The handler's dispatch connection. The update opens its own short transaction.
        store: The object store holding ``object_key``.
        row_id: The committed ``artifacts`` row to repair.
        object_key: The object that row describes.
        row_etag: The etag the row currently carries, so an agreeing row costs no write.
    """
    try:
        head = await asyncio.to_thread(store.head, object_key)
        if head is None or head.etag == row_etag:
            return
        async with conn.transaction():
            await conn.execute(_REFRESH_ETAG_SQL, (head.etag, row_id))
    except Exception:  # noqa: BLE001 - advisory repair must not mask the caller outcome
        _log.warning(
            "reconciling etag for %s failed; leaving artifacts row %s describing etag %s",
            object_key,
            row_id,
            row_etag,
            exc_info=True,
        )
        return
    _log.info(
        "artifacts row %s re-pointed at %s's current etag after a concurrent overwrite",
        row_id,
        object_key,
    )
