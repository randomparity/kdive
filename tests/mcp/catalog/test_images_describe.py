"""``images.describe`` read tool: per-image detail addressed by row id (ADR-0252).

Mirrors ``resources.describe``: a caller sees a public row or a private row owned by a project
their token grants ``viewer``; a valid-but-invisible id and an unknown id both return a
byte-identical ``not_found`` (no existence/membership leak), and a malformed id is a
``configuration_error``. The staged ``path`` and the S3 ``object_key`` are withheld.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.catalog import images as catalog_images
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


async def _insert(
    pool: AsyncConnectionPool,
    *,
    name: str,
    visibility: str,
    owner: str | None,
    state: str = "registered",
) -> str:
    key = None if state == "defined" else f"images/local-libvirt/{name}/x86_64.qcow2"
    digest = None if state == "defined" else "sha256:abc"
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, digest, capabilities, "
            " provenance, visibility, owner, expires_at, state, pending_since) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(key)s, "
            " %(digest)s, '{}', '{}', %(vis)s, %(owner)s, "
            " CASE WHEN %(vis)s = 'private' THEN now() + interval '1 hour' ELSE NULL END, "
            " %(state)s, now()) RETURNING id",
            {
                "name": name,
                "key": key,
                "digest": digest,
                "vis": visibility,
                "owner": owner,
                "state": state,
            },
        )
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _insert_staged_path(pool: AsyncConnectionPool, *, name: str, path: str) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, path, visibility, owner, "
            " state, pending_since) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(path)s, "
            " 'public', NULL, 'registered', now()) RETURNING id",
            {"name": name, "path": path},
        )
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


def test_describe_public_row_carries_full_detail(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            iid = await _insert(pool, name="fedora", visibility="public", owner=None)
            resp = await catalog_images.describe_image(pool, _ctx(), iid)
        assert resp.status == "registered"
        d = resp.data
        assert d["name"] == "fedora" and d["format"] == "qcow2"
        assert d["root_device"] == "/dev/vda" and d["digest"] == "sha256:abc"
        assert d["capabilities"] == [] and d["provenance"] == {}
        assert d["managed_by"] == "runtime" and d["visibility"] == "public"
        assert "object_key" not in d and "path" not in d

    asyncio.run(_run())


def test_describe_owned_private_visible(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            iid = await _insert(pool, name="mine", visibility="private", owner="proj-a")
            resp = await catalog_images.describe_image(pool, _ctx("proj-a"), iid)
        assert resp.status != "error"
        assert resp.data["name"] == "mine"
        assert resp.data["owner"] == "proj-a"
        assert resp.data["expires_at"] != ""  # private rows carry an ISO reclaim deadline

    asyncio.run(_run())


def test_describe_unauthorized_private_is_not_found_no_leak(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            iid = await _insert(pool, name="theirs", visibility="private", owner="proj-b")
            visible = await catalog_images.describe_image(pool, _ctx("proj-a"), iid)
            unknown = await catalog_images.describe_image(
                pool, _ctx("proj-a"), "00000000-0000-0000-0000-000000000000"
            )
        assert visible.status == "error" and visible.error_category == "not_found"
        # Byte-identical to a genuinely-unknown id (excluding the echoed object_id).
        assert visible.model_dump(exclude={"object_id"}) == unknown.model_dump(
            exclude={"object_id"}
        )

    asyncio.run(_run())


def test_describe_unknown_id_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await catalog_images.describe_image(
                pool, _ctx(), "11111111-1111-1111-1111-111111111111"
            )
        assert resp.status == "error" and resp.error_category == "not_found"

    asyncio.run(_run())


def test_describe_malformed_id_is_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await catalog_images.describe_image(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_describe_normalizes_noncanonical_uuid_forms(migrated_url: str) -> None:
    # UUID() accepts urn:/brace/unhyphenated forms PostgreSQL's uuid input rejects; the handler
    # must normalize to the canonical form before the query, not raise an uncaught DataError.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            iid = await _insert(pool, name="fedora", visibility="public", owner=None)
            bare = iid.replace("-", "")
            by_urn = await catalog_images.describe_image(pool, _ctx(), f"urn:uuid:{iid}")
            by_braces = await catalog_images.describe_image(pool, _ctx(), f"{{{iid}}}")
            by_bare = await catalog_images.describe_image(pool, _ctx(), bare)
        for resp in (by_urn, by_braces, by_bare):
            assert resp.status != "error", "a non-canonical but valid UUID resolves the same row"
            assert resp.data["name"] == "fedora"

    asyncio.run(_run())


def test_describe_withholds_staged_path(migrated_url: str) -> None:
    secret = "/var/lib/kdive/rootfs/secret-local.qcow2"

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            iid = await _insert_staged_path(pool, name="local-rootfs", path=secret)
            resp = await catalog_images.describe_image(pool, _ctx(), iid)
        assert resp.status != "error"
        assert "path" not in resp.data
        assert secret not in str(resp.model_dump())

    asyncio.run(_run())
