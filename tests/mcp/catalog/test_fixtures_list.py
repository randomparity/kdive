"""``fixtures.list`` — provider-organized rootfs catalog read (#252, ADR-0089 §6, ADR-0112).

A plain authenticated read (no platform gate, no per-tool audit): the fixture catalog is the
provider-organized rootfs inventory, not secret content. The catalog now lives in the DB-backed
``image_catalog`` (ADR-0112 removed the packaged ``seed_data`` YAML); this read reports the
public catalog rows. Coverage:

* it flattens each public catalog row into ``{provider, name, arch}``;
* an empty catalog yields an empty list (no crash);
* a private (owner-scoped) image is NOT surfaced — only the public baseline is.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.catalog import fixtures
from tests.mcp.json_data import data_sequence, json_mapping


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=5, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_image(
    conn: AsyncConnection, *, provider: str, name: str, visibility: str, owner: str | None
) -> None:
    # A private image carries an expires_at (DB CHECK image_private_expiry); a public one does not.
    expires_at = None if owner is None else "now() + interval '1 day'"
    await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, capabilities, provenance, "
        " visibility, owner, expires_at, state, managed_by) "
        f"VALUES (%s, %s, 'x86_64', 'qcow2', '/dev/vda', '{{}}', '{{}}', %s, %s, "
        f"{'NULL' if expires_at is None else expires_at}, 'defined', %s)",
        (provider, name, visibility, owner, "config" if owner is None else "runtime"),
    )


async def _insert_staged(conn: AsyncConnection, *, name: str, volume: str) -> None:
    await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, volume, capabilities, provenance, "
        " visibility, owner, state, managed_by) "
        "VALUES ('remote-libvirt', %s, 'x86_64', 'qcow2', '/dev/vda', %s, '{}', '{}', "
        " 'public', NULL, 'registered', 'config')",
        (name, volume),
    )


def test_fixtures_carry_staged_volume(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _insert_staged(conn, name="fedora-remote", volume="fedora-remote.qcow2")
            resp = await fixtures.list_fixtures_tool(pool)
        rows = [json_mapping(row) for row in data_sequence(resp, "fixtures")]
        match = next(row for row in rows if row["name"] == "fedora-remote")
        assert match["volume"] == "fedora-remote.qcow2"

    asyncio.run(_run())


def test_lists_public_catalog_entries(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _insert_image(
                    conn, provider="local-libvirt", name="base", visibility="public", owner=None
                )
                await _insert_image(
                    conn, provider="local-libvirt", name="cloud", visibility="public", owner=None
                )
            resp = await fixtures.list_fixtures_tool(pool)
        assert resp.status == "ok"
        rows = [json_mapping(row) for row in data_sequence(resp, "fixtures")]
        names = {row["name"] for row in rows}
        assert {"base", "cloud"} <= names
        assert all(row["provider"] == "local-libvirt" for row in rows)

    asyncio.run(_run())


def test_empty_catalog_yields_empty_list(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await fixtures.list_fixtures_tool(pool)
        assert resp.status == "ok"
        assert data_sequence(resp, "fixtures") == []

    asyncio.run(_run())


def test_private_image_is_not_surfaced(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _insert_image(
                    conn, provider="local-libvirt", name="pub", visibility="public", owner=None
                )
                await _insert_image(
                    conn, provider="local-libvirt", name="priv", visibility="private", owner="p1"
                )
            resp = await fixtures.list_fixtures_tool(pool)
        rows = [json_mapping(row) for row in data_sequence(resp, "fixtures")]
        names = {row["name"] for row in rows}
        assert "pub" in names
        assert "priv" not in names, "a private image is never a baseline fixture"

    asyncio.run(_run())
