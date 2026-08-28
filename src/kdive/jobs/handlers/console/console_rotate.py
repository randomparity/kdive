"""Worker handler for the internal ``console_rotate`` job (local rotation, #892, ADR-0273).

Reads a running System's growing console log, rotates the new bytes into redacted
gzip-compressed part artifacts via the pure :func:`~kdive.artifacts.console.rotation.rotate`
core, and persists the rotation cursor in the object-store sidecar. The rotation is planned under
the per-System advisory lock (ADR-0095) — cursor read, part derivation, insert-if-absent probe —
and the part rows are registered under it again, but the part **objects** are PUT between those
two locked phases rather than inside one (ADR-0519), because a rotation seals an unbounded number
of parts and ``SYSTEM`` is the scope teardown, boot and revert also serialize on. The sidecar
cursor is advanced only after the part rows commit so a
crash before that write replays the identical ``(gen, index)`` parts as insert-if-absent no-ops.
The handler is best-effort: a permission wall on the console log (a non-root worker, ADR-0223)
degrades to "register no parts" rather than failing the job, and a missing object store is a no-op.

Each sealed part is stamped with the System's most-recently-booted Run as a correlation attribute
(ADR-0279), resolved once per job under the same per-System lock; ownership stays System-owned.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
from collections.abc import Callable
from typing import LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.catalog.discard import discard_unregistered_objects
from kdive.artifacts.catalog.etag_repair import reconcile_row_etag
from kdive.artifacts.catalog.registration import register_artifact_row
from kdive.artifacts.console.rotation import (
    RotationResult,
    SealedPart,
    part_object_name,
    rotate,
)
from kdive.artifacts.console.sidecar import read_sidecar, write_sidecar
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact, artifact_key
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, ArtifactClaimConflict
from kdive.domain.capacity.state import SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ConsoleRotatePayload, load_payload
from kdive.providers.shared.runtime_paths import console_log_path, read_console_log
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs.steps import latest_booted_run_id
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
# state-set against this state-read: whichever runs second sees the other's committed effect. The
# guard is evaluated twice — once when the rotation is planned and again when its rows are
# registered — because the part objects are PUT between those phases (ADR-0519). A teardown that
# lands in that window fails the second evaluation, and the objects already written are deleted
# rather than left behind a row-driven reclaim that has already passed them.
_LIVE_STATES: frozenset[SystemState] = frozenset(
    {
        SystemState.READY,
        SystemState.RESTORING,  # keep sealing the console across a revert (ADR-0378)
        SystemState.PAUSED,  # keep sealing while the guest is suspended (ADR-0378)
        SystemState.CRASHING,
        SystemState.CRASHED,
    }
)

_PART_ROW_SQL: LiteralString = (
    "SELECT id, etag FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND object_key = %s"
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


class _ExistingRow(NamedTuple):
    """A committed part row for this object key: its id, and the object etag it describes."""

    id: UUID
    etag: str


async def _existing_part_row(
    conn: AsyncConnection, system_id: UUID, object_key: str
) -> _ExistingRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_PART_ROW_SQL, (system_id, object_key))
        row = await cur.fetchone()
    return None if row is None else _ExistingRow(row["id"], str(row["etag"]))


async def _key_unregistered(conn: AsyncConnection, system_id: UUID, object_key: str) -> bool:
    """Whether no committed row claims ``object_key`` — the discard's row fence, run unlocked."""
    return await _existing_part_row(conn, system_id, object_key) is None


def _put_part(store: ObjectStore, system_id: UUID, part: SealedPart) -> StoredArtifact:
    return store.put_artifact(
        ArtifactWriteRequest(
            tenant=_TENANT,
            owner_kind=_OWNER_KIND,
            owner_id=str(system_id),
            name=part_object_name(part.gen, part.index),
            # mtime=0 is pinned rather than assumed: a re-derived part must compress to the same
            # bytes, or its etag stops being a stable identity for the ``(gen, index)`` key that
            # insert-if-absent is built on. CPython's default for this argument was the current
            # time before 3.13 and is 0 from 3.13 on, so the value this repository depends on has
            # already moved once; stating it keeps the invariant ours rather than the runtime's.
            data=gzip.compress(part.redacted, mtime=0),
            sensitivity=Sensitivity.REDACTED,
            retention_class=_RETENTION_CLASS,
            content_encoding="gzip",
        )
    )


class _Plan(NamedTuple):
    """A planned rotation: the advanced state, the parts still unsealed, and their attribution."""

    result: RotationResult
    pending: tuple[SealedPart, ...]
    run_id: UUID | None


def _part_key(system_id: UUID, part: SealedPart) -> str:
    """The object key a sealed part is stored under (its insert-if-absent identity)."""
    return artifact_key(
        _TENANT, _OWNER_KIND, str(system_id), part_object_name(part.gen, part.index)
    )


async def _plan_rotation(
    conn: AsyncConnection,
    store: ObjectStore,
    system_id: UUID,
    boot_id: str,
    redact: Callable[[bytes], bytes],
) -> _Plan | None:
    """Under the per-System lock: guard, read the cursor, and decide which parts to seal.

    Writes nothing. The part objects are PUT by :func:`_seal_pending` after this lock is
    released, because a rotation seals an unbounded number of parts and holding the contended
    ``SYSTEM`` scope across that many object-store round-trips bounds every teardown, boot and
    revert on this System by the store's latency (ADR-0519).

    The sidecar GET stays under the lock **deliberately**. It is one small bounded read, and it
    is the rotation cursor: moving it out would widen the window in which a peer rotation reads
    the same cursor and re-derives the same ``(gen, index)`` parts. Insert-if-absent on the part
    object key makes such a re-derivation harmless, but not free — it costs a duplicate PUT of
    identical bytes — so ADR-0519 trades the unbounded PUT loop for a bounded GET, not both.

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
        run_id = await _resolve_run_id(conn, system_id)
        state = await asyncio.to_thread(read_sidecar, store, _TENANT, system_id)
        result = rotate(state, file_bytes, boot_id, redact)
        pending = []
        for part in result.parts:
            if await _existing_part_row(conn, system_id, _part_key(system_id, part)) is None:
                pending.append(part)
        return _Plan(result=result, pending=tuple(pending), run_id=run_id)


async def _seal_pending(
    conn: AsyncConnection, store: ObjectStore, system_id: UUID, plan: _Plan
) -> bool:
    """PUT the planned part objects lock-free, then register their rows under one short lock.

    Returns ``False`` when the locked re-verify finds the System no longer live — teardown ran
    while the objects were in flight, so its row-driven reclaim has already passed them by and
    nothing else would ever reach them. Every object this attempt wrote is discarded in that
    case. The ordering that makes this safe is teardown's own: ``jobs/handlers/systems.py``
    writes ``TORN_DOWN`` **under this same SYSTEM lock** and runs ``_reclaim_console_artifacts``
    only afterwards, so a peer rotation's row can only have committed before that write and is
    therefore covered by the reclaim. The discard still re-probes each row and compares each
    etag immediately before deleting, so the delete cannot destroy an object a peer's row owns.

    ``plan.run_id`` is the System's most-recently-booted Run (ADR-0279), stamped as a correlation
    attribute; ownership stays ``owner_kind='systems'``. ``None`` leaves the part uncorrelated.
    """
    if not plan.pending:
        return True
    stored = [await asyncio.to_thread(_put_part, store, system_id, part) for part in plan.pending]
    claimed: list[tuple[_ExistingRow, str]] = []
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            live = await _system_is_live(conn, system_id)
            if live:
                for obj in stored:
                    # Re-check under the lock: another rotation may have sealed this exact
                    # ``(gen, index)`` while the object was in flight, and its row owns the key.
                    existing = await _existing_part_row(conn, system_id, obj.key)
                    if existing is not None:
                        claimed.append((existing, obj.key))
                        continue
                    row, inserted = await ARTIFACTS.claim(
                        conn,
                        register_artifact_row(
                            obj, owner_kind=_OWNER_KIND, owner_id=system_id, run_id=plan.run_id
                        ),
                    )
                    if not inserted:
                        claimed.append((_ExistingRow(row.id, row.etag), obj.key))
    except ArtifactClaimConflict:
        await discard_unregistered_objects(
            store,
            stored,
            still_unregistered=lambda key: _key_unregistered(conn, system_id, key),
        )
        raise
    # A peer rotation owns these keys, and this attempt's PUT overwrote the objects its rows
    # describe. Re-point each row at what its object actually holds — by stat, not by assuming
    # this attempt's etag, since landing last in the store and last at the lock are independent.
    # Outside the lock: each stat is a store round-trip, and there is one per claimed part.
    for row, key in claimed:
        await reconcile_row_etag(conn, store, row_id=row.id, object_key=key, row_etag=row.etag)
    if not live:
        _log.info(
            "system %s stopped being live while %d console part(s) were in flight; discarding "
            "them unregistered (teardown race guard)",
            system_id,
            len(stored),
        )
        await discard_unregistered_objects(
            store, stored, still_unregistered=lambda key: _key_unregistered(conn, system_id, key)
        )
    return live


async def _resolve_run_id(conn: AsyncConnection, system_id: UUID) -> UUID | None:
    """Resolve the System's most-recently-booted Run for part attribution (ADR-0279, #935).

    Best-effort: a resolution failure logs once and degrades to ``None`` (uncorrelated parts) so a
    transient query error never fails the rotation job or stalls the sidecar — capture stays
    best-effort (ADR-0273). Resolved once per job under the per-System lock the caller holds, so the
    boot it attributes to does not move while the job's parts are sealed.
    """
    try:
        return await latest_booted_run_id(conn, system_id)
    except Exception:
        _log.warning(
            "resolving the booted Run for system %s failed; sealing parts uncorrelated",
            system_id,
            exc_info=True,
        )
        return None


async def console_rotate_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore,
) -> str | None:
    """Rotate a System's growing console into redacted gzip part artifacts (best-effort).

    Plans the rotation under the per-System lock, PUTs the part objects lock-free, registers
    their rows under the lock again (ADR-0519), then advances the sidecar cursor after the part
    rows commit. A console log the worker cannot read degrades to "register no parts", and a
    System that stops being live at either locked phase seals nothing and leaves the cursor
    where it was — so the next rotation re-derives the same parts rather than skipping them.
    """
    payload = load_payload(job, ConsoleRotatePayload)
    system_id = UUID(payload.system_id)
    boot_id = payload.boot_id
    plan = await _plan_rotation(
        conn, artifact_store, system_id, boot_id, _make_redactor(secret_registry)
    )
    if plan is None:
        return None
    if not await _seal_pending(conn, artifact_store, system_id, plan):
        return None
    await asyncio.to_thread(
        write_sidecar, artifact_store, _TENANT, system_id, plan.result.next_state
    )
    return str(system_id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore,
) -> None:
    """Bind the ``console_rotate`` job handler with its redaction and object-store deps."""
    registry.register(
        JobKind.CONSOLE_ROTATE,
        lambda conn, job: console_rotate_handler(
            conn, job, secret_registry=secret_registry, artifact_store=artifact_store
        ),
    )
