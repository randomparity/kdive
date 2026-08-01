"""Database-session fence for active image publication attempts (ADR-0525)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from psycopg import AsyncConnection

from kdive.db.locks import (
    LockScope,
    require_top_level_transaction,
    scoped_session_advisory_lock,
)
from kdive.domain.catalog.images import ImageState
from kdive.domain.errors import CategorizedError, ErrorCategory

if TYPE_CHECKING:
    from kdive.services.images.publish import PublishReservation


async def assert_reservation_owner(conn: AsyncConnection, reservation: PublishReservation) -> None:
    """Raise ``CONFLICT`` unless the complete committed reservation still owns its row."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM image_catalog "
            "WHERE id = %s AND publication_attempt_id = %s AND state = %s "
            "AND object_key = %s AND digest = %s AND size_bytes = %s",
            (
                reservation.row_id,
                reservation.publication_attempt_id,
                ImageState.PENDING.value,
                reservation.object_key,
                reservation.request.digest,
                reservation.size_bytes,
            ),
        )
        owned = await cur.fetchone()
    if owned is None:
        raise CategorizedError(
            "this publish's reservation no longer owns its catalog row; a concurrent publish of "
            "the same image identity superseded it, or the reconciler reclaimed it past the "
            "publish deadline",
            category=ErrorCategory.CONFLICT,
            details={"row_id": str(reservation.row_id), "object_key": reservation.object_key},
        )


@asynccontextmanager
async def publication_fence(
    conn: AsyncConnection, reservation: PublishReservation
) -> AsyncIterator[None]:
    """Fence one active attempt across object write and committed catalog registration.

    The short reservation revalidation commits before yielding, leaving the connection
    transaction-idle throughout the potentially long object-store operation. A pooled
    non-autocommit connection is temporarily switched to autocommit and restored on every exit.
    """
    require_top_level_transaction(conn, "image publication fence")
    previous_autocommit = conn.autocommit
    if not previous_autocommit:
        await conn.set_autocommit(True)
    try:
        async with scoped_session_advisory_lock(conn, LockScope.IMAGE_PUBLISH, reservation.row_id):
            async with conn.transaction():
                await assert_reservation_owner(conn, reservation)
            require_top_level_transaction(conn, "image publication object write")
            yield
    finally:
        if not previous_autocommit:
            await conn.set_autocommit(False)
