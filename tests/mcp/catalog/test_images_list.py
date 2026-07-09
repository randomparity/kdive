"""``images.list`` read tool: RBAC-filtered to public + caller's projects' private rows.

The read tool is the ``kdivectl images list`` server seam. A caller sees every public
catalog image plus the private images owned by the projects granted to their token, and
never another project's private image (the isolation the spec's exit criterion 3 asserts).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg.types.json import Jsonb
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


def _member_ctx(*projects: str) -> RequestContext:
    return RequestContext(
        principal="dev-1",
        agent_session="sess-1",
        projects=tuple(projects),
        roles={},
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
) -> None:
    # A `defined` row is object-less by design (image_object_present CHECK); only a
    # built (pending/registered) row carries an object_key + digest.
    key = None if state == "defined" else f"images/local-libvirt/{name}/x86_64.qcow2"
    digest = None if state == "defined" else "sha256:abc"
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
            " expires_at, state, pending_since) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(key)s, "
            " %(digest)s, %(vis)s, %(owner)s, "
            " CASE WHEN %(vis)s = 'private' THEN now() + interval '1 hour' ELSE NULL END, "
            " %(state)s, now())",
            {
                "name": name,
                "key": key,
                "digest": digest,
                "vis": visibility,
                "owner": owner,
                "state": state,
            },
        )


async def _insert_staged(pool: AsyncConnectionPool, *, name: str, volume: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, volume, visibility, owner, "
            " state, pending_since) "
            "VALUES ('remote-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(volume)s, "
            " 'public', NULL, 'registered', now())",
            {"name": name, "volume": volume},
        )


async def _insert_staged_path(pool: AsyncConnectionPool, *, name: str, path: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, path, visibility, owner, "
            " state, pending_since) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(path)s, "
            " 'public', NULL, 'registered', now())",
            {"name": name, "path": path},
        )


async def _insert_characterized(
    pool: AsyncConnectionPool,
    *,
    name: str,
    capabilities: list[str],
    provenance: dict[str, object],
    description: str | None,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
            " state, pending_since, capabilities, provenance, description) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(key)s, "
            " 'sha256:abc', 'public', NULL, 'registered', now(), %(caps)s, %(prov)s, %(desc)s)",
            {
                "name": name,
                "key": f"images/local-libvirt/{name}/x86_64.qcow2",
                "caps": capabilities,
                "prov": Jsonb(provenance),
                "desc": description,
            },
        )


def _item(resp: object, name: str) -> Any:
    for item in getattr(resp, "items", []):
        if item.data["name"] == name:
            return item
    raise AssertionError(f"{name} not in listing")


def _names(resp: object) -> set[str]:
    items = getattr(resp, "items", [])
    return {str(item.data["name"]) for item in items}


def _volume_of(resp: object, name: str) -> str:
    for item in getattr(resp, "items", []):
        if item.data["name"] == name:
            return str(item.data["volume"])
    raise AssertionError(f"{name} not in listing")


def test_list_carries_staged_volume_token(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_staged(pool, name="fedora-remote", volume="fedora-remote.qcow2")
            await _insert(pool, name="local-s3", visibility="public", owner=None)
            resp = await catalog_images.list_images(pool, _ctx())
        assert _volume_of(resp, "fedora-remote") == "fedora-remote.qcow2"
        assert _volume_of(resp, "local-s3") == ""  # no staged volume -> empty string

    asyncio.run(_run())


def test_list_surfaces_staged_path_without_leaking_path(migrated_url: str) -> None:
    # A staged-path image is listed (discoverable by name) but its absolute host path is never
    # projected into the response envelope (no-leak, ADR-0123/0228).
    secret = "/var/lib/kdive/rootfs/secret-local.qcow2"

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_staged_path(pool, name="local-rootfs", path=secret)
            resp = await catalog_images.list_images(pool, _ctx())
        assert "local-rootfs" in _names(resp)
        item = next(i for i in resp.items if i.data["name"] == "local-rootfs")
        assert "path" not in item.data
        assert secret not in str(resp.model_dump())

    asyncio.run(_run())


def test_list_returns_public_and_own_private_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="fedora", visibility="public", owner=None)
            await _insert(pool, name="mine", visibility="private", owner="proj-a")
            await _insert(pool, name="theirs", visibility="private", owner="proj-b")
            resp = await catalog_images.list_images(pool, _ctx("proj-a"))
        assert resp.status == "ok"
        assert _names(resp) == {"fedora", "mine"}

    asyncio.run(_run())


def test_list_hides_private_images_without_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="mine", visibility="private", owner="proj-a")
            resp = await catalog_images.list_images(pool, _member_ctx("proj-a"))
        assert _names(resp) == set()

    asyncio.run(_run())


def test_list_excludes_other_projects_private(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="theirs", visibility="private", owner="proj-b")
            resp = await catalog_images.list_images(pool, _ctx("proj-a"))
        assert _names(resp) == set()

    asyncio.run(_run())


def test_list_no_projects_sees_only_public(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="fedora", visibility="public", owner=None)
            await _insert(pool, name="mine", visibility="private", owner="proj-a")
            resp = await catalog_images.list_images(pool, _ctx())
        assert _names(resp) == {"fedora"}

    asyncio.run(_run())


def test_list_includes_pending_and_defined_states(migrated_url: str) -> None:
    # The operator list surfaces every catalog row regardless of publish state (a
    # defined baseline / a pending publish), unlike resolve_rootfs which returns only
    # registered rows.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="baseline", visibility="public", owner=None, state="defined")
            resp = await catalog_images.list_images(pool, _ctx())
        assert "baseline" in _names(resp)

    asyncio.run(_run())


def test_list_paginates_with_natural_key_cursor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(5):
                await _insert(pool, name=f"img-{i}", visibility="public", owner=None)
            seen: set[str] = set()
            cursor: str | None = None
            for _ in range(10):
                page = await catalog_images.list_images(pool, _ctx(), limit=2, cursor=cursor)
                seen |= _names(page)
                if not page.data["truncated"]:
                    break
                nxt = page.data["next_cursor"]
                assert isinstance(nxt, str)
                cursor = nxt
        assert seen == {f"img-{i}" for i in range(5)}

    asyncio.run(_run())


def test_list_no_truncation_at_exactly_limit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="a", visibility="public", owner=None)
            await _insert(pool, name="b", visibility="public", owner=None)
            resp = await catalog_images.list_images(pool, _ctx(), limit=2)
        assert resp.data["truncated"] is False
        assert resp.data["next_cursor"] is None

    asyncio.run(_run())


def test_list_malformed_cursor_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await catalog_images.list_images(pool, _ctx(), cursor="!!!")
        assert resp.status == "error"
        assert resp.data["reason"] == "invalid_cursor"

    asyncio.run(_run())


def test_compact_os_full() -> None:
    prov = {"os_release": {"id": "fedora", "version_id": "43", "pretty_name": "Fedora Linux 43"}}
    assert catalog_images._compact_os(prov) == {"id": "fedora", "version_id": "43"}


def test_compact_os_id_only() -> None:
    assert catalog_images._compact_os({"os_release": {"id": "debian"}}) == {"id": "debian"}


def test_compact_os_absent_returns_empty() -> None:
    assert catalog_images._compact_os({}) == {}
    assert catalog_images._compact_os({"os_release": None}) == {}
    assert catalog_images._compact_os({"os_release": "not-a-dict"}) == {}


def test_compact_os_no_id_returns_empty() -> None:
    # A record without a distro id is not a usable identity; never emit a bare version.
    assert catalog_images._compact_os({"os_release": {"version_id": "43"}}) == {}


def test_list_row_carries_capabilities_os_description(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_characterized(
                pool,
                name="fedora-kdive-ready-43",
                capabilities=["kdump", "drgn", "ssh"],
                provenance={
                    "os_release": {"id": "fedora", "version_id": "43"},
                    "default_kernel_version": "6.19.10-300.fc44.x86_64",
                },
                description="RHEL-family debug host",
            )
            resp = await catalog_images.list_images(pool, _ctx())
        data = _item(resp, "fedora-kdive-ready-43").data
        assert data["capabilities"] == ["kdump", "drgn", "ssh"]
        assert data["os"] == {"id": "fedora", "version_id": "43"}
        assert data["default_kernel_version"] == "6.19.10-300.fc44.x86_64"
        assert data["description"] == "RHEL-family debug host"

    asyncio.run(_run())


def test_list_row_omits_os_and_empties_description_when_unset(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_characterized(
                pool,
                name="bare-image",
                capabilities=[],
                provenance={},
                description=None,
            )
            resp = await catalog_images.list_images(pool, _ctx())
        data = _item(resp, "bare-image").data
        assert data["capabilities"] == []
        assert data["os"] == {}
        assert data["default_kernel_version"] == ""
        assert data["description"] == ""

    asyncio.run(_run())
