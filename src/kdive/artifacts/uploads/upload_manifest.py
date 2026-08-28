"""Owner-scoped, replaceable upload manifests with an object-store prefix and deadline."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.artifacts.uploads.uploads import ChunkEntry, ManifestEntry
from kdive.db.locks import LockScope

UploadOwnerKind = Literal["runs", "investigations"]
RUN_UPLOAD_OWNER: UploadOwnerKind = "runs"
INVESTIGATION_UPLOAD_OWNER: UploadOwnerKind = "investigations"

#: Tenant shared by upload-window minting and orphan sweeping (ADR-0455).
UPLOAD_TENANT = "local"

# Mint, reaping, and orphan deletion must use the same owner lock (ADR-0502).
_LOCK_SCOPES: dict[UploadOwnerKind, LockScope] = {
    RUN_UPLOAD_OWNER: LockScope.RUN,
    INVESTIGATION_UPLOAD_OWNER: LockScope.INVESTIGATION,
}

#: Upload owner kinds, shared with the reaper's object-store roots (ADR-0455).
UPLOAD_OWNER_KINDS: tuple[UploadOwnerKind, ...] = tuple(_LOCK_SCOPES)

#: Finalize reason when the shared upload-window predicate finds an expired deadline.
UPLOAD_WINDOW_EXPIRED = "upload_window_expired"


class UploadManifest(NamedTuple):
    """A persisted manifest: the declared entries, the key prefix, and the deadline."""

    entries: tuple[ManifestEntry, ...]
    prefix: str
    deadline: datetime


class ManifestStamp(NamedTuple):
    """Postgres reference clock and timezone-aware deadline from one manifest operation."""

    server_time: datetime
    deadline: datetime

    @property
    def expired(self) -> bool:
        """Return whether ``deadline < server_time``; equality remains open (ADR-0512)."""
        return self.deadline < self.server_time


class WindowRefresh(NamedTuple):
    """Post-refresh deadline and whether either monotonic clamp withheld a full ``ttl`` grant."""

    deadline: datetime
    capped: bool


@dataclass(frozen=True)
class UploadManifestReplaceRequest:
    """A full replacement for one owner's upload manifest."""

    owner_kind: UploadOwnerKind
    owner_id: UUID
    prefix: str
    entries: Sequence[ManifestEntry]
    ttl: timedelta


def _entry_payload(entry: ManifestEntry) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": entry.name,
        "sha256": entry.sha256,
        "size_bytes": entry.size_bytes,
    }
    if entry.chunks is not None:
        payload["chunks"] = [{"sha256": c.sha256, "size_bytes": c.size_bytes} for c in entry.chunks]
    if entry.encoding is not None:
        # Absent ⇒ identity, so only a non-identity encoding is persisted; a pre-existing manifest
        # without these keys deserializes as identity (ADR-0437). ``uncompressed_size`` is always
        # present alongside a non-identity ``encoding`` (the validator requires it).
        payload["encoding"] = entry.encoding
        payload["uncompressed_size"] = entry.uncompressed_size
    return payload


async def replace_manifest(
    conn: AsyncConnection,
    request: UploadManifestReplaceRequest,
) -> ManifestStamp:
    """Replace one manifest and restart its capped window at Postgres ``now() + ttl``."""
    payload = [_entry_payload(e) for e in request.entries]
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO upload_manifests "
            "  (owner_kind, owner_id, prefix, manifest, deadline, window_started_at) "
            "VALUES (%s, %s, %s, %s, now() + %s, now()) "
            "ON CONFLICT (owner_kind, owner_id) DO UPDATE SET "
            "  prefix = EXCLUDED.prefix, manifest = EXCLUDED.manifest, "
            "  deadline = EXCLUDED.deadline, window_started_at = EXCLUDED.window_started_at "
            "RETURNING now(), deadline",
            (request.owner_kind, request.owner_id, request.prefix, Jsonb(payload), request.ttl),
        )
        row = await cur.fetchone()
    if row is None:  # a RETURNING upsert always yields one row; fail loud if it ever does not
        raise RuntimeError(
            f"replace_manifest RETURNING yielded no row for {request.owner_kind} {request.owner_id}"
        )
    return ManifestStamp(server_time=row[0], deadline=row[1])


async def refresh_deadline(
    conn: AsyncConnection,
    owner_kind: UploadOwnerKind,
    owner_id: UUID,
    ttl: timedelta,
    *,
    max_window: timedelta,
) -> WindowRefresh | None:
    """Refresh an open, owner-locked window with Postgres's transaction clock (ADR-0511).

    The monotonic clamp is ``deadline := GREATEST(deadline, LEAST(now() + ttl,
    window_started_at + max_window))``. ``LEAST`` enforces the cap and ``GREATEST`` preserves an
    open deadline; a spent cap returns that unchanged deadline with ``capped=True``, never
    ``None``. A lowered TTL can leave an already-later deadline to expire naturally.

    The caller must already hold the owner lock and have established the window is open in this
    transaction. ``None`` means no matching open row; under that precondition it means reaping,
    while other callers must re-read to distinguish absence from expiry. Only
    :func:`replace_manifest` restarts the capped window.
    """
    # Exact ``<>`` detects both clamps: ``LEAST`` shortens a grant and ``GREATEST`` preserves one.
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE upload_manifests SET deadline = "
            "  GREATEST(deadline, LEAST(now() + %(ttl)s, window_started_at + %(max_window)s)) "
            "WHERE owner_kind = %(owner_kind)s AND owner_id = %(owner_id)s AND deadline >= now() "
            "RETURNING deadline, deadline <> now() + %(ttl)s",
            {
                "ttl": ttl,
                "max_window": max_window,
                "owner_kind": owner_kind,
                "owner_id": owner_id,
            },
        )
        row = await cur.fetchone()
    return None if row is None else WindowRefresh(deadline=row[0], capped=row[1])


async def deadline_stamp(conn: AsyncConnection, manifest: UploadManifest) -> ManifestStamp:
    """Pair a fetched deadline with the Postgres transaction reference clock."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT now()")
        row = await cur.fetchone()
    if row is None:  # a bare SELECT always yields one row; fail loud if it ever does not
        raise RuntimeError("deadline_stamp could not read the database reference clock")
    return ManifestStamp(server_time=row[0], deadline=manifest.deadline)


async def window_deadline(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID
) -> datetime | None:
    """Return an owner's manifest deadline without deserializing its entries."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT deadline FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
            (owner_kind, owner_id),
        )
        row = await cur.fetchone()
    return None if row is None else row[0]


async def get_manifest(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID
) -> UploadManifest | None:
    """Return an owner's persisted manifest, or ``None`` when absent."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT prefix, manifest, deadline FROM upload_manifests "
            "WHERE owner_kind = %s AND owner_id = %s",
            (owner_kind, owner_id),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    entries = tuple(_entry_from_payload(e) for e in row["manifest"])
    return UploadManifest(entries=entries, prefix=row["prefix"], deadline=row["deadline"])


def get_manifest_sync(
    conn: Connection, owner_kind: UploadOwnerKind, owner_id: UUID
) -> UploadManifest | None:
    """Return an owner's manifest over a sync connection for off-event-loop provider fetches."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT prefix, manifest, deadline FROM upload_manifests "
            "WHERE owner_kind = %s AND owner_id = %s",
            (owner_kind, owner_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    entries = tuple(_entry_from_payload(e) for e in row["manifest"])
    return UploadManifest(entries=entries, prefix=row["prefix"], deadline=row["deadline"])


def _entry_from_payload(payload: Any) -> ManifestEntry:
    raw_chunks = payload.get("chunks")
    chunks = (
        tuple(ChunkEntry(c["sha256"], int(c["size_bytes"])) for c in raw_chunks)
        if isinstance(raw_chunks, list)
        else None
    )
    encoding = payload.get("encoding")  # absent ⇒ identity (ADR-0437)
    raw_uncompressed = payload.get("uncompressed_size")
    uncompressed_size = int(raw_uncompressed) if raw_uncompressed is not None else None
    return ManifestEntry(
        payload["name"],
        payload["sha256"],
        int(payload["size_bytes"]),
        chunks=chunks,
        encoding=encoding,
        uncompressed_size=uncompressed_size,
    )


async def delete_manifest(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID
) -> None:
    """Delete an owner's manifest; absence is successful."""
    await conn.execute(
        "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
        (owner_kind, owner_id),
    )


def lock_scope_for(owner_kind: UploadOwnerKind) -> LockScope:
    """Return the required advisory-lock scope, rejecting unsupported owner kinds."""
    scope = _LOCK_SCOPES.get(owner_kind)
    if scope is None:
        raise ValueError(f"unsupported upload owner kind: {owner_kind}")
    return scope
