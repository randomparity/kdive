"""Project-private upload registration (ADR-0093, ADR-0516, ADR-0520, issue #286).

A developer uploads a custom rootfs through the ADR-0048 ingest, which lands the bytes as a
*quarantined* object (its guest contract unverified, its scope not yet owner-bound).
``register_private_upload`` turns that quarantined object into a bootable project-private catalog
image:

1. Read the quarantined object's bytes (its size and content digest).
2. Validate the image's guest contract; a non-conforming image is rejected *while still
   quarantined* (never registered, never promoted out of the quarantine prefix).
3. Under the project advisory lock, enforce the per-project count/bytes quota fail-closed and
   commit a ``pending`` row reserving this upload's bytes. A denial is audited and raises before
   any write.
4. Release the lock, then write the object and flip the row to ``registered`` — the shared
   row-first publish steps with ``visibility='private'`` and ``owner=project``, no second
   implementation.

The lock is held across the reservation only, never across the object-store write (ADR-0520): the
committed ``pending`` row *is* the quota claim, so a concurrent upload sees it in the aggregate
without the lock spanning a multi-GiB PUT.

The owner of the registered image is the **project**; the uploading ``principal`` is recorded only
for audit attribution.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from psycopg import AsyncConnection
from psycopg.rows import dict_row

import kdive.config as config
from kdive.artifacts import storage as artifact_types
from kdive.artifacts.storage import validate_key_component
from kdive.config.core_settings import (
    IMAGE_PRIVATE_LIFETIME_MAX,
    IMAGE_PRIVATE_MAX_BYTES,
    IMAGE_PRIVATE_MAX_COUNT,
)
from kdive.db.locks import LockScope, advisory_xact_lock, require_top_level_transaction
from kdive.domain.catalog.image_format import ImageFormat
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.cataloging.validation import DEFAULT_INSPECT, InspectSeam, validate_guest_contract
from kdive.security import audit
from kdive.services.images.publish import (
    ImageObjectStore,
    PublishRequest,
    PublishReservation,
    finish_publish,
    reserve_publish,
    write_publish_object,
)

_log = logging.getLogger(__name__)

_UPLOAD_TOOL = "images.upload"
_OBJECT_KIND = "image_catalog"
_QCOW2_FORMAT: ImageFormat = "qcow2"
_ROOT_DEVICE = "/dev/vda"

# A project's live private images are the ones that occupy quota: a registered row, or a publish
# still in flight (`pending`). A `defined` baseline is public/object-less, so it never counts.
_LIVE_PRIVATE_STATES = (ImageState.PENDING.value, ImageState.REGISTERED.value)


class UploadObjectStore(ImageObjectStore, Protocol):
    """The object-store capability the upload path needs: publish's write/HEAD plus a read.

    Extends :class:`~kdive.services.images.publish.ImageObjectStore` (``put_artifact``/``head``)
    with the ``get_artifact`` the upload path uses to read the quarantined bytes. The concrete
    :class:`~kdive.store.objectstore.ObjectStore` satisfies it.
    """

    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact: ...


@dataclass(frozen=True, slots=True)
class PrivateUploadRequest:
    """Project-private image upload inputs before validation and publish."""

    project: str
    principal: str
    name: str
    provider: str
    arch: str
    quarantine_key: str
    expires_at: datetime
    required: tuple[str, ...]


def _clamp_expiry(expires_at: datetime, *, now: datetime) -> datetime:
    max_seconds = config.require(IMAGE_PRIVATE_LIFETIME_MAX)
    ceiling = now + timedelta(seconds=max_seconds)
    return min(expires_at, ceiling)


async def _project_usage(conn: AsyncConnection, project: str) -> tuple[int, int]:
    """Return the project's live private image count and reserved bytes, under the held lock.

    One aggregate over the ``pending`` + ``registered`` private rows owned by ``project``: their
    count, and the sum of the ``size_bytes`` each recorded when its row was reserved (ADR-0520).
    No object-store round trip — the earlier implementation HEADed every row's object, which cost
    one network call per image and had to happen inside the lock. Reading committed state instead
    is what lets the lock span the reservation alone.

    A ``pending`` row counts its full reserved size even though its object is not written yet;
    that is the point of the reservation. An abandoned one is released by the reconciler's
    ``repair_dangling_images`` on its ``pending_since`` deadline.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT count(*) AS live_count, COALESCE(SUM(size_bytes), 0) AS used_bytes "
            "FROM image_catalog "
            "WHERE visibility = %(private)s AND owner = %(owner)s "
            "AND state = ANY(%(states)s)",
            {
                "private": ImageVisibility.PRIVATE.value,
                "owner": project,
                "states": list(_LIVE_PRIVATE_STATES),
            },
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: an aggregate with no GROUP BY always yields exactly one row.
        raise RuntimeError(f"project usage aggregate returned no row for project {project!r}")
    return int(row["live_count"]), int(row["used_bytes"])


def _quota_denial(
    *, project: str, count: int, used_bytes: int, new_bytes: int
) -> CategorizedError | None:
    """Return the fail-closed quota denial for this upload, or ``None`` if it fits.

    Pure decision: the count cap admits one more row; the bytes cap admits ``new_bytes`` on top of
    the current total. No durable write — the caller audits the denial on a committed connection.
    """
    max_count = config.require(IMAGE_PRIVATE_MAX_COUNT)
    max_bytes = config.require(IMAGE_PRIVATE_MAX_BYTES)
    if count + 1 > max_count:
        return CategorizedError(
            f"project {project!r} is at its private-image count cap",
            category=ErrorCategory.QUOTA_EXCEEDED,
            details={"used": count, "cap": max_count},
        )
    if used_bytes + new_bytes > max_bytes:
        return CategorizedError(
            f"project {project!r} would exceed its private-image bytes cap",
            category=ErrorCategory.QUOTA_EXCEEDED,
            details={"used_bytes": used_bytes, "new_bytes": new_bytes, "cap_bytes": max_bytes},
        )
    return None


async def _audit_denial(
    conn: AsyncConnection, *, project: str, principal: str, name: str, denial: CategorizedError
) -> None:
    """Append the fail-closed quota-denial audit row on its own committed connection.

    Object-agnostic (no image was created), so it reuses :func:`audit.record_denial`'s reserved
    bare ``denied`` transition. Runs in its own transaction so the denial is durably audited even
    though the locked transaction that detected it rolled back without a write.
    """
    async with conn.transaction():
        await audit.record_denial(
            conn,
            event=audit.DenialEvent(
                principal=principal,
                agent_session=None,
                project=project,
                tool=_UPLOAD_TOOL,
                args={"provider": "local-libvirt", "name": name, "visibility": "private"},
                reason=str(denial),
            ),
        )


def _validate_staged(source: Path, required: Sequence[str], inspect: InspectSeam) -> None:
    validate_guest_contract(source, required=required, inspect=inspect)


async def _reject_oversize_upload(store: UploadObjectStore, quarantine_key: str) -> None:
    """Reject an upload whose object already exceeds the per-project bytes cap before buffering.

    HEADs the quarantined object (cheap — no body read) and rejects a single object larger than
    ``IMAGE_PRIVATE_MAX_BYTES`` up front, so the service never reads a multi-GiB body into memory
    only to deny it under the lock. The authoritative cap (current usage + this upload) is still
    enforced under the project lock; this is the pre-buffer DoS bound, not a replacement for it.

    Raises:
        CategorizedError: ``QUOTA_EXCEEDED`` if the object alone exceeds the per-project bytes cap;
            ``STALE_HANDLE`` if the quarantined object is gone.
    """
    head = await asyncio.to_thread(store.head, quarantine_key)
    if head is None:
        raise CategorizedError(
            f"quarantined upload object {quarantine_key!r} is gone",
            category=ErrorCategory.STALE_HANDLE,
            details={"key": quarantine_key},
        )
    max_bytes = config.require(IMAGE_PRIVATE_MAX_BYTES)
    if head.size_bytes > max_bytes:
        raise CategorizedError(
            "uploaded image exceeds the per-project private-image bytes cap",
            category=ErrorCategory.QUOTA_EXCEEDED,
            details={"size_bytes": head.size_bytes, "cap_bytes": max_bytes},
        )


async def register_private_upload(
    conn: AsyncConnection,
    store: UploadObjectStore,
    *,
    request: PrivateUploadRequest,
    inspect: InspectSeam = DEFAULT_INSPECT,
) -> ImageCatalogEntry:
    """Register a quarantined upload as a project-private catalog image, quota-reserved.

    Takes the PROJECT advisory lock across the quota check and the ``pending``-row reservation
    only, then publishes with the lock released (ADR-0520). The cap stays fail-closed because the
    reservation commits inside the lock: two concurrent uploads cannot both pass it, since the
    second one's usage read sees the first one's committed claim. The quarantined object is
    validated against the guest contract *before* any reservation or write, so a non-conforming
    image is rejected while still quarantined (never registered). The durable writes go through
    the shared publish steps (``visibility='private'``, ``owner=project``); the uploading
    ``principal`` is recorded only for audit attribution.

    Args:
        conn: An async Postgres connection with **no transaction open** — this function opens its
            own to commit the reservation and release the project lock with it, and a savepoint
            would do neither. An autocommit connection, or a freshly checked-out pooled one on
            which nothing has run yet, satisfies this.
        store: The object store holding the quarantined object and receiving the published image.
        request: The upload identity, owning project, source key, expiry, and required guest
            contract tags.
        inspect: The libguestfs inspection seam (defaults to a real ``guestfish`` probe; tests
            inject a stub).

    Returns:
        The persisted ``registered`` project-private :class:`ImageCatalogEntry`.

    Raises:
        CategorizedError: ``QUOTA_EXCEEDED`` (audited) if a cap would be breached;
            ``CONFIGURATION_ERROR`` if the image fails its guest contract or its bytes do not hash
            to the computed digest; ``STALE_HANDLE``/``INFRASTRUCTURE_FAILURE`` from the store.
        RuntimeError: ``conn`` already has a transaction open when the reservation is reached.
    """
    # Validate the identity components before any filesystem or object-key use: `arch`/`name`/
    # `provider` are folded into the staged temp path and the object key, so a `/`-bearing value
    # could otherwise traverse out of the temp directory. The publish re-validates at key
    # construction, but the staged write happens first, so the guard belongs here.
    for label, value in (
        ("provider", request.provider),
        ("name", request.name),
        ("arch", request.arch),
        ("owner", request.project),
    ):
        validate_key_component(label, value)

    await _reject_oversize_upload(store, request.quarantine_key)
    fetched = await asyncio.to_thread(store.get_artifact, request.quarantine_key, None)
    data = fetched.data
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    now = datetime.now(UTC)
    clamped_expiry = _clamp_expiry(request.expires_at, now=now)

    publish_request = PublishRequest(
        provider=request.provider,
        name=request.name,
        arch=request.arch,
        format=_QCOW2_FORMAT,
        root_device=_ROOT_DEVICE,
        digest=digest,
        capabilities=request.required,
        provenance={
            "upload": {"principal": request.principal, "quarantine_key": request.quarantine_key}
        },
        visibility=ImageVisibility.PRIVATE,
        owner=request.project,
        expires_at=clamped_expiry,
    )

    with tempfile.TemporaryDirectory(prefix="kdive-upload-") as workdir:
        source = Path(workdir) / f"{request.arch}.qcow2"
        await asyncio.to_thread(source.write_bytes, data)
        await asyncio.to_thread(_validate_staged, source, request.required, inspect)

        entry = await _publish_under_quota(
            conn,
            store,
            request=publish_request,
            source=source,
            principal=request.principal,
            new_bytes=len(data),
        )
    return entry


async def _reserve_under_quota(
    conn: AsyncConnection,
    *,
    request: PublishRequest,
    project: str,
    principal: str,
    new_bytes: int,
) -> PublishReservation:
    """Enforce the quota fail-closed under the PROJECT lock and return the committed reservation.

    The lock spans exactly this: one aggregate read of the project's live private rows, the cap
    decision, and the ``pending`` row that claims ``new_bytes``. It holds no object-store call and
    no unbounded loop, and it is released by the ``return`` committing the transaction — which is
    what lets the caller run the PUT unlocked (ADR-0520).

    The transaction must be a real one and not a savepoint: releasing a savepoint commits neither
    the reservation nor the lock, so the claim would be invisible to a concurrent upload *and* the
    PROJECT lock would still be held across the PUT — the exact span this shortens. Every caller
    supplies a statement-free connection today, but that is a property of the callers, so it is
    asserted (ADR-0516 §1, ADR-0506).

    Raises:
        CategorizedError: ``QUOTA_EXCEEDED``. The denial reserves nothing and the locked
            transaction rolls back having written nothing, so it is audited durably on a fresh
            transaction before raising — an over-cap upload is both denied and audited.
    """
    require_top_level_transaction(conn, "the private-upload quota reservation")
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.PROJECT, project):
        count, used_bytes = await _project_usage(conn, project)
        denial = _quota_denial(
            project=project, count=count, used_bytes=used_bytes, new_bytes=new_bytes
        )
        if denial is None:
            return await reserve_publish(conn, request, size_bytes=new_bytes)
    await _audit_denial(
        conn, project=project, principal=principal, name=request.name, denial=denial
    )
    raise denial


async def _publish_under_quota(
    conn: AsyncConnection,
    store: UploadObjectStore,
    *,
    request: PublishRequest,
    source: Path,
    principal: str,
    new_bytes: int,
) -> ImageCatalogEntry:
    """Reserve the quota under the PROJECT lock, then publish with the lock released.

    The three phases are deliberately separate transactions (ADR-0520): the locked reservation,
    the object write with nothing held, and the registration flip composed with its audit row. The
    cap stays fail-closed because the reservation commits inside the lock — a concurrent upload's
    aggregate read sees the claim — rather than because the lock spans the multi-GiB PUT.

    A PUT that fails or a worker that dies after the reservation commits leaves a ``pending`` row
    holding its bytes; the reconciler's ``repair_dangling_images`` releases it once its
    ``pending_since + KDIVE_IMAGE_PUBLISH_GRACE_SECONDS`` deadline elapses, which is ADR-0092's
    recovery path rather than a rollback this function would have to duplicate.
    """
    project = request.owner
    if project is None:  # Invariant: this path always sets owner to the project.
        raise RuntimeError("private upload has no owning project")
    reservation = await _reserve_under_quota(
        conn, request=request, project=project, principal=principal, new_bytes=new_bytes
    )
    try:
        config_written = await write_publish_object(store, reservation, source)
    except Exception:
        # Re-raised unchanged — this only records that the committed reservation outlived the
        # write that was supposed to consume it. Without the line, the project silently carries
        # `new_bytes` against its cap until the reconciler reaps the row, and an operator chasing
        # a spurious QUOTA_EXCEEDED has no trail until that removal logs an hour later.
        _log.warning(
            "private-upload reservation %s abandoned by a failed write: project %s holds "
            "%d byte(s) against its cap until the publish deadline reaps the pending row",
            reservation.row_id,
            project,
            new_bytes,
            exc_info=True,
        )
        raise
    # The flip and its audit row share one transaction so a registered image is never unaudited;
    # `audit.record_system` opens none of its own, by contract, for exactly this composition.
    async with conn.transaction():
        entry = await finish_publish(conn, reservation, config_written=config_written)
        await _audit_registration(conn, entry, principal=principal)
    return entry


async def _audit_registration(
    conn: AsyncConnection, entry: ImageCatalogEntry, *, principal: str
) -> None:
    if entry.owner is None:  # Invariant: a private image always carries its owning project.
        raise RuntimeError("registered private image has no owner project to audit under")
    await audit.record_system(
        conn,
        principal=principal,
        event=audit.AuditEvent(
            tool=_UPLOAD_TOOL,
            object_kind=_OBJECT_KIND,
            object_id=entry.id,
            transition="private-upload:registered",
            args={"provider": entry.provider, "name": entry.name, "arch": entry.arch},
            project=entry.owner,
        ),
    )
