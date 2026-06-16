"""buildconfig.get tool tests — read_build_config called directly with DB pool + object store."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH, seed_build_configs
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog import build_configs
from kdive.mcp.tools.catalog.build_configs import read_build_config, set_build_config
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole
from kdive.store.objectstore import ObjectStore

_PLATFORM_ADMIN = RequestContext(
    principal="op-1",
    agent_session="sess-1",
    projects=(),
    roles={},
    platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
)
_PLATFORM_OPERATOR = RequestContext(
    principal="op-1",
    agent_session="sess-1",
    projects=(),
    roles={},
    platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def test_buildconfig_get_returns_inline_bytes_and_sha(
    migrated_url: str, minio_store: ObjectStore
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await seed_build_configs(conn, minio_store)

            data = KDUMP_FRAGMENT_PATH.read_bytes()

            async with pool.connection() as conn:
                resp = await read_build_config(conn, minio_store, name="kdump")

        assert resp.status == "available"
        assert resp.data["content"] == data.decode()
        assert resp.data["sha256"] == hashlib.sha256(data).hexdigest()
        assert "merge_config.sh -m" in str(resp.data["merge_recipe"])

    asyncio.run(_run())


def test_buildconfig_get_unknown_name_is_configuration_error(
    migrated_url: str, minio_store: ObjectStore
) -> None:
    caught: list[CategorizedError] = []

    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            with pytest.raises(CategorizedError) as exc:
                await read_build_config(conn, minio_store, name="nope")
            caught.append(exc.value)

    asyncio.run(_run())
    assert caught[0].category is ErrorCategory.CONFIGURATION_ERROR


def test_buildconfig_get_tool_maps_unknown_name_to_failure_envelope(
    migrated_url: str, minio_store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            monkeypatch.setattr(build_configs, "_resolve_store", lambda: minio_store)
            monkeypatch.setattr(
                build_configs,
                "current_context",
                lambda: RequestContext(
                    principal="dev-1",
                    agent_session="sess-dev",
                    projects=(),
                    roles={},
                    platform_roles=frozenset(),
                ),
            )
            app = FastMCP("build-config-test")
            build_configs.register(app, pool)
            tools = {tool.name: tool for tool in await app.list_tools()}
            result = await cast(Any, tools["buildconfig.get"]).fn("nope")

        assert isinstance(result, ToolResponse)
        assert result.object_id == "nope"
        assert result.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert result.suggested_next_actions == ["buildconfig.get"]

    asyncio.run(_run())


def test_buildconfig_get_tool_maps_store_resolution_error(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = CategorizedError("store missing", category=ErrorCategory.CONFIGURATION_ERROR)

    def _raise_store() -> ObjectStore:
        raise error

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            monkeypatch.setattr(build_configs, "_resolve_store", _raise_store)
            monkeypatch.setattr(
                build_configs,
                "current_context",
                lambda: RequestContext(
                    principal="dev-1",
                    agent_session="sess-dev",
                    projects=(),
                    roles={},
                    platform_roles=frozenset(),
                ),
            )
            app = FastMCP("build-config-test")
            build_configs.register(app, pool)
            tools = {tool.name: tool for tool in await app.list_tools()}
            result = await cast(Any, tools["buildconfig.get"]).fn("kdump")

        assert isinstance(result, ToolResponse)
        assert result.object_id == "kdump"
        assert result.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert result.suggested_next_actions == ["buildconfig.get"]

    asyncio.run(_run())


async def _platform_audit_rows(pool: AsyncConnectionPool, tool: str) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, scope, args_digest FROM platform_audit_log WHERE tool = %s",
            (tool,),
        )
        rows = await cur.fetchall()
    return [{"principal": r[0], "scope": r[1], "args_digest": r[2]} for r in rows]


def test_set_publishes_and_get_reports_operator_source(
    migrated_url: str, minio_store: ObjectStore
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await set_build_config(
                pool,
                lambda: minio_store,
                _PLATFORM_ADMIN,
                name="kdump",
                content="CONFIG_X=y\n",
                description="d",
            )
            assert resp.status == "published"
            assert resp.data["source"] == "operator"
            assert resp.data["sha256"] == hashlib.sha256(b"CONFIG_X=y\n").hexdigest()
            assert resp.data["bytes"] == len(b"CONFIG_X=y\n")

            async with pool.connection() as conn:
                got = await read_build_config(conn, minio_store, name="kdump")
            assert got.data["content"] == "CONFIG_X=y\n"
            assert got.data["source"] == "operator"

            audited = await _platform_audit_rows(pool, "buildconfig.set")
        assert len(audited) == 1
        assert audited[0]["scope"] == "kdump"
        assert audited[0]["args_digest"] != "CONFIG_X=y\n"  # digest, not plaintext

    asyncio.run(_run())


def test_set_replaces_bytes_on_second_call(migrated_url: str, minio_store: ObjectStore) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await set_build_config(
                pool,
                lambda: minio_store,
                _PLATFORM_ADMIN,
                name="kdump",
                content="A\n",
                description="d",
            )
            await set_build_config(
                pool,
                lambda: minio_store,
                _PLATFORM_ADMIN,
                name="kdump",
                content="B\n",
                description="",
            )
            async with pool.connection() as conn:
                got = await read_build_config(conn, minio_store, name="kdump")
        assert got.data["content"] == "B\n"
        assert got.data["sha256"] == hashlib.sha256(b"B\n").hexdigest()

    asyncio.run(_run())


def test_set_requires_platform_admin(migrated_url: str, minio_store: ObjectStore) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await set_build_config(
                pool,
                lambda: minio_store,
                _PLATFORM_OPERATOR,
                name="kdump",
                content="x\n",
                description="",
            )
            audited = await _platform_audit_rows(pool, "buildconfig.set")
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        # The denial is audited because the caller holds *some* platform role.
        assert len(audited) == 1
        assert audited[0]["scope"] == "denied:kdump"

    asyncio.run(_run())


def test_set_rejects_bad_name_and_empty_content(
    migrated_url: str, minio_store: ObjectStore
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            bad = await set_build_config(
                pool,
                lambda: minio_store,
                _PLATFORM_ADMIN,
                name="../etc",
                content="x\n",
                description="",
            )
            empty = await set_build_config(
                pool,
                lambda: minio_store,
                _PLATFORM_ADMIN,
                name="kdump",
                content="",
                description="",
            )
            # No row was written for either rejected call.
            async with pool.connection() as conn:
                got = await build_configs.get_build_config(conn, "kdump")
        assert bad.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert bad.data["field"] == "name"
        assert empty.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert empty.data["field"] == "content"
        assert got is None

    asyncio.run(_run())


def test_set_rejects_oversize_content(
    migrated_url: str, minio_store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KDIVE_MAX_BUILD_CONFIG_BYTES", "8")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await set_build_config(
                pool,
                lambda: minio_store,
                _PLATFORM_ADMIN,
                name="kdump",
                content="123456789",
                description="",
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["field"] == "content"
        assert resp.data["limit"] == 8

    asyncio.run(_run())


def test_set_denies_and_audits_before_resolving_store(migrated_url: str) -> None:
    """A non-admin caller is denied + audited even when store resolution would fail.

    The store factory raises (no S3); a denied caller must never reach it, so the result is
    AUTHORIZATION_DENIED (not CONFIGURATION_ERROR) and a denial row is written.
    """
    store_calls: list[int] = []

    def _raising_factory() -> ObjectStore:
        store_calls.append(1)
        raise CategorizedError("no s3", category=ErrorCategory.CONFIGURATION_ERROR)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await set_build_config(
                pool,
                _raising_factory,
                _PLATFORM_OPERATOR,
                name="kdump",
                content="x\n",
                description="",
            )
            audited = await _platform_audit_rows(pool, "buildconfig.set")
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert store_calls == []  # the gate short-circuited before store resolution
        assert len(audited) == 1
        assert audited[0]["scope"] == "denied:kdump"

    asyncio.run(_run())
