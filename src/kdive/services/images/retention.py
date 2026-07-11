"""Shared image retention policy and deletion helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.storage import ObjectListing
from kdive.domain.catalog.images import ImageVisibility
from kdive.images.cataloging.read_model import image_referenced_by_live_system

_log = logging.getLogger(__name__)

_PRIVATE_VISIBILITY = ImageVisibility.PRIVATE.value


@runtime_checkable
class ImageSweepStore(Protocol):
    """The narrow object-store port the image sweeps consume."""

    def list_image_objects(self) -> list[ObjectListing]: ...
    def head_present(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


async def repair_expired_private_images(conn: AsyncConnection, store: ImageSweepStore) -> int:
    """Delete expired private images whose retention guards allow pruning.

    Candidates are ``private`` rows with ``expires_at < now()``. Each row is rechecked under
    :func:`expire_one_private_image`, which holds the row lock while honoring both the
    non-terminal-System reference guard and the concurrent-extend fence.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, object_key, kernel_config_key FROM image_catalog "
            "WHERE visibility = %s AND expires_at IS NOT NULL AND expires_at < now()",
            (_PRIVATE_VISIBILITY,),
        )
        candidates = await cur.fetchall()
    pruned = 0
    for cand in candidates:
        if await expire_one_private_image(
            conn, store, cand["id"], cand["object_key"], cand["kernel_config_key"]
        ):
            pruned += 1
    return pruned


async def expire_one_private_image(
    conn: AsyncConnection,
    store: ImageSweepStore,
    row_id: UUID,
    object_key: str | None,
    config_key: str | None,
) -> bool:
    """Delete one expired private image's objects and row if no retention guard blocks it.

    The locked ``expires_at < now()`` re-read is the extend fence: a concurrent operator extend
    committed after candidate selection turns this into a no-op. The reference guard runs under
    the same transaction so a System that still uses the image defers expiry. Object deletion
    precedes row deletion so a crash leaves at most a dangling row for the reconciler to heal; the
    kernel ``.config`` sibling (``config_key``, ADR-0317) is deleted alongside the qcow2 for prompt
    reclamation (the leaked-sweep is the backstop for the other row-deletion paths).
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT 1 FROM image_catalog "
            "WHERE id = %s AND visibility = %s "
            "  AND expires_at IS NOT NULL AND expires_at < now() FOR UPDATE",
            (row_id, _PRIVATE_VISIBILITY),
        )
        if await cur.fetchone() is None:
            return False
        if await image_referenced_by_live_system(cur, row_id):
            _log.info(
                "images: expired private image %s referenced by a non-terminal System; "
                "deferring expiry",
                row_id,
            )
            return False
        if object_key is not None:
            await asyncio.to_thread(store.delete, object_key)
        if config_key is not None:
            await asyncio.to_thread(store.delete, config_key)
        await cur.execute("DELETE FROM image_catalog WHERE id = %s", (row_id,))
    _log.info("images: expired private image %s pruned (object + row deleted)", row_id)
    return True
