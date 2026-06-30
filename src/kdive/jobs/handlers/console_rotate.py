"""Worker handler for the internal ``console_rotate`` job (local rotation, #892, ADR-0273).

Reads a running System's growing console log, rotates the new bytes into redacted
gzip-compressed part artifacts via the pure :func:`~kdive.providers.console_parts.rotation.rotate`
core, and persists the rotation cursor in the object-store sidecar. The read-sidecar -> seal
critical section runs under the per-System advisory lock (ADR-0095) so two rotations of one
System never interleave; the sidecar cursor is advanced only after the part rows commit so a
crash before that write replays the identical ``(gen, index)`` parts as insert-if-absent no-ops.
The handler is best-effort: a permission wall on the console log (a non-root worker, ADR-0223)
degrades to "register no parts" rather than failing the job, and a missing object store is a no-op.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
from collections.abc import Callable
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact, artifact_key
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS
from kdive.domain.capacity.state import SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.providers.console_parts.rotation import (
    RotationResult,
    SealedPart,
    part_object_name,
    rotate,
)
from kdive.providers.console_parts.sidecar import read_sidecar, write_sidecar
from kdive.providers.shared.runtime_paths import console_log_path, read_console_log
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore

_log = logging.getLogger(__name__)

# Local-libvirt parts and the sidecar share the tenant the per-Run console evidence uses
# (boot_evidence.py), so ``artifacts.get`` serves them from the same owner prefix.
_TENANT = "local"
_OWNER_KIND = "systems"
# Same retention class on both providers (remote sets it in console/wiring.py). No retention sweep
# reclaims system-owned console evidence (gc.py excludes console/vmcore, pins owner_kind='runs'), so
# console parts are bounded by teardown reclaim, not an expiry sweep.
_RETENTION_CLASS = "console"

# Seal parts only while the System is live (the sweep's predicate, console_rotation.py). A
# console_rotate job swept while the System was ``ready`` can run AFTER teardown has reclaimed the
# parts/sidecar and set the System terminal; without this guard it would re-seal gen-0 parts from
# the still-present console log (absent sidecar -> ZERO state) and orphan them past teardown. The
# guard and teardown both run under the per-System advisory lock, so the lock serializes the
# state-set against this state-read: whichever runs second sees the other's committed effect.
_LIVE_STATES: frozenset[SystemState] = frozenset({SystemState.READY, SystemState.CRASHED})

_PART_ROW_SQL: LiteralString = (
    "SELECT id FROM artifacts WHERE owner_kind = 'systems' AND owner_id = %s AND object_key = %s"
)

_SYSTEM_STATE_SQL: LiteralString = "SELECT state FROM systems WHERE id = %s"


def _make_redactor(secret_registry: SecretRegistry) -> Callable[[bytes], bytes]:
    """Wrap the text redactor into the ``bytes -> bytes`` redaction ``rotate`` expects.

    ``rotate`` redacts the whole pending region once before any part boundary, so decoding,
    redacting, and re-encoding the whole buffer keeps a boundary-straddling secret contiguous.
    """
    redactor = Redactor(registry=secret_registry)

    def _redact(buffer: bytes) -> bytes:
        return redactor.redact_text(buffer.decode("utf-8", "replace")).encode("utf-8")

    return _redact


async def _system_is_live(conn: AsyncConnection, system_id: UUID) -> bool:
    """True when the System is in a live state the sweep targets (``ready``/``crashed``).

    A missing row (the System was deleted) is not live. Read under the per-System advisory lock so
    it serializes against teardown's terminal-state write.
    """
    async with conn.cursor() as cur:
        await cur.execute(_SYSTEM_STATE_SQL, (system_id,))
        row = await cur.fetchone()
    return row is not None and SystemState(row[0]) in _LIVE_STATES


async def _existing_part_row(conn: AsyncConnection, system_id: UUID, object_key: str) -> bool:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_PART_ROW_SQL, (system_id, object_key))
        return await cur.fetchone() is not None


def _put_part(store: ObjectStore, system_id: UUID, part: SealedPart) -> StoredArtifact:
    return store.put_artifact(
        ArtifactWriteRequest(
            tenant=_TENANT,
            owner_kind=_OWNER_KIND,
            owner_id=str(system_id),
            name=part_object_name(part.gen, part.index),
            data=gzip.compress(part.redacted),
            sensitivity=Sensitivity.REDACTED,
            retention_class=_RETENTION_CLASS,
            content_encoding="gzip",
        )
    )


async def _seal_part(
    conn: AsyncConnection, store: ObjectStore, system_id: UUID, part: SealedPart
) -> None:
    """Store one part's gzipped object and register its row, idempotent on the object key."""
    object_key = artifact_key(
        _TENANT, _OWNER_KIND, str(system_id), part_object_name(part.gen, part.index)
    )
    if await _existing_part_row(conn, system_id, object_key):
        return
    stored = await asyncio.to_thread(_put_part, store, system_id, part)
    await ARTIFACTS.insert(
        conn, register_artifact_row(stored, owner_kind=_OWNER_KIND, owner_id=system_id)
    )


async def _rotate_under_lock(
    conn: AsyncConnection,
    store: ObjectStore,
    system_id: UUID,
    boot_id: str,
    redact: Callable[[bytes], bytes],
) -> RotationResult | None:
    """Read the cursor, seal new parts, and return the advanced state — all under the lock.

    Returns ``None`` (sealing nothing) when the System is no longer live (teardown reclaimed it,
    the race guard above) or the console log cannot be read (ADR-0223): the permission wall is a
    host-config problem, not a job failure, so the handler degrades.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        if not await _system_is_live(conn, system_id):
            _log.info(
                "system %s is no longer live; sealing no console parts (teardown race guard)",
                system_id,
            )
            return None
        try:
            file_bytes = await asyncio.to_thread(read_console_log, console_log_path(system_id))
        except CategorizedError:
            _log.warning(
                "console log for system %s is unreadable; registering no console parts",
                system_id,
                exc_info=True,
            )
            return None
        state = await asyncio.to_thread(read_sidecar, store, _TENANT, system_id)
        result = rotate(state, file_bytes, boot_id, redact)
        for part in result.parts:
            await _seal_part(conn, store, system_id, part)
        return result


async def console_rotate_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
) -> str | None:
    """Rotate a System's growing console into redacted gzip part artifacts (best-effort).

    Seals parts under the per-System lock, then advances the sidecar cursor after the part rows
    commit. A console log the worker cannot read degrades to "register no parts"; a missing object
    store is a no-op.
    """
    if artifact_store is None:
        _log.warning("object storage is not configured; skipping console rotation")
        return None
    system_id = UUID(job.payload["system_id"])
    boot_id = job.payload.get("boot_id", "")
    result = await _rotate_under_lock(
        conn, artifact_store, system_id, boot_id, _make_redactor(secret_registry)
    )
    if result is None:
        return None
    await asyncio.to_thread(write_sidecar, artifact_store, _TENANT, system_id, result.next_state)
    return str(system_id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
) -> None:
    """Bind the ``console_rotate`` job handler with its redaction and object-store deps."""
    registry.register(
        JobKind.CONSOLE_ROTATE,
        lambda conn, job: console_rotate_handler(
            conn, job, secret_registry=secret_registry, artifact_store=artifact_store
        ),
    )
