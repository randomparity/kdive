"""``images.kernel_config`` read tool: presigned download URL for a stored image config (ADR-0317).

Resolves the row under the ``images.describe`` visibility predicate (public, or owned-private
with ``viewer``), HEADs the stored ``/boot/config-<ver>`` object, and presigns a short-lived GET.
A row with no config (no ``kernel_config_key``), a missing object, or an invisible/absent row is a
``configuration_error``/``not_found``; the config is never inspected or validated.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import HeadResult
from kdive.mcp.tools.catalog import kernel_config as kernel_config_tools
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role


def _ctx(*projects: str) -> RequestContext:
    return RequestContext(
        principal="dev-1",
        agent_session="sess-1",
        projects=tuple(projects),
        roles={p: Role.VIEWER for p in projects},
        platform_roles=frozenset(),
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


class _FakeStore:
    """A narrow config-fetch store stand-in: presence via ``present``, a fixed presigned URL."""

    def __init__(self, *, present: set[str] | None = None, size: int = 42) -> None:
        self._present = present if present is not None else set()
        self._size = size
        self.presigned: list[str] = []

    def head(self, key: str) -> HeadResult | None:
        if key not in self._present:
            return None
        return HeadResult(size_bytes=self._size, checksum_sha256=None, etag="etag")

    def presign_get(self, key: str, *, expires_in: int) -> str:
        self.presigned.append(key)
        return f"https://signed/{key}?ttl={expires_in}"


async def _insert(
    pool: AsyncConnectionPool,
    *,
    name: str,
    visibility: str = "public",
    owner: str | None = None,
    kernel_config_key: str | None = None,
    provenance: str = "{}",
) -> str:
    key = f"images/local-libvirt/{name}/x86_64.qcow2"
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, kernel_config_key, digest, "
            " provenance, visibility, owner, expires_at, state, pending_since) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(key)s, "
            " %(ckey)s, 'sha256:abc', %(prov)s::jsonb, %(vis)s, %(owner)s, "
            " CASE WHEN %(vis)s = 'private' THEN now() + interval '1 hour' ELSE NULL END, "
            " 'registered', now()) RETURNING id",
            {
                "name": name,
                "key": key,
                "ckey": kernel_config_key,
                "prov": provenance,
                "vis": visibility,
                "owner": owner,
            },
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row[0])


def test_present_config_returns_download_uri(migrated_url: str) -> None:
    config_key = "images/local-libvirt/fedora/x86_64.config"

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert(
                pool,
                name="fedora",
                kernel_config_key=config_key,
                provenance='{"default_kernel_version": "6.19.10-300.fc44.x86_64"}',
            )
            store = _FakeStore(present={config_key}, size=1234)
            resp = await kernel_config_tools.kernel_config(
                pool, _ctx(), image_id, store_factory=lambda: store
            )
        assert resp.refs is not None
        assert resp.refs["download_uri"].startswith(f"https://signed/{config_key}")
        assert store.presigned == [config_key]
        assert resp.data["default_kernel_version"] == "6.19.10-300.fc44.x86_64"
        assert resp.data["size_bytes"] == 1234

    asyncio.run(_run())


def test_no_config_key_is_unavailable(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert(pool, name="no-config", kernel_config_key=None)
            store = _FakeStore()
            resp = await kernel_config_tools.kernel_config(
                pool, _ctx(), image_id, store_factory=lambda: store
            )
        assert resp.error_category is not None
        assert resp.data["reason"] == "kernel_config_unavailable"
        assert store.presigned == []

    asyncio.run(_run())


def test_object_absent_is_unavailable(migrated_url: str) -> None:
    config_key = "images/local-libvirt/gone/x86_64.config"

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert(pool, name="gone", kernel_config_key=config_key)
            store = _FakeStore(present=set())  # key set but object missing
            resp = await kernel_config_tools.kernel_config(
                pool, _ctx(), image_id, store_factory=lambda: store
            )
        assert resp.error_category is not None
        assert resp.data["reason"] == "kernel_config_unavailable"

    asyncio.run(_run())


def test_invisible_private_is_not_found(migrated_url: str) -> None:
    config_key = "images/local-libvirt/secret/x86_64.config"

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert(
                pool,
                name="secret",
                visibility="private",
                owner="other-proj",
                kernel_config_key=config_key,
            )
            store = _FakeStore(present={config_key})
            # Caller has no grant on other-proj: byte-identical not_found, no config leak.
            resp = await kernel_config_tools.kernel_config(
                pool, _ctx(), image_id, store_factory=lambda: store
            )
        assert resp.status == "error" and resp.error_category == "not_found"
        assert store.presigned == []

    asyncio.run(_run())


def test_malformed_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await kernel_config_tools.kernel_config(
                pool, _ctx(), "not-a-uuid", store_factory=lambda: _FakeStore()
            )
        assert resp.error_category is not None

    asyncio.run(_run())
