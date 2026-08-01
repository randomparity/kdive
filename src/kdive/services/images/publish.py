"""Row-first publish/register two-write for catalog images (ADR-0092, issue #285).

``publish_image`` registers the catalog row **before** the object, so a rowless object can
never exist during a live publish (the window in which ``leaked_images`` could race the write).
It adopts the identity's existing ``defined``/``pending`` row (or inserts a fresh ``pending``
row), sets its ``object_key``, writes the qcow2 to the image prefix, gates on ``store.head()``,
then flips the row to ``registered`` and returns it.

Publish is **idempotent on the scoped identity
``(provider, name, arch, visibility, owner)``**: a re-run after a crashed attempt adopts that
scope's in-flight ``pending`` row and re-arms its ``pending_since`` rather than colliding. Public
and private rows, and private rows for different owners, intentionally do not adopt each other.
The recovery path for a crash mid-publish is the reconciler, not a bespoke rollback — the leftover
``pending`` row and (possibly absent) object are swept by the deadline-guarded
``leaked_images``/``dangling_images`` sweeps once past the publish grace.

The blocking object-store calls (boto3) are offloaded via ``asyncio.to_thread`` so the worker
event loop never stalls behind a multi-GiB upload.

``publish_image`` is the composition of three steps that are also callable separately —
``reserve_publish`` (commit the ``pending`` row, recording the object's ``size_bytes``),
``write_publish_object`` (the object-store write, no DB access), and ``finish_publish`` (the flip
to ``registered``). The seam exists for the private-upload path, which holds the PROJECT advisory
lock across the reservation only and releases it before the PUT (ADR-0520, #1726): the committed
``pending`` row is the quota claim, so the lock does not have to span the write.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.cursor_async import AsyncCursor
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from kdive.artifacts import storage as artifact_types
from kdive.domain.catalog.image_format import ImageFormat
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.cataloging.object_keys import config_object_key, object_write_request
from kdive.images.cataloging.projection import IMAGE_CATALOG_ENTRY_PROJECTION

_log = logging.getLogger(__name__)


class ImageObjectStore(Protocol):
    """The narrow object-store capability publish needs (an :class:`ObjectStore` satisfies it)."""

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact: ...

    def head(self, key: str) -> artifact_types.HeadResult | None: ...


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """The fields needed to create an image row — not a built :class:`ImageCatalogEntry`.

    ``publish_image`` assigns the row's ``id``/``object_key``/``state``/``pending_since``; this
    request carries only the caller-supplied identity, boot layout, content digest, and scope.

    Attributes:
        provider: The provider whose plane built the image (e.g. ``"local-libvirt"``).
        name: The catalog image name.
        arch: The target architecture.
        format: The image format. Only ``"qcow2"`` is supported.
        root_device: The guest root device path (e.g. ``"/dev/vda"``).
        digest: The qcow2 content digest (``"sha256:<hex>"``) — the image identity, which the
            materialization fetch verifies the downloaded bytes against.
        capabilities: The guest-contract tags the image satisfies.
        provenance: The pinned build inputs/args, JSONB-serializable for the row.
        visibility: ``ImageVisibility.PUBLIC`` or ``ImageVisibility.PRIVATE``.
        owner: The owning project — set iff ``visibility`` is ``"private"``.
        expires_at: The private-image TTL deadline — set iff ``visibility`` is ``"private"``.
        kernel_config: The image's extracted ``/boot/config-<ver>`` bytes, or ``None`` when no
            config was captured (ADR-0317). Written best-effort as a sibling object of the qcow2;
            a failure degrades to a registered image with no config offered, never failing publish.
    """

    provider: str
    name: str
    arch: str
    format: ImageFormat
    root_device: str
    digest: str
    capabilities: tuple[str, ...]
    provenance: dict[str, object]
    visibility: ImageVisibility
    owner: str | None = None
    expires_at: datetime | None = None
    kernel_config: bytes | None = None

    def __post_init__(self) -> None:
        private = self.visibility is ImageVisibility.PRIVATE
        if private != (self.owner is not None):
            raise ValueError("owner must be set iff visibility is private")
        if private != (self.expires_at is not None):
            raise ValueError("expires_at must be set iff visibility is private")


@dataclass(frozen=True, slots=True)
class PublishReservation:
    """A committed ``pending`` row claiming its object keys and its quota bytes (ADR-0520).

    Handed from :func:`reserve_publish` to :func:`write_publish_object` and
    :func:`finish_publish` so the three steps share the row identity and the keys derived once at
    reservation, rather than re-deriving them per step.

    Attributes:
        row_id: The ``image_catalog`` row this publish owns.
        object_key: The qcow2's object-store key, already persisted on the row.
        config_key: The kernel-config sibling's key, or ``None`` when no config was captured.
        request: The originating :class:`PublishRequest`.
    """

    row_id: UUID
    object_key: str
    config_key: str | None
    request: PublishRequest


def _write_request(
    request: PublishRequest, data: bytes, *, suffix: str
) -> artifact_types.ArtifactWriteRequest:
    """A write request for ``request`` (delegates to the shared image object-key layout)."""
    return object_write_request(
        request.provider,
        request.name,
        request.arch,
        request.visibility,
        request.owner,
        data=data,
        suffix=suffix,
    )


def image_object_key(request: PublishRequest) -> str:
    """The object-store key for a catalog image, scoped to its visibility and owner.

    A public image lives under ``images/{provider}/{name}/{arch}.qcow2``; a private image is
    **owner-scoped** (``images/{provider}__{owner}/{name}/{arch}.qcow2``) so two projects' private
    images of the same identity never collide on one object. The key is persisted on the row, and
    the materialization fetch reads it from the row (it never recomputes the key), so the scheme is
    free to encode owner without a fetch-side change.
    """
    return _write_request(request, b"", suffix="qcow2").key()


def kernel_config_object_key(request: PublishRequest) -> str:
    """The object-store key for the image's ``/boot/config-<ver>`` sibling of the qcow2 (ADR-0317).

    Same tenant/owner scoping as :func:`image_object_key`; the ``.config`` suffix distinguishes it
    from the ``{arch}.qcow2`` object. Persisted on the row's ``kernel_config_key`` when a config is
    offered, ``None`` otherwise. Delegates to :func:`config_object_key` (the single key source).
    """
    return config_object_key(
        request.provider, request.name, request.arch, request.visibility, request.owner
    )


async def _adopt_or_insert_pending(
    conn: AsyncConnection,
    request: PublishRequest,
    object_key: str,
    config_key: str | None,
    size_bytes: int,
) -> UUID:
    """Adopt this scope's existing non-registered row, or insert a fresh ``pending`` row.

    Runs in one transaction so concurrent re-runs of the same image serialize on the adopted row.
    The match is scoped by ``(provider, name, arch, visibility, owner)`` — a public publish never
    adopts a project's private row and one project never adopts another's, so cross-tenant
    isolation holds (the private uniqueness key is ``(owner, provider, name)``). A ``defined``
    baseline and a crashed ``pending`` attempt are both adopted in place and moved to ``pending``
    with ``object_key`` set and ``pending_since`` re-armed; resolution never returns either, so an
    adopted row is never visible mid-publish.

    ``size_bytes`` is the size of the object this publish is about to write. It lands on the row
    *before* the object exists so the row is a durable quota claim (ADR-0520); an adopted row's
    stale size is overwritten by this attempt's.

    The adopt refreshes ``digest`` for the same reason it refreshes ``object_key``: the row must
    describe *this* attempt's bytes. Leaving the abandoned attempt's digest in place while writing
    different bytes registers an image the materialization fetch can never verify — the exact
    permanent-unfetchability :func:`_verify_source_digest` exists to prevent, arrived at from the
    other side. A ``defined`` baseline's ``NULL`` digest is filled in by the same assignment.
    """
    select_q = sql.SQL(
        "SELECT id FROM image_catalog "
        "WHERE provider = %(provider)s AND name = %(name)s AND arch = %(arch)s "
        "AND visibility = %(visibility)s AND owner IS NOT DISTINCT FROM %(owner)s "
        "AND state IN (%(defined)s, %(pending)s) "
        "ORDER BY CASE WHEN state = %(pending)s THEN 0 ELSE 1 END "
        "FOR UPDATE LIMIT 1"
    )
    params = {
        "provider": request.provider,
        "name": request.name,
        "arch": request.arch,
        "visibility": request.visibility.value,
        "owner": request.owner,
        "defined": ImageState.DEFINED.value,
        "pending": ImageState.PENDING.value,
    }
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(select_q, params)
        existing = await cur.fetchone()
        if existing is not None:
            await cur.execute(
                "UPDATE image_catalog "
                "SET state = %s, object_key = %s, kernel_config_key = %s, digest = %s, "
                "    size_bytes = %s, pending_since = now() "
                "WHERE id = %s",
                (
                    ImageState.PENDING.value,
                    object_key,
                    config_key,
                    request.digest,
                    size_bytes,
                    existing["id"],
                ),
            )
            return existing["id"]
        return await _insert_pending(cur, request, object_key, config_key, size_bytes)


async def _insert_pending(
    cur: AsyncCursor[DictRow],
    request: PublishRequest,
    object_key: str,
    config_key: str | None,
    size_bytes: int,
) -> UUID:
    """Insert a fresh ``pending`` row from ``request`` and return its id.

    ``cur`` is a ``dict_row`` cursor already inside the adopt transaction.
    """
    insert_q = (
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, kernel_config_key, digest, "
        " capabilities, provenance, visibility, owner, expires_at, state, size_bytes, "
        " pending_since) "
        "VALUES (%(provider)s, %(name)s, %(arch)s, %(format)s, %(root_device)s, %(object_key)s, "
        " %(kernel_config_key)s, %(digest)s, %(capabilities)s, %(provenance)s, %(visibility)s, "
        " %(owner)s, %(expires_at)s, %(state)s, %(size_bytes)s, now()) RETURNING id"
    )
    params = {
        "provider": request.provider,
        "name": request.name,
        "arch": request.arch,
        "format": request.format,
        "root_device": request.root_device,
        "object_key": object_key,
        "kernel_config_key": config_key,
        "digest": request.digest,
        "capabilities": list(request.capabilities),
        "provenance": Jsonb(request.provenance),
        "visibility": request.visibility.value,
        "owner": request.owner,
        "expires_at": request.expires_at,
        "state": ImageState.PENDING.value,
        "size_bytes": size_bytes,
    }
    await cur.execute(insert_q, params)
    row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into image_catalog returned no row")
    return row["id"]


def _verify_source_digest(data: bytes, digest: str) -> None:
    """Reject a publish whose source bytes do not hash to the row's declared ``digest``.

    The materialization fetch verifies ``sha256(object) == row.digest`` on every boot, so a row
    registered with a mismatched digest would be permanently unfetchable. Verifying here turns
    that latent corruption into a fail-fast at publish (the row stays ``pending``, never
    ``registered``). This matters most for a caller-supplied digest (the #286 private-upload path).
    """
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise CategorizedError(
            "published image bytes do not match the declared content digest",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"declared": digest, "actual": actual},
        )


async def _write_object(store: ImageObjectStore, request: PublishRequest, data: bytes) -> None:
    await asyncio.to_thread(store.put_artifact, _write_request(request, data, suffix="qcow2"))


async def _write_config_best_effort(
    store: ImageObjectStore, request: PublishRequest, config_key: str | None
) -> bool:
    """Write the config sibling object; return whether it is present. Never raises (advisory).

    The config is an advisory artifact (ADR-0317): a write/HEAD failure degrades to "no config
    offered" so the image still publishes — only the qcow2 write is fatal. A ``None`` key means no
    config was captured, so nothing is written.
    """
    if config_key is None or request.kernel_config is None:
        return False
    write = _write_request(request, request.kernel_config, suffix="config")
    try:
        await asyncio.to_thread(store.put_artifact, write)
        head = await asyncio.to_thread(store.head, config_key)
    except CategorizedError:
        _log.warning(
            "image kernel-config write failed for %s/%s (%s); registering with no config offered",
            request.name,
            request.arch,
            config_key,
            exc_info=True,
        )
        return False
    if head is None:
        _log.warning(
            "image kernel-config object %s absent after write for %s/%s; no config offered",
            config_key,
            request.name,
            request.arch,
        )
        return False
    return True


async def _registered(
    conn: AsyncConnection, reservation: PublishReservation, *, clear_config_key: bool = False
) -> ImageCatalogEntry:
    """Flip the reserved row to ``registered``, fenced on the reservation still owning it.

    The predicate is the reservation's identity — ``id`` **and** the ``digest``/``object_key`` it
    wrote — not ``id`` alone. `id` alone is not enough once the lock no longer spans the write
    (ADR-0520 §7): a concurrent publish of the same identity adopts this very row under
    :func:`_adopt_or_insert_pending`'s ``FOR UPDATE``, overwriting ``digest`` with its own, and
    both attempts then race to PUT the same key. Whichever object survives, at most one attempt's
    digest is still on the row, so registering on ``id`` alone lets the loser publish a row whose
    digest can never match its object — a live, quota-consuming, permanently unfetchable image,
    with both callers told they succeeded.

    Raises:
        CategorizedError: ``CONFLICT`` when the row no longer carries this reservation — a later
            reservation of the same identity superseded it, or the reconciler swept it past its
            publish deadline. Either way this attempt must not register, and the caller gets a
            typed error rather than a corrupt success or a bare ``RuntimeError``.
    """
    set_clause = "state = %s" + (", kernel_config_key = NULL" if clear_config_key else "")
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"UPDATE image_catalog SET {set_clause} "
            "WHERE id = %s AND digest = %s AND object_key = %s "
            f"RETURNING {IMAGE_CATALOG_ENTRY_PROJECTION}",
            (
                ImageState.REGISTERED.value,
                reservation.row_id,
                reservation.request.digest,
                reservation.object_key,
            ),
        )
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError(
            "this publish's reservation no longer owns its catalog row; a concurrent publish of "
            "the same image identity superseded it, or the reconciler reclaimed it past the "
            "publish deadline",
            category=ErrorCategory.CONFLICT,
            details={"row_id": str(reservation.row_id), "object_key": reservation.object_key},
        )
    return ImageCatalogEntry.model_validate(row)


async def reserve_publish(
    conn: AsyncConnection, request: PublishRequest, *, size_bytes: int
) -> PublishReservation:
    """Commit this publish's ``pending`` row — the object's claim on its key and its quota bytes.

    The first of the three steps :func:`publish_image` composes. Split out so a caller that must
    enforce a quota can hold its lock across *this* step alone and release it before
    :func:`write_publish_object` (ADR-0520): the committed row already counts toward the
    per-project caps, so a concurrent upload's aggregate read sees the claim without the lock
    being held over the write.

    Args:
        conn: An async Postgres connection; the adopt/insert opens its own transaction.
        request: The image identity, layout, digest, and scope.
        size_bytes: The size of the object about to be written — recorded on the row before the
            object exists, which is what makes the row a quota claim rather than a placeholder.

    Returns:
        The :class:`PublishReservation` naming the committed row and its object keys.
    """
    object_key = image_object_key(request)
    config_key = kernel_config_object_key(request) if request.kernel_config is not None else None
    row_id = await _adopt_or_insert_pending(conn, request, object_key, config_key, size_bytes)
    return PublishReservation(
        row_id=row_id, object_key=object_key, config_key=config_key, request=request
    )


async def write_publish_object(
    store: ImageObjectStore, reservation: PublishReservation, source: Path
) -> bool:
    """Write the reserved row's qcow2 (and best-effort config sibling); return config presence.

    The second of the three steps, and the only one that touches the object store. It issues **no
    database statement at all**, which is what lets a quota-enforcing caller run it with no lock
    held (ADR-0520). Verifies the source bytes against the row's declared digest, PUTs the qcow2,
    HEAD-gates it, then writes the config sibling best-effort.

    A failure here leaves the reserved ``pending`` row behind holding its quota bytes; that row is
    reclaimed by the reconciler's ``repair_dangling_images`` on its ``pending_since`` deadline
    (ADR-0092's recovery path, not a bespoke rollback).

    Returns:
        Whether the config sibling object is present, for :func:`finish_publish`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``source`` bytes do not hash to the reserved
            row's ``digest``; ``INFRASTRUCTURE_FAILURE`` if the qcow2 write or HEAD gate fails.
    """
    request = reservation.request
    data = await asyncio.to_thread(source.read_bytes)
    _verify_source_digest(data, request.digest)
    await _write_object(store, request, data)

    head = await asyncio.to_thread(store.head, reservation.object_key)
    if head is None:
        raise CategorizedError(
            "published image object is not present after write (HEAD gate failed)",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"object_key": reservation.object_key},
        )
    return await _write_config_best_effort(store, request, reservation.config_key)


async def finish_publish(
    conn: AsyncConnection, reservation: PublishReservation, *, config_written: bool
) -> ImageCatalogEntry:
    """Flip the reserved row to ``registered`` and return it.

    The third of the three steps. It opens no transaction of its own, so a caller can compose it
    with whatever else must land atomically with the registration — the private-upload path
    composes it with its audit row, which :func:`kdive.security.audit.record_system` likewise
    leaves to the caller to wrap.

    Raises:
        CategorizedError: ``CONFLICT`` when the reservation no longer owns its row (superseded by
            a concurrent same-identity publish, or reclaimed by the reconciler). See
            :func:`_registered` for why the fence is the reservation's identity, not its ``id``.
    """
    return await _registered(
        conn,
        reservation,
        clear_config_key=reservation.config_key is not None and not config_written,
    )


async def publish_image(
    conn: AsyncConnection, store: ImageObjectStore, *, request: PublishRequest, source: Path
) -> ImageCatalogEntry:
    """Row-first two-write publish: pending row → object → HEAD-gate → ``registered``.

    Adopts the identity's existing ``defined``/``pending`` row (or inserts a ``pending`` row from
    ``request``), sets its ``object_key``, writes the object at ``source`` to the image prefix,
    HEAD-gates, then flips the row to ``registered`` and returns it. Idempotent on the scoped
    identity ``(provider, name, arch, visibility, owner)``: a re-run adopts that scope's in-flight
    ``pending`` row and re-arms its ``pending_since``. Public and private rows, and private rows
    for different owners, intentionally do not adopt each other. Realizing a seeded ``defined``
    baseline is this same path.

    When ``request.kernel_config`` is present its deterministic ``{arch}.config`` key is set on the
    ``pending`` row before any object is written (so the leaked-sweep protects it the instant the
    row exists, ADR-0317), and the config object is written **best-effort** after the qcow2
    HEAD-gate: a config write/HEAD failure degrades to a registered image with ``kernel_config_key``
    cleared (no config offered), never failing the publish. Only the qcow2 write/HEAD is fatal.

    Args:
        conn: An async Postgres connection (autocommit; the adopt step opens its own
            transaction).
        store: The image object store.
        request: The image identity, layout, digest, and scope.
        source: The local path to the built qcow2 to publish.

    Returns:
        The persisted ``registered`` :class:`ImageCatalogEntry`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``source`` bytes do not hash to
            ``request.digest`` (the catalog identity the materialization fetch verifies against);
            ``INFRASTRUCTURE_FAILURE`` if the object write or HEAD gate fails (the row stays
            ``pending`` for the reconciler to recover).
    """
    stat = await asyncio.to_thread(source.stat)
    reservation = await reserve_publish(conn, request, size_bytes=stat.st_size)
    config_written = await write_publish_object(store, reservation, source)
    return await finish_publish(conn, reservation, config_written=config_written)
