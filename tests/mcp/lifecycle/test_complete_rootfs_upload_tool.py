"""Investigation rootfs finalize — write-once row + idempotency (ADR-0441, #1502)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import HeadResult, PresignedUpload, PresignPutRequest, artifact_key
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import InvestigationState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Investigation
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog.artifacts.uploads import (
    create_investigation_upload,
    investigation_rootfs_object_name,
)
from kdive.mcp.tools.lifecycle.investigations.complete_rootfs_upload import (
    complete_rootfs_upload,
)
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.clock import STORE_MTIME
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_ROOTFS_SHA256 = base64.b64encode(hashlib.sha256(b"rootfs-bytes").digest()).decode("ascii")


class _PresignStore:
    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        return PresignedUpload(url=f"https://store/{request.key}", required_headers={})


class _HeadStore:
    """A head-only store fake keyed on one object's stored checksum."""

    def __init__(self, checksum: str | None, *, present: bool = True, etag: str = "etag-1") -> None:
        self.checksum = checksum
        self.present = present
        self.etag = etag
        self.keys: list[str] = []

    def head(self, key: str) -> HeadResult | None:
        self.keys.append(key)
        if not self.present:
            return None
        return HeadResult(
            size_bytes=4096,
            checksum_sha256=self.checksum,
            etag=self.etag,
            sensitivity=Sensitivity.SENSITIVE,
            last_modified=STORE_MTIME,
            version_id="test-version",
        )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx(role: Role | None = Role.CONTRIBUTOR) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=("proj",), roles=roles)


async def _seed_investigation(
    pool: AsyncConnectionPool, *, state: InvestigationState = InvestigationState.OPEN
) -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                title="seeded",
                state=state,
            ),
        )
    return str(inv.id)


async def _open_window(
    pool: AsyncConnectionPool,
    inv_id: str,
    *,
    encoding: str | None = None,
    uncompressed_size: int | None = None,
) -> str:
    """Open the upload window (persist the manifest) and return the derived object key."""
    decl: dict[str, Any] = {"name": "rootfs", "sha256": _ROOTFS_SHA256, "size_bytes": 4096}
    if encoding is not None:
        decl["encoding"] = encoding
        decl["uncompressed_size"] = uncompressed_size
    resp = await create_investigation_upload(
        pool,
        _ctx(),
        investigation_id=inv_id,
        artifacts=[decl],
        resolver=provider_resolver(),
        store=_PresignStore(),
    )
    assert resp.status == "upload_ready"
    entry = upload_manifest.ManifestEntry("rootfs", _ROOTFS_SHA256, 4096)
    return artifact_key("local", "investigations", inv_id, investigation_rootfs_object_name(entry))


async def _set_deadline(pool: AsyncConnectionPool, inv_id: str, offset: timedelta) -> None:
    """Move the open window's deadline by an offset, on the DB clock the check reads."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE upload_manifests SET deadline = now() + %s "
            "WHERE owner_kind = 'investigations' AND owner_id = %s",
            (offset, UUID(inv_id)),
        )
        assert cur.rowcount == 1


async def _investigation_rootfs_rows(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT object_key, retention_class, sensitivity, encoding, uncompressed_size "
            "FROM artifacts WHERE owner_kind = 'investigations' ORDER BY object_key"
        )
        cols = [c.name for c in cur.description or []]
        return [dict(zip(cols, row, strict=True)) for row in await cur.fetchall()]


async def _finalize(
    pool: AsyncConnectionPool, inv_id: str, store: _HeadStore, *, ctx: RequestContext | None = None
) -> ToolResponse:
    return await complete_rootfs_upload(pool, ctx or _ctx(), inv_id, store=store)


def test_finalize_writes_row_and_returns_checksum(migrated_url: str) -> None:
    async def _run() -> tuple[ToolResponse, list[dict[str, Any]], Any]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            key = await _open_window(pool, inv_id)
            out = await _finalize(pool, inv_id, _HeadStore(_ROOTFS_SHA256))
            rows = await _investigation_rootfs_rows(pool)
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "investigations", UUID(inv_id))
            assert out.data["object_key"] == key
            return out, rows, manifest

    out, rows, manifest = asyncio.run(_run())
    assert out.status == "rootfs_upload_complete"
    assert out.data["checksum_sha256"] == _ROOTFS_SHA256
    assert len(rows) == 1
    assert rows[0]["retention_class"] == "rootfs"
    assert rows[0]["sensitivity"] == "sensitive"
    assert manifest is None  # deleted on success


def test_finalize_persists_gzip_encoding(migrated_url: str) -> None:
    # AC-4c: a gzip upload's encoding/uncompressed_size are recoverable from the durable row.
    async def _run() -> list[dict[str, Any]]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _open_window(
                pool, inv_id, encoding="gzip", uncompressed_size=6 * 1024 * 1024 * 1024
            )
            out = await _finalize(pool, inv_id, _HeadStore(_ROOTFS_SHA256))
            assert out.status == "rootfs_upload_complete"
            return await _investigation_rootfs_rows(pool)

    rows = asyncio.run(_run())
    assert len(rows) == 1
    assert rows[0]["encoding"] == "gzip"
    assert rows[0]["uncompressed_size"] == 6 * 1024 * 1024 * 1024


def test_finalize_idempotent_recall_yields_one_row(migrated_url: str) -> None:
    # AC-4: a second finalize after success does not create a duplicate row.
    async def _run() -> tuple[ToolResponse, list[dict[str, Any]]]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _open_window(pool, inv_id)
            first = await _finalize(pool, inv_id, _HeadStore(_ROOTFS_SHA256))
            assert first.status == "rootfs_upload_complete"
            second = await _finalize(pool, inv_id, _HeadStore(_ROOTFS_SHA256))
            return second, await _investigation_rootfs_rows(pool)

    second, rows = asyncio.run(_run())
    assert second.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert second.data["reason"] == "no_upload_manifest"
    assert len(rows) == 1  # no duplicate


def test_finalize_converges_on_conflicting_row(migrated_url: str) -> None:
    # AC-4 (concurrent proof): a finalize whose content-addressed row already exists (a racing
    # finalize committed it) converges via INSERT ... ON CONFLICT DO NOTHING onto that one row.
    async def _run() -> tuple[ToolResponse, list[dict[str, Any]]]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            key = await _open_window(pool, inv_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                    "retention_class) VALUES ('investigations', %s, %s, 'preexisting', "
                    "'sensitive', 'rootfs')",
                    (UUID(inv_id), key),
                )
            out = await _finalize(pool, inv_id, _HeadStore(_ROOTFS_SHA256))
            return out, await _investigation_rootfs_rows(pool)

    out, rows = asyncio.run(_run())
    assert out.status == "rootfs_upload_complete"
    assert out.data["checksum_sha256"] == _ROOTFS_SHA256
    assert len(rows) == 1  # ON CONFLICT DO NOTHING kept the single row


def test_finalize_rejects_terminal_investigation_and_leaves_manifest(migrated_url: str) -> None:
    # AC-4d reject branch: finalize on a closed investigation rejects and leaves the manifest
    # for the re-scoped reaper — no committed row.
    async def _run() -> tuple[ToolResponse, Any, list[dict[str, Any]]]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _open_window(pool, inv_id)
            async with pool.connection() as conn:
                await INVESTIGATIONS.update_state(conn, UUID(inv_id), InvestigationState.CLOSED)
            out = await _finalize(pool, inv_id, _HeadStore(_ROOTFS_SHA256))
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "investigations", UUID(inv_id))
            return out, manifest, await _investigation_rootfs_rows(pool)

    out, manifest, rows = asyncio.run(_run())
    assert out.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert out.data["reason"] == "investigation_not_accepting_upload"
    assert manifest is not None  # left for the reaper
    assert rows == []  # nothing committed


def test_finalize_rejects_missing_object(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _open_window(pool, inv_id)
            return await _finalize(pool, inv_id, _HeadStore(None, present=False))

    out = asyncio.run(_run())
    assert out.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert out.data["reason"] == "rootfs_not_uploaded"


def test_finalize_rejects_checksumless_object(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _open_window(pool, inv_id)
            return await _finalize(pool, inv_id, _HeadStore(None))

    out = asyncio.run(_run())
    assert out.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert out.data["reason"] == "rootfs_checksum_missing"


def test_finalize_rejects_checksum_mismatch(migrated_url: str) -> None:
    async def _run() -> tuple[ToolResponse, list[dict[str, Any]]]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _open_window(pool, inv_id)
            out = await _finalize(pool, inv_id, _HeadStore("c29tZS1vdGhlci1kaWdlc3Q="))
            return out, await _investigation_rootfs_rows(pool)

    out, rows = asyncio.run(_run())
    assert out.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert out.data["reason"] == "rootfs_checksum_mismatch"
    assert rows == []


def test_finalize_no_manifest_rejects(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            return await _finalize(pool, inv_id, _HeadStore(_ROOTFS_SHA256))

    out = asyncio.run(_run())
    assert out.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert out.data["reason"] == "no_upload_manifest"
    # A finalize arriving after the reaper collected the window lands here; point it at the
    # same re-mint the expiry rejection names (ADR-0444 decision 1).
    assert out.suggested_next_actions == ["artifacts.create_investigation_upload"]


def test_finalize_rejects_past_deadline_window(migrated_url: str) -> None:
    """AC-1: a past-deadline manifest is rejected with a self-correcting `upload_window_expired`
    naming the deadline, the DB reference clock, and the re-mint tool (ADR-0444 decision 1). The
    manifest is left for the reaper and nothing is committed."""

    async def _run() -> tuple[ToolResponse, Any, list[dict[str, Any]], list[str]]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _open_window(pool, inv_id)
            await _set_deadline(pool, inv_id, timedelta(seconds=-1))
            store = _HeadStore(_ROOTFS_SHA256)
            out = await _finalize(pool, inv_id, store)
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "investigations", UUID(inv_id))
            return out, manifest, await _investigation_rootfs_rows(pool), store.keys

    out, manifest, rows, head_keys = asyncio.run(_run())
    assert out.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert out.data["reason"] == "upload_window_expired"
    assert out.suggested_next_actions == ["artifacts.create_investigation_upload"]
    # The rejection states the wall, the clock it is measured on, and the recovery (#1336).
    deadline, server_time = out.data["manifest_deadline"], out.data["server_time"]
    assert isinstance(deadline, str) and isinstance(server_time, str)
    assert deadline < server_time  # both ISO-8601 UTC, so lexicographic order is chronological
    on_expiry = out.data["on_expiry"]
    assert isinstance(on_expiry, dict)
    assert on_expiry["tool"] == "artifacts.create_investigation_upload"
    assert manifest is not None  # left for the reaper, not deleted by the rejection
    assert rows == []  # nothing committed
    assert head_keys == []  # rejected before the object HEAD


def test_finalize_inside_deadline_still_succeeds(migrated_url: str) -> None:
    # AC-2: the deadline check only rejects a lapsed window — a live one finalizes as before.
    async def _run() -> tuple[ToolResponse, list[dict[str, Any]]]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _open_window(pool, inv_id)
            await _set_deadline(pool, inv_id, timedelta(hours=1))
            out = await _finalize(pool, inv_id, _HeadStore(_ROOTFS_SHA256))
            return out, await _investigation_rootfs_rows(pool)

    out, rows = asyncio.run(_run())
    assert out.status == "rootfs_upload_complete"
    assert out.data["checksum_sha256"] == _ROOTFS_SHA256
    assert len(rows) == 1


def test_finalize_is_contributor_gated(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _open_window(pool, inv_id)
            viewer = _ctx(role=Role.VIEWER)
            with pytest.raises(AuthorizationError):
                await _finalize(pool, inv_id, _HeadStore(_ROOTFS_SHA256), ctx=viewer)

    asyncio.run(_run())
