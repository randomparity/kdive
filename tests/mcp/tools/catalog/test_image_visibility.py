"""Direct tests for shared image visibility lookup helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.catalog.image_visibility import default_kernel_version, fetch_visible_image
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role


def _ctx(*projects: str) -> RequestContext:
    return RequestContext(
        principal="dev-1",
        agent_session="sess-1",
        projects=tuple(projects),
        roles={project: Role.VIEWER for project in projects},
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


async def _insert_image(
    pool: AsyncConnectionPool, *, name: str, visibility: str, owner: str | None
) -> UUID:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
            " expires_at, state, pending_since, provenance) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(key)s, "
            " 'sha256:abc', %(visibility)s, %(owner)s, "
            " CASE WHEN %(visibility)s = 'private' THEN now() + interval '1 hour' ELSE NULL END, "
            " 'registered', now(), %(provenance)s) RETURNING id",
            {
                "name": name,
                "key": f"images/local-libvirt/{name}/x86_64.qcow2",
                "visibility": visibility,
                "owner": owner,
                "provenance": Jsonb({"default_kernel_version": f"{name}-kernel"}),
            },
        )
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


def test_fetch_visible_image_filters_public_and_private_rows(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            public_id = await _insert_image(pool, name="public", visibility="public", owner=None)
            owned_id = await _insert_image(pool, name="owned", visibility="private", owner="proj-a")
            hidden_id = await _insert_image(
                pool, name="hidden", visibility="private", owner="proj-b"
            )

            public = await fetch_visible_image(pool, _ctx("proj-a"), public_id)
            owned = await fetch_visible_image(pool, _ctx("proj-a"), owned_id)
            hidden = await fetch_visible_image(pool, _ctx("proj-a"), hidden_id)

        assert public is not None and public.name == "public"
        assert owned is not None and owned.name == "owned"
        assert hidden is None

    asyncio.run(_run())


def test_fetch_visible_image_empty_results_and_missing_kernel_version(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            absent = await fetch_visible_image(
                pool, _ctx(), UUID("00000000-0000-0000-0000-000000000000")
            )
        assert absent is None
        assert default_kernel_version({}) == ""

    asyncio.run(_run())
