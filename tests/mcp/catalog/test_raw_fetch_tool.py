"""artifacts.fetch_raw tool tests — handler called directly with an injected pool (#781).

Covers the owner-fetchable raw vmcore + vmlinux egress: presigned-URL happy paths, the
per-asset cross-project / role gate, the ``*_unavailable`` edges, HEAD-before-presign, and
the audited egress (ADR-0243).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import HeadResult
from kdive.mcp.app import build_app
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog.artifacts.raw_fetch import RawAsset, fetch_raw
from kdive.security.authz.rbac import Role, RoleDenied
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.artifacts.read_model import run_fetch_context, system_project
from tests.integration._seed import seed_unbound_running_run
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair

_VMLINUX_REF = "proj/runs/r/vmlinux"


class _FakeStore:
    """A fake object store exposing only what fetch_raw needs: head + presign_get."""

    url = "https://signed.example/download?token=stub"

    def __init__(self, *, missing_head: bool = False, size: int = 4096) -> None:
        self.missing_head = missing_head
        self.size = size
        self.presigned_keys: list[str] = []

    def head(self, key: str) -> HeadResult | None:
        if self.missing_head:
            return None
        return HeadResult(size_bytes=self.size, checksum_sha256=None, etag="e", sensitivity=None)

    def presign_get(self, key: str, *, expires_in: int) -> str:
        self.presigned_keys.append(key)
        return self.url


def _ctx(
    role: Role | None = Role.CONTRIBUTOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_run_with_vmlinux(pool: AsyncConnectionPool) -> str:
    """Seed a crashed System + a succeeded Run on it carrying a vmlinux ref; return run id."""
    sys_id = await seed_crashed_system(pool)
    return await seed_run_on_system(pool, sys_id, debuginfo_ref=_VMLINUX_REF, build_id="deadbeef")


async def _seed_raw_vmcore_row(pool: AsyncConnectionPool, run_id: str) -> str:
    """Insert a raw Run-owned vmcore artifact row (ADR-0244); return the object key."""
    key = f"proj/runs/{run_id}/vmcore-host_dump"
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('runs', %s, %s, 'e', 'sensitive', 'vmcore')",
            (run_id, key),
        )
    return key


# --- DB readers (Task 1) ------------------------------------------------------------------


def test_run_fetch_context_returns_row_fields(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run_with_vmlinux(pool)
            async with pool.connection() as conn:
                ctx = await run_fetch_context(conn, UUID(run_id))
                assert ctx is not None
                assert ctx.project == "proj"
                assert ctx.debuginfo_ref == _VMLINUX_REF
                assert ctx.system_id is not None
                assert await system_project(conn, ctx.system_id) == "proj"
                assert await run_fetch_context(conn, uuid4()) is None
                assert await system_project(conn, uuid4()) is None

    asyncio.run(_run())


# --- fetch_raw happy paths (Task 2) -------------------------------------------------------


def test_fetch_raw_vmlinux_presigns_url(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run_with_vmlinux(pool)
            store = _FakeStore()
            resp = await fetch_raw(
                pool, _ctx(), run_id=run_id, asset=RawAsset.VMLINUX, store_factory=lambda: store
            )
        assert resp.status == "available"
        assert resp.refs["download_uri"] == store.url
        assert resp.data["asset"] == "vmlinux"
        assert resp.data["size_bytes"] == 4096
        assert "content" not in resp.data
        assert store.presigned_keys == [_VMLINUX_REF]

    asyncio.run(_run())


def test_fetch_raw_vmcore_presigns_url(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run_with_vmlinux(pool)
            key = await _seed_raw_vmcore_row(pool, run_id)
            store = _FakeStore()
            resp = await fetch_raw(
                pool, _ctx(), run_id=run_id, asset=RawAsset.VMCORE, store_factory=lambda: store
            )
        assert resp.status == "available"
        assert resp.data["asset"] == "vmcore"
        assert resp.refs["download_uri"] == store.url
        assert store.presigned_keys == [key]

    asyncio.run(_run())


def test_fetch_raw_vmcore_cross_project_is_not_found(migrated_url: str) -> None:
    # The Run-owned vmcore is gated on the Run's project (ADR-0244); a caller in another project
    # gets the existence-masking not_found, never the core. Guards the egress gate move from the
    # System's project to the Run's (run.project == system.project is enforced at admission/bind).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run_with_vmlinux(pool)
            await _seed_raw_vmcore_row(pool, run_id)
            store = _FakeStore()
            resp = await fetch_raw(
                pool,
                _ctx(projects=("other",)),
                run_id=run_id,
                asset=RawAsset.VMCORE,
                store_factory=lambda: store,
            )
        assert resp.status == "error"
        assert "download_uri" not in resp.refs
        assert store.presigned_keys == []

    asyncio.run(_run())


# --- authorization + edges (Task 3) -------------------------------------------------------


def test_fetch_raw_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run_with_vmlinux(pool)
            store = _FakeStore()
            resp = await fetch_raw(
                pool,
                _ctx(projects=("other",)),
                run_id=run_id,
                asset=RawAsset.VMLINUX,
                store_factory=lambda: store,
            )
        assert resp.status == "error"
        assert "download_uri" not in resp.refs
        assert store.presigned_keys == []

    asyncio.run(_run())


def test_fetch_raw_viewer_role_is_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run_with_vmlinux(pool)
            with pytest.raises(RoleDenied):
                await fetch_raw(
                    pool,
                    _ctx(Role.VIEWER),
                    run_id=run_id,
                    asset=RawAsset.VMLINUX,
                    store_factory=_FakeStore,
                )

    asyncio.run(_run())


def test_fetch_raw_vmlinux_unavailable_when_no_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_unbound_running_run(pool)
            resp = await fetch_raw(
                pool, _ctx(), run_id=run_id, asset=RawAsset.VMLINUX, store_factory=_FakeStore
            )
        assert resp.status == "error"
        assert resp.data["reason"] == "vmlinux_unavailable"

    asyncio.run(_run())


def test_fetch_raw_vmcore_unavailable_when_no_core(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run_with_vmlinux(pool)
            resp = await fetch_raw(
                pool, _ctx(), run_id=run_id, asset=RawAsset.VMCORE, store_factory=_FakeStore
            )
        assert resp.status == "error"
        assert resp.data["reason"] == "vmcore_unavailable"

    asyncio.run(_run())


def test_fetch_raw_vmcore_unavailable_when_run_unbound(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_unbound_running_run(pool)
            resp = await fetch_raw(
                pool, _ctx(), run_id=run_id, asset=RawAsset.VMCORE, store_factory=_FakeStore
            )
        assert resp.status == "error"
        assert resp.data["reason"] == "vmcore_unavailable"

    asyncio.run(_run())


def test_fetch_raw_unavailable_when_object_missing(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run_with_vmlinux(pool)
            store = _FakeStore(missing_head=True)
            resp = await fetch_raw(
                pool, _ctx(), run_id=run_id, asset=RawAsset.VMLINUX, store_factory=lambda: store
            )
        assert resp.status == "error"
        assert resp.data["reason"] == "vmlinux_unavailable"
        assert store.presigned_keys == []

    asyncio.run(_run())


def test_fetch_raw_malformed_run_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await fetch_raw(
                pool, _ctx(), run_id="not-a-uuid", asset=RawAsset.VMLINUX, store_factory=_FakeStore
            )
        assert resp.status == "error"

    asyncio.run(_run())


def test_fetch_raw_writes_audit_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run_with_vmlinux(pool)
            store = _FakeStore()
            resp = await fetch_raw(
                pool, _ctx(), run_id=run_id, asset=RawAsset.VMLINUX, store_factory=lambda: store
            )
            assert resp.status == "available"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE tool = 'artifacts.fetch_raw' "
                    "AND project = 'proj'"
                )
                row = await cur.fetchone()
            assert row is not None and row["n"] == 1

    asyncio.run(_run())


# --- tool registration (Task 4) -----------------------------------------------------------


def test_fetch_raw_tool_registered() -> None:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    tool = asyncio.run(app.get_tool("artifacts.fetch_raw"))
    assert tool is not None
    props = tool.parameters["properties"]
    assert set(props) == {"run_id", "asset"}
    assert props["asset"]["$ref"] == "#/$defs/RawAsset"
    assert set(tool.parameters["$defs"]["RawAsset"]["enum"]) == {"vmcore", "vmlinux"}
