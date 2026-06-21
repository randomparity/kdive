"""Unit tests for the inventory-override ledger repository (ADR-0199, #638).

Drives the repository helpers directly against a disposable migrated Postgres (ADR-0019),
covering each helper's happy path and its empty / absent / conflict edge.
"""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.inventory.overrides import (
    BUILD_HOST_RESOURCE_KIND,
    InventoryOverrideDisposition,
    InventorySourceKind,
    OverrideIdentity,
    clear_override,
    lookup,
    lookup_many,
    set_override,
)

_RESOURCE = InventorySourceKind.RESOURCE
_BUILD_HOST = InventorySourceKind.BUILD_HOST


def _resource_identity(name: str, kind: str = "remote-libvirt") -> OverrideIdentity:
    return OverrideIdentity(source_kind=_RESOURCE, resource_kind=kind, name=name)


def _build_host_identity(name: str) -> OverrideIdentity:
    return OverrideIdentity(
        source_kind=_BUILD_HOST, resource_kind=BUILD_HOST_RESOURCE_KIND, name=name
    )


def test_set_then_lookup_round_trips(migrated_url: str) -> None:
    identity = _resource_identity("h1")

    async def _run() -> None:
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await set_override(
                conn,
                identity,
                disposition=InventoryOverrideDisposition.REMOVED,
                reason="operator removed",
                actor="alice",
            )
            found = await lookup(conn, identity)
        assert found is not None
        assert found.disposition is InventoryOverrideDisposition.REMOVED
        assert found.reason == "operator removed"
        assert found.actor == "alice"
        assert found.source_kind is _RESOURCE
        assert found.resource_kind == "remote-libvirt"
        assert found.name == "h1"

    asyncio.run(_run())


def test_lookup_absent_identity_is_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            assert await lookup(conn, _resource_identity("nope")) is None

    asyncio.run(_run())


def test_set_is_idempotent_on_conflict(migrated_url: str) -> None:
    # A re-set for the same identity replaces the disposition/reason/actor in place — no
    # duplicate-key error (a remove-then-re-remove is idempotent).
    identity = _resource_identity("h1")

    async def _run() -> None:
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await set_override(
                conn,
                identity,
                disposition=InventoryOverrideDisposition.DETACHED,
                reason="first",
                actor="alice",
            )
            await set_override(
                conn,
                identity,
                disposition=InventoryOverrideDisposition.REMOVED,
                reason="second",
                actor="bob",
            )
            found = await lookup(conn, identity)
            count = await conn.execute("SELECT count(*) FROM inventory_overrides")
            total = await count.fetchone()
        assert total is not None and total[0] == 1
        assert found is not None
        assert found.disposition is InventoryOverrideDisposition.REMOVED
        assert found.reason == "second"
        assert found.actor == "bob"

    asyncio.run(_run())


def test_clear_returns_true_then_false(migrated_url: str) -> None:
    identity = _resource_identity("h1")

    async def _run() -> None:
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await set_override(
                conn,
                identity,
                disposition=InventoryOverrideDisposition.REMOVED,
                reason="r",
                actor="a",
            )
            first = await clear_override(conn, identity)
            second = await clear_override(conn, identity)
            assert await lookup(conn, identity) is None
        assert first is True
        assert second is False

    asyncio.run(_run())


def test_lookup_many_filters_by_source_kind_and_keys_correctly(migrated_url: str) -> None:
    async def _run() -> None:
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await set_override(
                conn,
                _resource_identity("h1", kind="remote-libvirt"),
                disposition=InventoryOverrideDisposition.REMOVED,
                reason="r",
                actor="a",
            )
            await set_override(
                conn,
                _resource_identity("h1", kind="fault-inject"),
                disposition=InventoryOverrideDisposition.DETACHED,
                reason="r",
                actor="a",
            )
            await set_override(
                conn,
                _build_host_identity("bh1"),
                disposition=InventoryOverrideDisposition.REMOVED,
                reason="r",
                actor="a",
            )
            resources = await lookup_many(conn, _RESOURCE)
            build_hosts = await lookup_many(conn, _BUILD_HOST)
        # Same name across two resource kinds coexists, keyed by (resource_kind, name).
        assert set(resources) == {("remote-libvirt", "h1"), ("fault-inject", "h1")}
        assert resources[("remote-libvirt", "h1")].disposition is (
            InventoryOverrideDisposition.REMOVED
        )
        assert resources[("fault-inject", "h1")].disposition is (
            InventoryOverrideDisposition.DETACHED
        )
        # The build-host family is filtered out of the resource lookup, and vice versa.
        assert set(build_hosts) == {(BUILD_HOST_RESOURCE_KIND, "bh1")}

    asyncio.run(_run())


def test_disposition_check_rejects_unknown_at_repo_boundary(migrated_url: str) -> None:
    # A raw INSERT with a bad disposition is rejected by the SQL CHECK; the enum-typed repo API
    # cannot express it, so this asserts the table-level guard the repo relies on.
    async def _run() -> None:
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO inventory_overrides (source_kind, resource_kind, name, "
                    "disposition, reason, actor) "
                    "VALUES ('resource', 'remote-libvirt', 'h', 'bogus', 'r', 'a')"
                )

    asyncio.run(_run())
