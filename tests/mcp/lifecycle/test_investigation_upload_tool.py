"""Investigation-scoped rootfs upload window — presign + manifest (ADR-0441, #1502)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import PresignedUpload, PresignPutRequest
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import InvestigationState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Investigation
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog.artifacts.uploads import (
    ArtifactDeclaration,
    create_investigation_upload,
    rootfs_object_token,
)
from kdive.security.audit import args_digest
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_ROOTFS_SHA256 = base64.b64encode(hashlib.sha256(b"rootfs-bytes").digest()).decode("ascii")
_TOKEN = rootfs_object_token(_ROOTFS_SHA256)


class _FakeStore:
    """A presign-only store fake for the investigation (rootfs) owner."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        self.calls.append((request.key, request.sha256, request.size_bytes))
        assert request.sensitivity is Sensitivity.SENSITIVE
        assert request.retention_class == "rootfs"
        return PresignedUpload(
            url=f"https://store/{request.key}",
            required_headers={"x-amz-checksum-sha256": request.sha256},
        )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx(
    role: Role | None = Role.CONTRIBUTOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


async def _seed_investigation(
    pool: AsyncConnectionPool,
    *,
    project: str = "proj",
    state: InvestigationState = InvestigationState.OPEN,
) -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                title="seeded",
                state=state,
            ),
        )
    return str(inv.id)


async def _audit_rows(pool: AsyncConnectionPool, object_id: str) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT tool, object_kind, transition, args_digest FROM audit_log "
            "WHERE object_id = %s ORDER BY ts DESC",
            (object_id,),
        )
        return await cur.fetchall()


async def _create(
    pool: AsyncConnectionPool,
    inv_id: str,
    artifacts: list[ArtifactDeclaration],
    *,
    ctx: RequestContext | None = None,
    store: Any = None,
) -> ToolResponse:
    return await create_investigation_upload(
        pool,
        ctx or _ctx(),
        investigation_id=inv_id,
        artifacts=artifacts,
        resolver=provider_resolver(),
        store=store,
    )


def test_mints_content_addressed_rootfs_put_and_persists_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            store = _FakeStore()
            responses = await _create(
                pool,
                inv_id,
                [{"name": "rootfs", "sha256": _ROOTFS_SHA256, "size_bytes": 4096}],
                store=store,
            )
            items = responses.items
            expected_key = f"local/investigations/{inv_id}/rootfs-{_TOKEN}"
            assert responses.status == "upload_ready"
            assert [r.object_id for r in items] == [expected_key]
            assert items[0].suggested_next_actions == ["investigations.complete_rootfs_upload"]
            assert items[0].refs["upload_url"].startswith("https://store/")
            assert store.calls == [(expected_key, _ROOTFS_SHA256, 4096)]
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "investigations", UUID(inv_id))
            assert manifest is not None
            assert {e.name for e in manifest.entries} == {"rootfs"}

    asyncio.run(_run())


def test_states_deadline_contract_and_remint_tool(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            return await _create(
                pool,
                inv_id,
                [{"name": "rootfs", "sha256": _ROOTFS_SHA256, "size_bytes": 4096}],
                store=_FakeStore(),
            )

    responses = asyncio.run(_run())
    data = responses.data
    assert isinstance(data["server_time"], str) and data["server_time"].endswith("+00:00")
    assert isinstance(data["manifest_deadline"], str)
    assert data["on_expiry"] == {
        "tool": "artifacts.create_investigation_upload",
        "effect": "re-mint replaces the manifest and resets the deadline",
    }
    assert isinstance(responses.items[0].data["expires_at"], str)


def test_rejects_closed_investigation(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.CLOSED)
            store = _FakeStore()
            out = await _create(
                pool,
                inv_id,
                [{"name": "rootfs", "sha256": _ROOTFS_SHA256, "size_bytes": 4096}],
                store=store,
            )
            assert store.calls == []
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "investigations", UUID(inv_id))
            assert manifest is None
            return out

    out = asyncio.run(_run())
    assert out.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert out.data["reason"] == "owner_not_accepting_upload"


def test_accepts_active_investigation(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.ACTIVE)
            return await _create(
                pool,
                inv_id,
                [{"name": "rootfs", "sha256": _ROOTFS_SHA256, "size_bytes": 4096}],
                store=_FakeStore(),
            )

    out = asyncio.run(_run())
    assert out.status == "upload_ready"


def test_rejects_chunked_rootfs(migrated_url: str) -> None:
    _5gib = 5 * 1024 * 1024 * 1024

    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            store = _FakeStore()
            out = await _create(
                pool,
                inv_id,
                [
                    {
                        "name": "rootfs",
                        "sha256": _ROOTFS_SHA256,
                        "size_bytes": _5gib + 100,
                        "chunks": [
                            {"sha256": "c0", "size_bytes": _5gib},
                            {"sha256": "c1", "size_bytes": 100},
                        ],
                    }
                ],
                store=store,
            )
            assert store.calls == []
            return out

    out = asyncio.run(_run())
    assert out.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert out.data["reason"] == "chunking_not_supported"


def test_accepts_gzip_encoding_and_persists(migrated_url: str) -> None:
    async def _run() -> tuple[ToolResponse, upload_manifest.ManifestEntry]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            store = _FakeStore()
            responses = await _create(
                pool,
                inv_id,
                [
                    {
                        "name": "rootfs",
                        "sha256": _ROOTFS_SHA256,
                        "size_bytes": 4096,
                        "encoding": "gzip",
                        "uncompressed_size": 6 * 1024 * 1024 * 1024,
                    }
                ],
                store=store,
            )
            # The signed bytes are the compressed transport object, not the canonical size.
            assert store.calls == [
                (f"local/investigations/{inv_id}/rootfs-{_TOKEN}", _ROOTFS_SHA256, 4096)
            ]
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "investigations", UUID(inv_id))
            assert manifest is not None
            return responses, manifest.entries[0]

    responses, rootfs = asyncio.run(_run())
    assert responses.status == "upload_ready"
    assert rootfs.encoding == "gzip"
    assert rootfs.uncompressed_size == 6 * 1024 * 1024 * 1024


def test_rejects_bad_checksum_before_minting(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            store = _FakeStore()
            out = await _create(
                pool,
                inv_id,
                [{"name": "rootfs", "sha256": "not-a-32-byte-digest", "size_bytes": 4096}],
                store=store,
            )
            assert store.calls == []
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "investigations", UUID(inv_id))
            assert manifest is None
            return out

    out = asyncio.run(_run())
    assert out.error_category == ErrorCategory.CONFIGURATION_ERROR.value


def test_is_contributor_gated(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            with pytest.raises(AuthorizationError):
                await _create(
                    pool,
                    inv_id,
                    [{"name": "rootfs", "sha256": _ROOTFS_SHA256, "size_bytes": 4096}],
                    ctx=_ctx(role=Role.VIEWER),
                    store=_FakeStore(),
                )
            responses = await _create(
                pool,
                inv_id,
                [{"name": "rootfs", "sha256": _ROOTFS_SHA256, "size_bytes": 4096}],
                ctx=_ctx(role=Role.CONTRIBUTOR),
                store=_FakeStore(),
            )
            assert responses.status == "upload_ready"

    asyncio.run(_run())


def test_writes_audit_row(migrated_url: str) -> None:
    async def _run() -> tuple[str, list[tuple[Any, ...]]]:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            await _create(
                pool,
                inv_id,
                [{"name": "rootfs", "sha256": _ROOTFS_SHA256, "size_bytes": 4096}],
                store=_FakeStore(),
            )
            return inv_id, await _audit_rows(pool, inv_id)

    inv_id, rows = asyncio.run(_run())
    assert len(rows) == 1
    tool, object_kind, transition, digest = rows[0]
    assert tool == "artifacts.create_investigation_upload"
    assert object_kind == "investigations"
    assert transition == "create_upload"
    assert digest == args_digest({"owner_id": inv_id, "artifacts": ["rootfs"]})
