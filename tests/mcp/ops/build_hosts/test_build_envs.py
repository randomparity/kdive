"""``build_envs.list`` projection handler and exposure classification (#778, ADR-0241).

Coverage:
* Two seeded hosts (ssh with toolchain_desc, ephemeral_libvirt without) → the projection
  returns exactly {name, kind, toolchain_desc, enabled} per env.
* The desc-less host has ``toolchain_desc is None``.
* The secret keys ``address``, ``ssh_credential_ref``, and ``base_image_volume`` are ABSENT
  from every projected item.
* ``build_envs.list`` requires ``project_contributor`` scope (not viewer).
* A viewer-scoped context does NOT see ``build_envs.list``.
"""

from __future__ import annotations

import asyncio
from typing import cast

import psycopg

from kdive.mcp.exposure import ExposureScope, required_scopes, tool_visible
from kdive.mcp.tools.ops.build_hosts.build_envs import list_build_envs
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.serialization import JsonValue

_SECRET_KEYS = {"address", "ssh_credential_ref", "base_image_volume"}
_EXPECTED_KEYS = {"name", "kind", "toolchain_desc", "enabled"}

_TOOL = "build_envs.list"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contributor_ctx() -> RequestContext:
    return RequestContext(
        principal="dev-user",
        agent_session="sess-dev",
        projects=("proj-a",),
        roles={"proj-a": Role.CONTRIBUTOR},
        platform_roles=frozenset(),
        client_id=None,
    )


def _viewer_ctx() -> RequestContext:
    return RequestContext(
        principal="viewer-user",
        agent_session="sess-viewer",
        projects=("proj-a",),
        roles={"proj-a": Role.VIEWER},
        platform_roles=frozenset(),
        client_id=None,
    )


async def _seed_hosts(url: str) -> None:
    """Insert one ssh host (with toolchain_desc) and one ephemeral_libvirt (without)."""
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn:
        # SSH host with a toolchain description
        await conn.execute(
            "INSERT INTO build_hosts "
            "  (name, kind, address, ssh_credential_ref, workspace_root, max_concurrent, "
            "   toolchain_desc) "
            "VALUES (%s, 'ssh', '10.0.0.2', 'ssh://build/key', '/build', 2, %s)",
            ("ssh-builder", "gcc13, binutils2.41; suits fedora39/6.5"),
        )
        # Ephemeral-libvirt host without a toolchain description
        await conn.execute(
            "INSERT INTO build_hosts "
            "  (name, kind, base_image_volume, workspace_root, max_concurrent) "
            "VALUES (%s, 'ephemeral_libvirt', 'kdive-build-base.qcow2', '/build', 1)",
            ("eph-builder",),
        )


# ---------------------------------------------------------------------------
# Projection tests
# ---------------------------------------------------------------------------


def test_list_build_envs_projection(migrated_url: str) -> None:
    """The handler returns the four-key projection and hides secret fields."""

    async def _run() -> None:
        await _seed_hosts(migrated_url)
        conn = await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)
        async with conn:
            resp = await list_build_envs(conn)

        assert resp.status == "ok"
        raw_envs = resp.data["build_envs"]
        assert isinstance(raw_envs, list)
        envs = cast(list[dict[str, JsonValue]], raw_envs)
        # Seeded two rows plus the built-in worker-local seed → at least 2 of ours
        env_by_name = {cast(str, e["name"]): e for e in envs}
        assert "ssh-builder" in env_by_name
        assert "eph-builder" in env_by_name

        ssh_env = env_by_name["ssh-builder"]
        eph_env = env_by_name["eph-builder"]

        # Verify the four expected keys are present for each
        for env in (ssh_env, eph_env):
            assert set(env.keys()) == _EXPECTED_KEYS, f"unexpected keys in {env!r}"

        # Verify no secret keys leak into any projected item
        for env in envs:
            for secret_key in _SECRET_KEYS:
                assert secret_key not in env, f"secret key {secret_key!r} leaked into {env!r}"

        # SSH host has a toolchain_desc
        assert ssh_env["toolchain_desc"] == "gcc13, binutils2.41; suits fedora39/6.5"
        assert ssh_env["kind"] == "ssh"
        assert ssh_env["enabled"] is True

        # Ephemeral host has no toolchain_desc
        assert eph_env["toolchain_desc"] is None
        assert eph_env["kind"] == "ephemeral_libvirt"

    asyncio.run(_run())


def test_list_build_envs_each_item_has_exactly_four_keys(migrated_url: str) -> None:
    """Every projected item has EXACTLY the four expected keys — no extras, no fewer."""

    async def _run() -> None:
        await _seed_hosts(migrated_url)
        conn = await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)
        async with conn:
            resp = await list_build_envs(conn)

        raw_envs = resp.data["build_envs"]
        assert isinstance(raw_envs, list)
        for item in cast(list[dict[str, JsonValue]], raw_envs):
            assert set(item.keys()) == _EXPECTED_KEYS, f"unexpected keys in {item!r}"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Exposure classification tests
# ---------------------------------------------------------------------------


def test_build_envs_list_requires_contributor_scope() -> None:
    """``build_envs.list`` must be classified as PROJECT_CONTRIBUTOR."""
    scopes = required_scopes(_TOOL)
    assert scopes == frozenset({ExposureScope.PROJECT_CONTRIBUTOR})


def test_build_envs_list_visible_to_contributor() -> None:
    contributor = _contributor_ctx()
    assert tool_visible(_TOOL, contributor)


def test_build_envs_list_not_visible_to_viewer() -> None:
    """A project viewer does NOT see ``build_envs.list``."""
    viewer = _viewer_ctx()
    assert not tool_visible(_TOOL, viewer)
