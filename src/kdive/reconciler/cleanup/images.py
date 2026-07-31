"""Image-catalog object drift repair for the reconciler (M2.4/6, ADR-0092, ADR-0093).

Two deadline-guarded sweeps, modeled on
:func:`kdive.reconciler.cleanup.uploads.repair_abandoned_uploads` (a ``deadline < now()``
window + a table cross-check, never an eager delete), each isolated on a fresh pooled
connection and each evaluating time in Postgres ``now()`` (never a Python clock):

* :func:`repair_leaked_images` — an object under the image prefix referenced by **no catalog row**
  (via either ``object_key`` or ``kernel_config_key``, so a live image's ``.config`` sibling is
  protected, ADR-0317), older than the publish grace (keyed off the object's store mtime): delete
  the object. A ``pending`` row owns its objects (both keys are written before their objects in the
  row-first publish), so a live publish is never raced; the mtime grace is the second fence against
  a just-written object whose row commit is in flight.
* :func:`repair_dangling_images` — after a non-``defined`` row's publish deadline, preserve a
  registered row whose object exists, remove one whose object is missing, and either register or
  reclaim an abandoned pending publication from its size/checksum evidence. Object-less
  ``defined`` baselines are object-less by design and skipped.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.cursor_async import AsyncCursor
from psycopg.rows import DictRow, dict_row

from kdive.artifacts.storage import HeadResult, ObjectListing
from kdive.db.locks import LockScope, try_advisory_xact_lock
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.errors import CategorizedError
from kdive.services.images.audit import record_private_registration
from kdive.services.images.publish import digest_sha256_b64
from kdive.services.images.retention import ImageSweepStore

_log = logging.getLogger(__name__)

_DEFINED_STATE = ImageState.DEFINED.value


# The image sweeps consume the shared object-listing value type (key + store mtime); the
# alias keeps the reconciler tests' import surface stable while reusing one definition.
ImageMtime = ObjectListing


async def repair_leaked_images(
    conn: AsyncConnection, store: ImageSweepStore, grace: timedelta
) -> int:
    """Delete image-prefix objects with no catalog row, older than the publish grace.

    A live publish writes the catalog row **before** the object (ADR-0092 §3), so a rowless
    object is an orphan — a manual upload, or a build that wrote bytes before any row. The
    object's store mtime is compared against ``now() - grace`` **in Postgres**, so a freshly
    written object (a publish whose row commit is still in flight) is protected by the grace
    window and never raced. Each candidate's row-absence is re-checked immediately before the
    delete so a row that landed between the listing and the delete protects its object.

    Returns the number of objects deleted; one structured-log line per delete.
    """
    objects = await asyncio.to_thread(store.list_image_objects)
    deleted = 0
    for obj in objects:
        try:
            if await _delete_if_leaked(conn, store, obj, grace):
                deleted += 1
        except CategorizedError as exc:
            _log.warning("reconciler: leaked image object %s cleanup failed: %s", obj.key, exc)
    return deleted


async def _delete_if_leaked(
    conn: AsyncConnection, store: ImageSweepStore, obj: ImageMtime, grace: timedelta
) -> bool:
    """Delete ``obj`` iff no catalog row references it and it is older than the grace window."""
    head = await asyncio.to_thread(store.head, obj.key)
    if head is None or head.last_modified != obj.last_modified:
        return False
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS (SELECT 1 FROM image_catalog "
            "WHERE object_key = %s OR kernel_config_key = %s) OR %s >= now() - %s",
            (obj.key, obj.key, obj.last_modified, grace),
        )
        row = await cur.fetchone()
    protected = bool(row[0]) if row is not None else True
    if protected:
        return False
    await asyncio.to_thread(store.delete_version, obj.key, head.version_id)
    _log.info("reconciler: leaked image object %s deleted (no row, past grace)", obj.key)
    return True


async def repair_dangling_images(
    conn: AsyncConnection, store: ImageSweepStore, grace: timedelta
) -> int:
    """Resolve abandoned publications and remove missing registered rows past their deadline.

    Each candidate gets one transaction holding the exact image-publication xact fence and row
    lock through object HEAD/delete, terminal row mutation, private audit, and commit. A contended
    fence or changed state/deadline/attempt is retried on a later pass. Matching pending bytes are
    registered; absent or invalid bytes are reclaimed only after confirmed absence. Registered
    rows retain the prior present/keep, missing/remove behavior.

    Returns the number of terminal catalog outcomes: rows registered or removed.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, state, publication_attempt_id FROM image_catalog "
            "WHERE object_key IS NOT NULL AND state <> %s AND pending_since < now() - %s "
            "AND (state <> %s OR publication_attempt_id IS NOT NULL)",
            (_DEFINED_STATE, grace, ImageState.PENDING.value),
        )
        candidates = await cur.fetchall()
    terminal = 0
    for cand in candidates:
        if await _repair_image_candidate(conn, store, cand, grace):
            terminal += 1
    return terminal


async def _repair_image_candidate(
    conn: AsyncConnection,
    store: ImageSweepStore,
    candidate: DictRow,
    grace: timedelta,
) -> bool:
    """Reach one terminal outcome while holding its transaction and publication fences."""
    row_id = candidate["id"]
    if not isinstance(row_id, UUID):
        raise RuntimeError("image repair candidate id is not a UUID")
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        if not await try_advisory_xact_lock(conn, LockScope.IMAGE_PUBLISH, row_id):
            return False
        await cur.execute(
            "SELECT * FROM image_catalog WHERE id = %s AND state = %s "
            "AND publication_attempt_id IS NOT DISTINCT FROM %s "
            "AND object_key IS NOT NULL AND state <> %s "
            "AND pending_since < now() - %s FOR UPDATE",
            (
                row_id,
                candidate["state"],
                candidate["publication_attempt_id"],
                _DEFINED_STATE,
                grace,
            ),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        entry = ImageCatalogEntry.model_validate(row)
        if entry.object_key is None:
            raise RuntimeError(f"locked image repair candidate {entry.id} has no object key")
        object_key = entry.object_key
        head = await asyncio.to_thread(store.head, object_key)
        if entry.state is ImageState.REGISTERED:
            return await _repair_registered_candidate(cur, entry, object_present=head is not None)
        if entry.state is not ImageState.PENDING:
            return False
        return await _repair_pending_candidate(cur, store, entry, object_key, head)


async def _repair_registered_candidate(
    cur: AsyncCursor[DictRow], entry: ImageCatalogEntry, *, object_present: bool
) -> bool:
    """Preserve registered/present and remove registered/missing under the caller's lock."""
    if object_present:
        return False
    await cur.execute("DELETE FROM image_catalog WHERE id = %s", (entry.id,))
    _log.info("reconciler: registered image row %s removed (object missing)", entry.id)
    return True


async def _repair_pending_candidate(
    cur: AsyncCursor[DictRow],
    store: ImageSweepStore,
    entry: ImageCatalogEntry,
    object_key: str,
    head: HeadResult | None,
) -> bool:
    """Apply the abandoned-pending decision table under the caller's locks."""
    if head is None:
        await _disarm_and_delete_pending(cur, entry.id)
        _log.info("reconciler: pending image row %s removed (object missing)", entry.id)
        return True

    expected_checksum = _persisted_checksum(entry.digest)
    missing_private_actor = (
        entry.visibility is ImageVisibility.PRIVATE and entry.publication_principal is None
    )
    valid = (
        expected_checksum is not None
        and head.size_bytes == entry.size_bytes
        and head.checksum_sha256 == expected_checksum
        and not missing_private_actor
    )
    if not valid:
        await asyncio.to_thread(store.delete_version, object_key, head.version_id)
        if await asyncio.to_thread(store.head, object_key) is not None:
            return False
        await _disarm_and_delete_pending(cur, entry.id)
        _log.info("reconciler: pending image row %s reclaimed (object invalid)", entry.id)
        return True

    clear_config = False
    if entry.kernel_config_key is not None:
        clear_config = await asyncio.to_thread(store.head, entry.kernel_config_key) is None
    set_config = ", kernel_config_key = NULL" if clear_config else ""
    await cur.execute(
        "UPDATE image_catalog SET state = %s, publication_attempt_id = NULL, "
        f"publication_principal = NULL{set_config} WHERE id = %s RETURNING *",
        (ImageState.REGISTERED.value, entry.id),
    )
    registered = await cur.fetchone()
    if registered is None:
        raise RuntimeError(f"locked image publication {entry.id} disappeared before registration")
    recovered = ImageCatalogEntry.model_validate(registered)
    if recovered.visibility is ImageVisibility.PRIVATE:
        principal = entry.publication_principal
        if principal is None:
            raise RuntimeError("validated private recovery unexpectedly has no principal")
        await record_private_registration(cur.connection, recovered, principal)
    _log.info("reconciler: pending image row %s recovered to registered", entry.id)
    return True


async def _disarm_and_delete_pending(cur: AsyncCursor[DictRow], row_id: UUID) -> None:
    """Clear attempt state under the fence, then pass the expand-phase delete trigger."""
    await cur.execute(
        "UPDATE image_catalog SET publication_attempt_id = NULL, publication_principal = NULL "
        "WHERE id = %s AND state = %s",
        (row_id, ImageState.PENDING.value),
    )
    await cur.execute("DELETE FROM image_catalog WHERE id = %s", (row_id,))


def _persisted_checksum(digest: str | None) -> str | None:
    """Return a canonical checksum, or ``None`` for an unverifiable persisted digest."""
    if digest is None:
        return None
    try:
        return digest_sha256_b64(digest)
    except CategorizedError:
        return None
