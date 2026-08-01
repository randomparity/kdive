"""Row-first publish/register two-write for catalog images (ADR-0092, ADR-0526, issue #285).

``publish_image`` registers the catalog row **before** the object, so a rowless object can
never exist during a live publish (the window in which ``leaked_images`` could race the write).
It adopts the identity's existing ``defined``/``pending`` row (or inserts a fresh ``pending``
row), sets its ``object_key``, writes the qcow2 to the image prefix, gates on ``store.head()``,
then flips the row to ``registered`` and returns it.

Pending-row adoption follows each visibility's registered identity. Public publication uses
``(provider, name, arch)``; private publication uses ``(owner, provider, name)`` without arch,
matching its registered uniqueness constraint. A re-run after a crashed attempt adopts that
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
``pending`` row is the quota claim, so no PROJECT or transaction-scoped lock spans the write. The
IMAGE_PUBLISH session lock remains held through the PUT, finish, and private registration audit
(ADR-0525).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from psycopg import AsyncConnection, sql
from psycopg.cursor_async import AsyncCursor
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from kdive.artifacts import storage as artifact_types
from kdive.domain.catalog.image_format import ImageFormat
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.cataloging.object_keys import (
    config_object_key,
    object_write_request,
    publication_write_request,
)
from kdive.images.cataloging.projection import IMAGE_CATALOG_ENTRY_PROJECTION
from kdive.services.images.publication_fence import publication_fence

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
        publication_attempt_id: The unique attempt that currently owns the row.
        object_key: The qcow2's object-store key, already persisted on the row.
        config_key: The kernel-config sibling's key, or ``None`` when no config was captured.
        size_bytes: The expected qcow2 size persisted by the reservation.
        request: The originating :class:`PublishRequest`.
    """

    row_id: UUID
    publication_attempt_id: UUID
    object_key: str
    config_key: str | None
    size_bytes: int
    request: PublishRequest


def _write_request(
    request: PublishRequest, data: bytes, *, suffix: str, attempt_id: UUID | None = None
) -> artifact_types.ArtifactWriteRequest:
    """A write request for ``request`` (delegates to the shared image object-key layout)."""
    if attempt_id is not None:
        return publication_write_request(
            request.provider,
            request.name,
            request.arch,
            request.visibility,
            request.owner,
            attempt_id=attempt_id,
            data=data,
            suffix=suffix,
        )
    return object_write_request(
        request.provider,
        request.name,
        request.arch,
        request.visibility,
        request.owner,
        data=data,
        suffix=suffix,
    )


def image_object_key(request: PublishRequest, attempt_id: UUID | None = None) -> str:
    """The object-store key for a catalog image, scoped to its visibility and owner.

    A public image lives under ``images/{provider}/{name}/{arch}.qcow2``; a private image is
    **owner-scoped** (``images/{provider}__{owner}/{name}/{arch}.qcow2``) so two projects' private
    images of the same identity never collide on one object. The key is persisted on the row, and
    the materialization fetch reads it from the row (it never recomputes the key), so the scheme is
    free to encode owner without a fetch-side change.
    """
    return _write_request(request, b"", suffix="qcow2", attempt_id=attempt_id).key()


def kernel_config_object_key(request: PublishRequest, attempt_id: UUID | None = None) -> str:
    """The object-store key for the image's ``/boot/config-<ver>`` sibling of the qcow2 (ADR-0317).

    With an ``attempt_id``, this is the attempt-specific publish sibling of the qcow2. Without one,
    it retains the deterministic staged/inventory config key from :func:`config_object_key`.
    """
    if attempt_id is not None:
        return _write_request(request, b"", suffix="config", attempt_id=attempt_id).key()
    return config_object_key(
        request.provider, request.name, request.arch, request.visibility, request.owner
    )


def _adoption_candidate_query(*, pending_only: bool) -> sql.Composed:
    """Build the one deterministic, row-locking adoption-candidate query (ADR-0526)."""
    state_predicate = (
        sql.SQL("state = %(pending)s")
        if pending_only
        else sql.SQL("state IN (%(defined)s, %(pending)s)")
    )
    return sql.SQL(
        "SELECT id, state FROM image_catalog "
        "WHERE provider = %(provider)s AND name = %(name)s "
        "AND visibility = %(visibility)s AND owner IS NOT DISTINCT FROM %(owner)s "
        "AND {state_predicate} "
        "AND (arch = %(arch)s OR (visibility = %(private)s AND state = %(pending)s)) "
        "ORDER BY CASE WHEN state = %(pending)s THEN 0 ELSE 1 END, created_at, id "
        "FOR UPDATE LIMIT 1"
    ).format(state_predicate=state_predicate)


def _adoption_candidate_params(request: PublishRequest) -> dict[str, object]:
    """Return parameters shared by quota selection and publish adoption."""
    return {
        "provider": request.provider,
        "name": request.name,
        "arch": request.arch,
        "visibility": request.visibility.value,
        "owner": request.owner,
        "private": ImageVisibility.PRIVATE.value,
        "defined": ImageState.DEFINED.value,
        "pending": ImageState.PENDING.value,
    }


async def lock_pending_adoption_candidate(
    conn: AsyncConnection, request: PublishRequest
) -> UUID | None:
    """Lock and return the exact pending row a reservation would adopt, if any.

    The caller owns the surrounding transaction. Holding this row lock through quota accounting
    keeps a reconciler from changing the selected claim before reservation; if no row is selected,
    quota counts every current claim and therefore remains fail-closed if a candidate appears later.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _adoption_candidate_query(pending_only=True), _adoption_candidate_params(request)
        )
        candidate = await cur.fetchone()
    return None if candidate is None else candidate["id"]


async def _adopt_or_insert_pending(
    conn: AsyncConnection,
    request: PublishRequest,
    object_key: str,
    config_key: str | None,
    size_bytes: int,
    publication_attempt_id: UUID,
    publication_principal: str | None,
) -> UUID:
    """Adopt this scope's existing non-registered row, or insert a fresh ``pending`` row.

    Runs in one transaction so concurrent re-runs of the same image serialize on the adopted row.
    Public pending rows match ``(provider, name, arch)``. Private pending rows match
    ``(owner, provider, name)`` without arch, exactly like the registered-private uniqueness key;
    adopting a private row replaces all request-owned durable fields, including its arch, format,
    layout, capabilities, provenance, expiry, digest, size, keys, and publication attempt. A public
    publish never adopts a project's private row and one project never adopts another's, so
    cross-tenant isolation holds. A ``defined`` baseline remains arch-scoped and preserves its
    declared metadata while gaining the realized object fields. Both cases move the row to
    ``pending`` and re-arm ``pending_since``; resolution never returns either, so an adopted row is
    never visible mid-publish (ADR-0526).

    ``size_bytes`` is the size of the object this publish is about to write. It lands on the row
    *before* the object exists so the row is a durable quota claim (ADR-0520); an adopted row's
    stale size is overwritten by this attempt's.

    The adopt refreshes ``digest`` for the same reason it refreshes ``object_key``: the row must
    describe *this* attempt's bytes. Leaving the abandoned attempt's digest in place while writing
    different bytes registers an image the materialization fetch can never verify — the exact
    permanent-unfetchability :func:`_verify_source_digest` exists to prevent, arrived at from the
    other side. A ``defined`` baseline's ``NULL`` digest is filled in by the same assignment.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _adoption_candidate_query(pending_only=False), _adoption_candidate_params(request)
        )
        existing = await cur.fetchone()
        if existing is not None:
            if existing["state"] == ImageState.PENDING.value:
                await _refresh_pending(
                    cur,
                    existing["id"],
                    request,
                    object_key,
                    config_key,
                    size_bytes,
                    publication_attempt_id,
                    publication_principal,
                )
            else:
                await _realize_defined(
                    cur,
                    existing["id"],
                    request,
                    object_key,
                    config_key,
                    size_bytes,
                    publication_attempt_id,
                    publication_principal,
                )
            return existing["id"]
        return await _insert_pending(
            cur,
            request,
            object_key,
            config_key,
            size_bytes,
            publication_attempt_id,
            publication_principal,
        )


async def _refresh_pending(
    cur: AsyncCursor[DictRow],
    row_id: UUID,
    request: PublishRequest,
    object_key: str,
    config_key: str | None,
    size_bytes: int,
    publication_attempt_id: UUID,
    publication_principal: str | None,
) -> None:
    """Replace every request-owned field on a superseded pending attempt."""
    await cur.execute(
        "UPDATE image_catalog "
        "SET state = %s, arch = %s, format = %s, root_device = %s, object_key = %s, "
        "    kernel_config_key = %s, digest = %s, capabilities = %s, provenance = %s, "
        "    expires_at = %s, size_bytes = %s, pending_since = now(), "
        "    publication_attempt_id = %s, publication_principal = %s "
        "WHERE id = %s",
        (
            ImageState.PENDING.value,
            request.arch,
            request.format,
            request.root_device,
            object_key,
            config_key,
            request.digest,
            list(request.capabilities),
            Jsonb(request.provenance),
            request.expires_at,
            size_bytes,
            publication_attempt_id,
            publication_principal,
            row_id,
        ),
    )


async def _realize_defined(
    cur: AsyncCursor[DictRow],
    row_id: UUID,
    request: PublishRequest,
    object_key: str,
    config_key: str | None,
    size_bytes: int,
    publication_attempt_id: UUID,
    publication_principal: str | None,
) -> None:
    """Realize a defined baseline without replacing its declared metadata."""
    await cur.execute(
        "UPDATE image_catalog "
        "SET state = %s, object_key = %s, kernel_config_key = %s, digest = %s, "
        "    size_bytes = %s, pending_since = now(), publication_attempt_id = %s, "
        "    publication_principal = %s "
        "WHERE id = %s",
        (
            ImageState.PENDING.value,
            object_key,
            config_key,
            request.digest,
            size_bytes,
            publication_attempt_id,
            publication_principal,
            row_id,
        ),
    )


async def _insert_pending(
    cur: AsyncCursor[DictRow],
    request: PublishRequest,
    object_key: str,
    config_key: str | None,
    size_bytes: int,
    publication_attempt_id: UUID,
    publication_principal: str | None,
) -> UUID:
    """Insert a fresh ``pending`` row from ``request`` and return its id.

    ``cur`` is a ``dict_row`` cursor already inside the adopt transaction.
    """
    insert_q = (
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, kernel_config_key, digest, "
        " capabilities, provenance, visibility, owner, expires_at, state, size_bytes, "
        " publication_attempt_id, publication_principal, pending_since) "
        "VALUES (%(provider)s, %(name)s, %(arch)s, %(format)s, %(root_device)s, %(object_key)s, "
        " %(kernel_config_key)s, %(digest)s, %(capabilities)s, %(provenance)s, %(visibility)s, "
        " %(owner)s, %(expires_at)s, %(state)s, %(size_bytes)s, %(publication_attempt_id)s, "
        " %(publication_principal)s, now()) RETURNING id"
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
        "publication_attempt_id": publication_attempt_id,
        "publication_principal": publication_principal,
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


def _digest_error(digest: str) -> CategorizedError:
    return CategorizedError(
        "image digest must be sha256:<64 hexadecimal digits>",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"digest": digest},
    )


def digest_sha256_b64(digest: str) -> str:
    """Convert a strict ``sha256:<hex>`` digest to canonical padded base64."""
    algorithm, separator, value = digest.partition(":")
    if algorithm != "sha256" or separator != ":" or len(value) != 64:
        raise _digest_error(digest)
    try:
        raw = bytes.fromhex(value)
    except ValueError as err:
        raise _digest_error(digest) from err
    if len(raw) != 32:
        raise _digest_error(digest)
    return base64.b64encode(raw).decode("ascii")


async def _write_object(
    store: ImageObjectStore, reservation: PublishReservation, data: bytes
) -> None:
    write = _write_request(
        reservation.request,
        data,
        suffix="qcow2",
        attempt_id=reservation.publication_attempt_id,
    )
    await asyncio.to_thread(
        store.put_artifact,
        replace(write, sha256_b64=digest_sha256_b64(reservation.request.digest)),
    )


async def _write_config_best_effort(
    store: ImageObjectStore,
    request: PublishRequest,
    config_key: str | None,
    *,
    attempt_id: UUID | None = None,
) -> bool:
    """Write the config sibling object; return whether it is present. Never raises (advisory).

    The config is an advisory artifact (ADR-0317): a write/HEAD failure degrades to "no config
    offered" so the image still publishes — only the qcow2 write is fatal. A ``None`` key means no
    config was captured, so nothing is written.
    """
    if config_key is None or request.kernel_config is None:
        return False
    write = _write_request(request, request.kernel_config, suffix="config", attempt_id=attempt_id)
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
    wrote — not ``id`` alone. `id` alone is not enough once the PROJECT lock no longer spans the
    write (ADR-0520 §7): the IMAGE_PUBLISH session fence orders recovery, but another publisher's
    reservation phase can still adopt this row while the first publisher holds that fence. The
    adoption overwrites the row's attempt, digest, and attempt-specific key. Registering on ``id``
    alone would then let the superseded publisher claim success for an object the row no longer
    names.

    Raises:
        CategorizedError: ``CONFLICT`` when the row no longer carries this reservation — a later
            reservation of the same identity superseded it, or the reconciler swept it past its
            publish deadline. Either way this attempt must not register, and the caller gets a
            typed error rather than a corrupt success or a bare ``RuntimeError``.
    """
    set_clause = "state = %s, publication_attempt_id = NULL, publication_principal = NULL" + (
        ", kernel_config_key = NULL" if clear_config_key else ""
    )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"UPDATE image_catalog SET {set_clause} "
            "WHERE id = %s AND publication_attempt_id = %s AND digest = %s AND object_key = %s "
            f"RETURNING {IMAGE_CATALOG_ENTRY_PROJECTION}",
            (
                ImageState.REGISTERED.value,
                reservation.row_id,
                reservation.publication_attempt_id,
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
    conn: AsyncConnection,
    request: PublishRequest,
    *,
    size_bytes: int,
    principal: str | None = None,
) -> PublishReservation:
    """Commit this publish's ``pending`` row — the object's claim on its key and its quota bytes.

    The first of the three steps :func:`publish_image` composes. Split out so a caller that must
    enforce a quota can hold its PROJECT lock across *this* step alone and release it before
    :func:`write_publish_object` (ADR-0520): the committed row already counts toward the
    per-project caps, so a concurrent upload's aggregate read sees the claim without the PROJECT
    lock being held over the write. The composed publish path acquires IMAGE_PUBLISH after this
    reservation and holds that row-scoped session lock through write and finish (ADR-0525).

    Args:
        conn: An async Postgres connection; the adopt/insert opens its own transaction.
        request: The image identity, layout, digest, and scope.
        size_bytes: The size of the object about to be written — recorded on the row before the
            object exists, which is what makes the row a quota claim rather than a placeholder.

    Returns:
        The :class:`PublishReservation` naming the committed row and its object keys.
    """
    if request.visibility is ImageVisibility.PRIVATE and principal is None:
        raise ValueError("private image reservation requires a principal")
    digest_sha256_b64(request.digest)
    publication_attempt_id = uuid4()
    object_key = image_object_key(request, publication_attempt_id)
    config_key = (
        kernel_config_object_key(request, publication_attempt_id)
        if request.kernel_config is not None
        else None
    )
    publication_principal = principal if request.visibility is ImageVisibility.PRIVATE else None
    row_id = await _adopt_or_insert_pending(
        conn,
        request,
        object_key,
        config_key,
        size_bytes,
        publication_attempt_id,
        publication_principal,
    )
    return PublishReservation(
        row_id=row_id,
        publication_attempt_id=publication_attempt_id,
        object_key=object_key,
        config_key=config_key,
        size_bytes=size_bytes,
        request=request,
    )


async def write_publish_object(
    store: ImageObjectStore, reservation: PublishReservation, source: Path
) -> bool:
    """Write the reserved row's qcow2 (and best-effort config sibling); return config presence.

    The second of the three steps, and the only one that touches the object store. It issues **no
    database statement at all**, so no transaction-scoped lock is needed across the PUT. The
    composed public and private publish paths still hold the IMAGE_PUBLISH session lock through
    this write and their committed finish (plus the private registration audit), while the PROJECT
    lock is absent (ADR-0520, ADR-0525). Verifies the source bytes against the row's declared
    digest, PUTs the qcow2, HEAD-gates it, then writes the config sibling best-effort.

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
    await _write_object(store, reservation, data)

    head = await asyncio.to_thread(store.head, reservation.object_key)
    if head is None:
        raise CategorizedError(
            "published image object is not present after write (HEAD gate failed)",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"object_key": reservation.object_key},
        )
    checksum = digest_sha256_b64(request.digest)
    if head.size_bytes != len(data) or head.checksum_sha256 != checksum:
        raise CategorizedError(
            "published image object does not match its declared size and checksum "
            "after write (HEAD gate failed)",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"object_key": reservation.object_key},
        )
    return await _write_config_best_effort(
        store,
        request,
        reservation.config_key,
        attempt_id=reservation.publication_attempt_id,
    )


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
    conn: AsyncConnection,
    store: ImageObjectStore,
    *,
    request: PublishRequest,
    source: Path,
    principal: str | None = None,
) -> ImageCatalogEntry:
    """Row-first two-write publish: pending row → object → HEAD-gate → ``registered``.

    Adopts the identity's existing ``defined``/``pending`` row (or inserts a ``pending`` row from
    ``request``), sets its ``object_key``, writes the object at ``source`` to the image prefix,
    HEAD-gates, then flips the row to ``registered`` and returns it. Public pending identity is
    ``(provider, name, arch)``. Private pending identity is ``(owner, provider, name)`` without
    arch, matching registered-name uniqueness; a cross-arch retry supersedes the earlier pending
    attempt and refreshes all request-owned fields. A seeded ``defined`` baseline remains
    arch-scoped and preserves its declared metadata when realized. Public and private rows, and
    private rows for different owners, intentionally do not adopt each other.

    Each reservation mints attempt-specific object keys. If overlapping reservations target the
    same pending identity, only the newest reservation can finish; an older attempt raises an
    actionless ``CONFLICT`` after its isolated object write rather than registering stale metadata.

    When ``request.kernel_config`` is present its attempt-specific config key is set on the
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
        principal: Required to reserve a private image; persisted only while the row is pending.

    Returns:
        The persisted ``registered`` :class:`ImageCatalogEntry`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``source`` bytes do not hash to
            ``request.digest`` (the catalog identity the materialization fetch verifies against);
            ``INFRASTRUCTURE_FAILURE`` if the object write or HEAD gate fails (the row stays
            ``pending`` for the reconciler to recover); ``CONFLICT`` if another reservation
            superseded this attempt or the reconciler reclaimed it.
    """
    stat = await asyncio.to_thread(source.stat)
    reservation = await reserve_publish(conn, request, size_bytes=stat.st_size, principal=principal)
    async with publication_fence(conn, reservation):
        config_written = await write_publish_object(store, reservation, source)
        async with conn.transaction():
            return await finish_publish(conn, reservation, config_written=config_written)
