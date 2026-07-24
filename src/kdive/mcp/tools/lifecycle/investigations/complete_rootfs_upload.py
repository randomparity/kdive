"""Finalize an investigation-scoped uploaded rootfs (ADR-0441, #1502).

The explicit commit symmetric with ``runs.complete_build``: it verifies the PUT object,
writes the write-once ``owner_kind='investigations'`` ``artifacts`` row every referencing System
resolves against, and deletes the upload manifest. It runs under the investigation lock so it
serializes with ``close_investigation`` — a finalize that loses the race to a close sees the
terminal state and is rejected, leaving the manifest for the reaper.

It also enforces the manifest deadline the mint advertised (ADR-0444): a finalize past the
window is rejected with a re-mint pointer rather than silently accepted, which is what lets the
reaper collect a lapsed window on any investigation state without racing a legitimate finalize.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import HeadResult, artifact_key
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import InvestigationState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools.catalog.artifacts.uploads import (
    COMPLETE_INVESTIGATION_ROOTFS_TOOL,
    CREATE_INVESTIGATION_UPLOAD_TOOL,
    investigation_rootfs_object_name,
    upload_expiry_contract,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.services.investigations.common import (
    InvestigationServiceError,
    resolve_contributor_investigation,
)
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)

_TENANT = "local"
_OWNER_KIND = "investigations"
_ROOTFS_RETENTION_CLASS = "rootfs"
_ACCEPTING_STATES = frozenset({InvestigationState.OPEN, InvestigationState.ACTIVE})

# Insert the write-once row against the migration-0076 partial UNIQUE index on
# ``artifacts(object_key) WHERE owner_kind='investigations'``: two finalizes for the same
# content-addressed key converge on one row rather than inserting duplicates.
_INSERT_ROW_SQL = (
    "INSERT INTO artifacts "
    "(owner_kind, owner_id, object_key, etag, sensitivity, retention_class, "
    " encoding, uncompressed_size) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (object_key) WHERE owner_kind = 'investigations' DO NOTHING"
)
_SELECT_ROW_SQL = (
    "SELECT id, encoding, uncompressed_size FROM artifacts "
    "WHERE owner_kind = 'investigations' AND object_key = %s"
)


class _HeadStore(Protocol):
    def head(self, key: str) -> HeadResult | None: ...


async def complete_rootfs_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    investigation_id: str,
    *,
    store: _HeadStore | None = None,
) -> ToolResponse:
    """Finalize the investigation's uploaded rootfs and return its ``checksum_sha256`` handle."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _invalid_uuid_error("investigation_id", investigation_id)
    try:
        head_store = store or object_store_from_env()
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(
            investigation_id, exc, suggested_next_actions=[COMPLETE_INVESTIGATION_ROOTFS_TOOL]
        )
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            try:
                inv = await resolve_contributor_investigation(conn, ctx, uid, investigation_id)
            except InvestigationServiceError:
                return _config_error(investigation_id, data={"reason": "not_found"})
            return await _finalize_locked(conn, ctx, uid, investigation_id, inv.project, head_store)


async def _finalize_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    raw_id: str,
    project: str,
    store: _HeadStore,
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await INVESTIGATIONS.get(conn, uid)
        if current is None:
            return _config_error(raw_id, data={"reason": "not_found"})
        if current.state not in _ACCEPTING_STATES:
            # Lost the race to a close: leave the manifest for the re-scoped reaper (ADR-0441 §3).
            return _config_error(
                raw_id,
                detail="investigation is not accepting a rootfs finalize; it is terminal",
                data={"reason": "investigation_not_accepting_upload"},
            )
        entry = await _rootfs_entry(conn, uid, raw_id)
        if isinstance(entry, ToolResponse):
            return entry
        expired = await _reject_if_expired(conn, uid, raw_id)
        if expired is not None:
            return expired
        object_key = artifact_key(
            _TENANT, _OWNER_KIND, str(uid), investigation_rootfs_object_name(entry)
        )
        head = await asyncio.to_thread(store.head, object_key)
        verdict = _verify_head(raw_id, head, entry.sha256)
        if verdict is not None:
            return verdict
        assert head is not None  # _verify_head returned no rejection ⇒ head is present
        await _write_row(conn, uid, object_key, head, entry)
        await _record_audit(conn, ctx, uid, project)
        await upload_manifest.delete_manifest(conn, _OWNER_KIND, uid)
    return ToolResponse.success(
        raw_id,
        "rootfs_upload_complete",
        suggested_next_actions=["systems.define"],
        data={
            "checksum_sha256": entry.sha256,
            "object_key": object_key,
            "encoding": entry.encoding,
            "uncompressed_size": entry.uncompressed_size,
        },
    )


async def _rootfs_entry(
    conn: AsyncConnection, uid: UUID, raw_id: str
) -> upload_manifest.ManifestEntry | ToolResponse:
    manifest = await upload_manifest.get_manifest(conn, _OWNER_KIND, uid)
    if manifest is None:
        return _no_manifest_error(raw_id)
    entry = next((e for e in manifest.entries if e.name == "rootfs"), None)
    if entry is None:
        return _no_manifest_error(raw_id)
    return entry


def _no_manifest_error(raw_id: str) -> ToolResponse:
    """Reject a finalize with no open window, pointing at the mint that opens one.

    This is also where a finalize that arrives *after* the reaper collected a lapsed window lands
    (ADR-0444), so it carries the same recovery action as the expiry rejection below.
    """
    return ToolResponse.failure(
        raw_id,
        ErrorCategory.CONFIGURATION_ERROR,
        suggested_next_actions=[CREATE_INVESTIGATION_UPLOAD_TOOL],
        data={"reason": "no_upload_manifest"},
    )


async def _reject_if_expired(conn: AsyncConnection, uid: UUID, raw_id: str) -> ToolResponse | None:
    """Reject a finalize past the manifest deadline; return ``None`` while the window is open.

    The deadline and the clock it is measured against are read from one statement on the Postgres
    clock that stamped it and that the reaper measures against (ADR-0444), so finalize and the
    reaper cannot reach opposite verdicts on the same manifest. ``now()`` is the transaction's
    start, so a request that arrived inside the window is never rejected for time it spent
    waiting on the investigation lock.
    """
    stamp = await upload_manifest.deadline_stamp(conn, _OWNER_KIND, uid)
    if stamp is None:
        return _no_manifest_error(raw_id)
    if stamp.deadline >= stamp.server_time:
        return None
    return ToolResponse.failure(
        raw_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail="the rootfs upload window has expired; re-mint it and re-upload the object",
        suggested_next_actions=[CREATE_INVESTIGATION_UPLOAD_TOOL],
        data={
            "reason": "upload_window_expired",
            **upload_expiry_contract(stamp, remint_tool=CREATE_INVESTIGATION_UPLOAD_TOOL),
        },
    )


def _verify_head(raw_id: str, head: HeadResult | None, declared: str) -> ToolResponse | None:
    """Return a rejection when the HEAD is missing, checksum-less, or mismatched; else ``None``."""
    if head is None:
        return _config_error(
            raw_id,
            detail="the declared rootfs object was never uploaded",
            data={"reason": "rootfs_not_uploaded"},
        )
    if head.checksum_sha256 is None:
        return _config_error(
            raw_id,
            detail="the uploaded rootfs object has no stored checksum; re-upload via the URL",
            data={"reason": "rootfs_checksum_missing"},
        )
    if head.checksum_sha256 != declared:
        # The object_key was minted from the declared checksum, so a mismatch is a store
        # checksum-encoding surprise; fail here rather than as a silent resolution miss later.
        return _config_error(
            raw_id,
            detail="the uploaded rootfs object's checksum does not match the declared checksum",
            data={"reason": "rootfs_checksum_mismatch"},
        )
    return None


async def _write_row(
    conn: AsyncConnection,
    uid: UUID,
    object_key: str,
    head: HeadResult,
    entry: upload_manifest.ManifestEntry,
) -> None:
    await conn.execute(
        _INSERT_ROW_SQL,
        (
            _OWNER_KIND,
            uid,
            object_key,
            head.etag,
            Sensitivity.SENSITIVE.value,
            _ROOTFS_RETENTION_CLASS,
            entry.encoding,
            entry.uncompressed_size,
        ),
    )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SELECT_ROW_SQL, (object_key,))
        rows = await cur.fetchall()
    if len(rows) != 1:  # the partial UNIQUE index must converge concurrent finalizes on one row
        raise RuntimeError(
            f"complete_rootfs_upload expected exactly one artifacts row for {object_key!r}, "
            f"found {len(rows)}"
        )


async def _record_audit(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, project: str
) -> None:
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=COMPLETE_INVESTIGATION_ROOTFS_TOOL,
            object_kind=_OWNER_KIND,
            object_id=uid,
            transition="complete_rootfs_upload",
            args={"investigation_id": str(uid)},
            project=project,
        ),
    )


__all__ = ["complete_rootfs_upload"]
