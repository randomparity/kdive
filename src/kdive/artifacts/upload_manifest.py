"""Owner-scoped upload-manifest storage for external-build ingestion (ADR-0048 §4).

A manifest is the declared ``(name, sha256, size_bytes)`` set an agent commits at
the artifact upload tools for one owner (a CREATED Run or a DEFINED System), plus the
object-key ``prefix`` the reaper lists and the ``deadline`` it keys off. It is replaced
wholesale on a re-mint (one call, full set) and deleted when the owner finalizes or is
reaped. It is not the write-once ``artifacts`` row.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.artifacts.uploads import ChunkEntry, ManifestEntry
from kdive.db.locks import LockScope

UploadOwnerKind = Literal["runs", "investigations"]
RUN_UPLOAD_OWNER: UploadOwnerKind = "runs"
INVESTIGATION_UPLOAD_OWNER: UploadOwnerKind = "investigations"

#: The object-store tenant every upload window mints under. Defined here, beside the owner kinds,
#: because the mint sites and the reconciler's orphan sweep (ADR-0455) must agree on the whole
#: prefix: a sweep whose tenant drifts from the mint's lists an empty prefix and reports a healthy
#: zero forever while the leak resumes.
UPLOAD_TENANT = "local"

# The advisory-lock scope each upload owner kind serializes under. It lives here rather than with
# the reaper that used to own it because three separate mechanisms now have to agree on it: the
# reaper's claim, the ADR-0502 write lease's mint, and the orphan sweep's per-key delete. The mint
# and the delete take the *same* lock, and that pairing is the whole of ADR-0502's ordering
# argument — a second copy of this two-entry map would unpair them silently.
_LOCK_SCOPES: dict[UploadOwnerKind, LockScope] = {
    RUN_UPLOAD_OWNER: LockScope.RUN,
    INVESTIGATION_UPLOAD_OWNER: LockScope.INVESTIGATION,
}

#: The owner kinds an upload window can be minted for, in reap order. The orphan sweep (ADR-0455)
#: derives the object-store roots it walks from this, so its scope cannot drift from the reaper's.
UPLOAD_OWNER_KINDS: tuple[UploadOwnerKind, ...] = tuple(_LOCK_SCOPES)

UPLOAD_WINDOW_EXPIRED = "upload_window_expired"
"""The window's deadline had passed when a finalize arrived (ADR-0444, ADR-0448 §1).

The agent-facing reason string both finalize lanes emit. It lives beside :attr:`ManifestStamp
.expired` — the predicate that decides it — rather than in either lane, because the lanes sit at
different layers: the runs finalize enforces in the service layer, the investigations finalize in
the MCP tool layer, and this is the only module both can import without one reaching into the
other's stack (ADR-0512).
"""


class UploadManifest(NamedTuple):
    """A persisted manifest: the declared entries, the key prefix, and the deadline."""

    entries: tuple[ManifestEntry, ...]
    prefix: str
    deadline: datetime


class ManifestStamp(NamedTuple):
    """The reference clock and reaper deadline for one manifest upsert (#1336).

    Both are read from the same INSERT's ``now()`` (statement-stable), so
    ``deadline - server_time == ttl`` exactly and ``server_time`` is the same clock
    the reaper measures ``deadline`` against. Both are timezone-aware (``timestamptz``).
    """

    server_time: datetime
    deadline: datetime

    @property
    def expired(self) -> bool:
        """Whether the upload window had already closed at ``server_time`` (ADR-0512).

        The single expiry rule both finalize lanes ask. Each used to write its own comparison,
        and they were spelled as exact logical negations — ``deadline < server_time`` in the runs
        service, ``deadline >= server_time`` in the investigations tool — so a later change to the
        rule would have landed on one lane only (#1555).

        A bare ``bool``, deliberately: the runs lane raises on it from the service layer and the
        investigations lane returns a ``ToolResponse`` from the MCP tool layer, so the shared
        verdict can carry neither lane's signalling type.

        A deadline **equal** to the reference clock is open, not expired. Both SQL sites agree at
        that instant from opposite directions: :func:`refresh_deadline` extends a window matching
        ``deadline >= now()``, and the reaper claims one matching ``deadline < now()``. So a
        finalize and the reaper cannot reach opposite verdicts on one manifest.
        """
        return self.deadline < self.server_time


class WindowRefresh(NamedTuple):
    """The outcome of one :func:`refresh_deadline` extension (ADR-0511).

    ``deadline`` is the value on the row after the update — the window's identity, which the
    finalize carries to its locked pre-commit re-read (ADR-0448 §2). It is *not* necessarily
    ``now() + ttl``: an extension is clamped to the window's cumulative cap, and it never shortens
    an already-open window, so on a spent budget it is the unchanged prior deadline.

    ``capped`` is true when this refresh did not leave the deadline a full ``ttl`` out — either
    ``LEAST`` clamped the grant short of one, or ``GREATEST`` kept a standing deadline that already
    reached past one and the refresh granted nothing at all (#1724). It is computed in SQL against
    the same statement's ``now()`` rather than derived by the caller, because the only clock the
    comparison may use is Postgres's (ADR-0448 decision 1).
    """

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
    """Upsert the owner's manifest, stamping ``deadline = now() + ttl`` in Postgres.

    Full-set replace: a re-mint overwrites the prior manifest, prefix, and deadline.

    It also restamps ``window_started_at``, which is what makes a re-mint the reset for
    :func:`refresh_deadline`'s cumulative cap (ADR-0511). Deliberately *not* ``created_at``, which
    this upsert leaves alone: a cap measured from the row's birth would bound the re-mint too, and
    granting a full fresh window on demand is the recovery ADR-0448 points every "your window is
    gone" rejection at. Both timestamps come from one statement's ``now()``, so
    ``deadline - window_started_at == ttl`` exactly on a fresh window.

    Args:
        conn: An async connection (autocommit or within a transaction).
        request: Owner, prefix, entries, and upload-window TTL for the replacement.

    Returns:
        The upsert's :class:`ManifestStamp` — ``now()`` (the reference clock) and the
        stamped ``deadline``, both from the same statement so the agent-facing contract
        (#1336) measures the deadline against the clock the reaper uses.
    """
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
    """Extend a non-expired manifest's deadline toward ``now() + ttl``, bounded by ``max_window``.

    Refreshing the deadline under the per-Run lock the reaper also takes is what stops the reaper
    from reclaiming an in-flight reassembly's chunk objects (ADR-0104 §6 step A).

    The extension is **bounded and monotonic** (ADR-0511), provided ``KDIVE_UPLOAD_TTL_SECONDS`` has
    been stable since the window's mint::

        deadline := GREATEST(deadline, LEAST(now() + ttl, window_started_at + max_window))

    ``LEAST`` is the cap: no sequence of refreshes can carry one minted window past
    ``max_window`` from its mint, which is what stops a client retrying a failing finalize inside
    its own still-open window from buying a fresh TTL on every attempt, forever. ``GREATEST`` is
    what keeps a spent budget from *shortening* an open window — a refresh that moved the deadline
    backward would hand the reaper the very chunk objects this call exists to protect, converting
    a retention bound into data loss.

    That same ``GREATEST`` is why the cap only holds under a stable ``ttl``: a decrease after the
    mint (or a row migration ``0085``'s backfill lands on, minted under a larger historical TTL)
    leaves the standing deadline — stamped at the mint's own ``window_started_at + ttl`` — larger
    than the *new*, smaller ``window_started_at + max_window``, and every later refresh's clamped
    grant is smaller still, so ``GREATEST`` never selects it. The window then decays out at the old
    bound rather than the new one: a bounded leak, never an exposure, since no refresh moves the
    deadline later than the mint already placed it.

    That outcome is a capped one and is reported as such (#1724). ``capped`` asks whether the
    deadline landed a full ``ttl`` out, not whether it landed short of one, because both clamps
    withhold the grant: ``LEAST`` by holding the deadline below ``now() + ttl``, ``GREATEST`` by
    holding one already above it. The second only ever selects the standing deadline, so a refresh
    reporting a deadline past ``now() + ttl`` is a refresh that moved nothing.

    ``window_started_at`` is restamped only by :func:`replace_manifest`, so a re-mint through
    ``artifacts.create_run_upload`` starts a new window with a full budget. That is deliberate:
    ADR-0448 makes the re-mint the advertised recovery from every "your window is gone" rejection,
    and a cap that bound it too would take away a capability rather than bound a silent one.

    Every comparison is against Postgres's ``now()``, never a Python clock — the reaper measures
    ``deadline`` against the same one, so the two cannot reach opposite verdicts under skew.

    ``None`` means the ``UPDATE`` matched nothing: either no row exists or its deadline is already
    past. **The caller must have established that the window is open before calling** — the sole
    caller, ``runs.complete_build``, does so via ``_require_open_window`` earlier in the same
    transaction, and ``now()`` is ``transaction_timestamp()``, so the ``deadline >= now()`` arm
    cannot flip in between and a ``None`` there means the row was reaped. A caller without that
    precondition cannot tell the two apart from the return value alone and must re-read the row.
    A spent budget never returns ``None``: it returns the unchanged deadline with ``capped`` set,
    so the caller cannot mistake an exhausted extension for a reaped window.

    Args:
        conn: An async connection, in the transaction whose ``now()`` is the reference clock.
        owner_kind: The owning table name — ``'runs'`` or ``'investigations'``.
        owner_id: The owning row's primary key.
        ttl: The extension a refresh grants when the cap leaves room for it.
        max_window: The furthest past ``window_started_at`` this window's deadline may reach.

    Returns:
        The post-update deadline and whether the cap bound this refresh, or ``None`` if no open
        window exists. The deadline is returned rather than a success flag so the caller can carry
        it as the identity of the window it is committing against (ADR-0448 §2): a later re-read
        finding a different value is a manifest some other call replaced.
    """
    # The `RETURNING` equality is exact, not a comparison hoping two clocks agree: `now()` is
    # `transaction_timestamp()`, so every occurrence below — the one `LEAST` clamps against and the
    # one the predicate re-reads — is the same timestamp, and `timestamptz` arithmetic on it is
    # exact. `deadline = now() + ttl` therefore holds precisely when `GREATEST`/`LEAST` selected
    # that term, which is the definition of a full grant; anything else is a capped one. Testing
    # `<>` rather than `<` is what makes `GREATEST` report: it holds a standing deadline stamped
    # under a larger historical `ttl` *above* `now() + ttl`, granting nothing, and `<` read that as
    # uncapped (#1724).
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
    """Pair a fetched manifest's deadline with the DB reference clock.

    The read-side twin of :func:`replace_manifest`'s write-side stamp (ADR-0444): both halves of
    the agent-facing deadline contract (#1336) are rendered from the same two fields, and both
    read ``now()`` — the clock the deadline was stamped from and the reaper measures against — so
    a finalize and the reaper cannot reach opposite verdicts on one manifest.

    Args:
        conn: An async connection, in the transaction whose ``now()`` is the reference clock.
        manifest: The already-fetched manifest whose deadline is being measured.

    Returns:
        The manifest's deadline beside that transaction's ``now()``.
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT now()")
        row = await cur.fetchone()
    if row is None:  # a bare SELECT always yields one row; fail loud if it ever does not
        raise RuntimeError("deadline_stamp could not read the database reference clock")
    return ManifestStamp(server_time=row[0], deadline=manifest.deadline)


async def window_deadline(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID
) -> datetime | None:
    """Return just the owner's manifest deadline, or ``None`` if no row exists.

    The scalar twin of :func:`get_manifest` for callers that only need the window's identity
    (ADR-0448 §2). ``get_manifest`` pulls the whole ``manifest`` JSONB and rebuilds a
    :class:`ManifestEntry` per artifact — thousands of :class:`ChunkEntry` tuples for a chunked
    multi-GiB upload — which is wasted work when the caller compares one timestamp, and the run
    finalize's comparison happens inside the ``RUN`` advisory lock.

    Args:
        conn: An async connection.
        owner_kind: The owning table name — ``'runs'`` or ``'investigations'``.
        owner_id: The owning row's primary key.

    Returns:
        The window's deadline, or ``None`` if no manifest is recorded for this owner.
    """
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
    """Return the owner's manifest, or ``None`` if none is recorded.

    Args:
        conn: An async connection.
        owner_kind: The owning table name — ``'runs'`` or ``'systems'``.
        owner_id: The owning row's primary key.

    Returns:
        The persisted manifest, or ``None`` if no row exists for this owner.
    """
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
    """Return the owner's manifest over a **sync** connection, or ``None`` if none is recorded.

    The sync twin of :func:`get_manifest`, sharing the :func:`_entry_from_payload` deserializer. It
    exists for the connectionless local-libvirt provider fetch, which — like the ADR-0228 catalog
    fetch — runs off the event loop (``asyncio.to_thread``) and opens its own short-lived sync
    connection rather than borrowing the async pool.

    Args:
        conn: A sync connection.
        owner_kind: The owning table name — ``'runs'`` or ``'systems'``.
        owner_id: The owning row's primary key.

    Returns:
        The persisted manifest, or ``None`` if no row exists for this owner.
    """
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
    """Delete the owner's manifest row (idempotent — absent is fine).

    Args:
        conn: An async connection.
        owner_kind: The owning table name — ``'runs'`` or ``'systems'``.
        owner_id: The owning row's primary key.
    """
    await conn.execute(
        "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
        (owner_kind, owner_id),
    )


def lock_scope_for(owner_kind: UploadOwnerKind) -> LockScope:
    """Return the advisory-lock scope for an upload owner kind, failing loud on an unknown one.

    An owner kind no writer of that owner locks under must never be locked under a guessed scope —
    that would take a lock nobody else takes and serialize against nothing at all. Both the reaper's
    claim and ADR-0502's write-lease mint/delete pairing depend on this raising rather than falling
    back.
    """
    scope = _LOCK_SCOPES.get(owner_kind)
    if scope is None:
        raise ValueError(f"unsupported upload owner kind: {owner_kind}")
    return scope
