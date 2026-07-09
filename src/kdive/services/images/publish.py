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
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.image_format import ImageFormat
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)

_RETENTION_CLASS = "image"


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


def _object_owner_kind(request: PublishRequest) -> str:
    """The ``owner_kind`` key segment, owner-scoped for a private image.

    A public image keys its provider directly (``{provider}``); a private image folds the owning
    project into the segment (``{provider}__{owner}``) so two projects' private images of the same
    ``(provider, name, arch)`` never collide on one object. The ``__`` separator is illegal in a
    provider/project name, so the segment stays unambiguous and slash-free (``artifact_key``
    rejects slashes in a component).
    """
    if request.visibility is ImageVisibility.PRIVATE and request.owner is not None:
        return f"{request.provider}__{request.owner}"
    return request.provider


def _image_write_request(
    request: PublishRequest, data: bytes
) -> artifact_types.ArtifactWriteRequest:
    return artifact_types.ArtifactWriteRequest(
        tenant="images",
        owner_kind=_object_owner_kind(request),
        owner_id=request.name,
        name=f"{request.arch}.qcow2",
        data=data,
        sensitivity=Sensitivity.REDACTED,
        retention_class=_RETENTION_CLASS,
    )


def image_object_key(request: PublishRequest) -> str:
    """The object-store key for a catalog image, scoped to its visibility and owner.

    A public image lives under ``images/{provider}/{name}/{arch}.qcow2``; a private image is
    **owner-scoped** (``images/{provider}__{owner}/{name}/{arch}.qcow2``) so two projects' private
    images of the same identity never collide on one object. The key is persisted on the row, and
    the materialization fetch reads it from the row (it never recomputes the key), so the scheme is
    free to encode owner without a fetch-side change.
    """
    return _image_write_request(request, b"").key()


def _config_write_request(
    request: PublishRequest, data: bytes
) -> artifact_types.ArtifactWriteRequest:
    return artifact_types.ArtifactWriteRequest(
        tenant="images",
        owner_kind=_object_owner_kind(request),
        owner_id=request.name,
        name=f"{request.arch}.config",
        data=data,
        sensitivity=Sensitivity.REDACTED,
        retention_class=_RETENTION_CLASS,
    )


def kernel_config_object_key(request: PublishRequest) -> str:
    """The object-store key for the image's ``/boot/config-<ver>`` sibling of the qcow2 (ADR-0317).

    Same tenant/owner scoping as :func:`image_object_key`; the ``.config`` suffix distinguishes it
    from the ``{arch}.qcow2`` object. Persisted on the row's ``kernel_config_key`` when a config is
    offered, ``None`` otherwise.
    """
    return _config_write_request(request, b"").key()


async def _adopt_or_insert_pending(
    conn: AsyncConnection, request: PublishRequest, object_key: str, config_key: str | None
) -> UUID:
    """Adopt this scope's existing non-registered row, or insert a fresh ``pending`` row.

    Runs in one transaction so concurrent re-runs of the same image serialize on the adopted row.
    The match is scoped by ``(provider, name, arch, visibility, owner)`` — a public publish never
    adopts a project's private row and one project never adopts another's, so cross-tenant
    isolation holds (the private uniqueness key is ``(owner, provider, name)``). A ``defined``
    baseline and a crashed ``pending`` attempt are both adopted in place and moved to ``pending``
    with ``object_key`` set and ``pending_since`` re-armed; resolution never returns either, so an
    adopted row is never visible mid-publish.
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
                "SET state = %s, object_key = %s, kernel_config_key = %s, pending_since = now() "
                "WHERE id = %s",
                (ImageState.PENDING.value, object_key, config_key, existing["id"]),
            )
            return existing["id"]
        return await _insert_pending(cur, request, object_key, config_key)


async def _insert_pending(
    cur: AsyncCursor[DictRow], request: PublishRequest, object_key: str, config_key: str | None
) -> UUID:
    """Insert a fresh ``pending`` row from ``request`` and return its id.

    ``cur`` is a ``dict_row`` cursor already inside the adopt transaction.
    """
    insert_q = (
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, kernel_config_key, digest, "
        " capabilities, provenance, visibility, owner, expires_at, state, pending_since) "
        "VALUES (%(provider)s, %(name)s, %(arch)s, %(format)s, %(root_device)s, %(object_key)s, "
        " %(kernel_config_key)s, %(digest)s, %(capabilities)s, %(provenance)s, %(visibility)s, "
        " %(owner)s, %(expires_at)s, %(state)s, now()) RETURNING id"
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
    await asyncio.to_thread(store.put_artifact, _image_write_request(request, data))


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
    write = _config_write_request(request, request.kernel_config)
    try:
        await asyncio.to_thread(store.put_artifact, write)
        head = await asyncio.to_thread(store.head, config_key)
    except CategorizedError:
        _log.warning("image kernel-config write failed; registering with no config offered")
        return False
    if head is None:
        _log.warning("image kernel-config object absent after write; no config offered")
        return False
    return True


async def _registered(
    conn: AsyncConnection, row_id: UUID, *, clear_config_key: bool = False
) -> ImageCatalogEntry:
    async with conn.cursor(row_factory=dict_row) as cur:
        if clear_config_key:
            await cur.execute(
                "UPDATE image_catalog SET state = %s, kernel_config_key = NULL "
                "WHERE id = %s RETURNING *",
                (ImageState.REGISTERED.value, row_id),
            )
        else:
            await cur.execute(
                "UPDATE image_catalog SET state = %s WHERE id = %s RETURNING *",
                (ImageState.REGISTERED.value, row_id),
            )
        row = await cur.fetchone()
    if row is None:  # Invariant: the row was just written as pending.
        raise RuntimeError(f"image_catalog row {row_id} vanished before registration")
    return ImageCatalogEntry.model_validate(row)


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
    object_key = image_object_key(request)
    config_key = kernel_config_object_key(request) if request.kernel_config is not None else None
    row_id = await _adopt_or_insert_pending(conn, request, object_key, config_key)

    data = await asyncio.to_thread(source.read_bytes)
    _verify_source_digest(data, request.digest)
    await _write_object(store, request, data)

    head = await asyncio.to_thread(store.head, object_key)
    if head is None:
        raise CategorizedError(
            "published image object is not present after write (HEAD gate failed)",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"object_key": object_key},
        )
    config_written = await _write_config_best_effort(store, request, config_key)
    return await _registered(
        conn, row_id, clear_config_key=config_key is not None and not config_written
    )
