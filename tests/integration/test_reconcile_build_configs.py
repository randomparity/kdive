"""DB-backed tests for the [[build_config]] reconcile pass (ADR-0122)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import cast

from psycopg_pool import AsyncConnectionPool

import kdive.artifacts.storage as _art
from kdive.build_configs.catalog import (
    read_build_config_provenance,
    upsert_operator_build_config,
    upsert_seed_build_config,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile.build_configs import reconcile_build_configs

_KEY = "system/build-configs/kdump/kdump.config"


class _FakeStore:
    """A publish-capable store double recording PUT bytes by key (head + put + get)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def head_present(self, key: str) -> bool:
        return key in self.objects

    def put_artifact(self, request: object) -> object:
        req = cast(_art.ArtifactWriteRequest, request)
        key = req.key()
        self.objects[key] = req.data
        return _art.StoredArtifact(
            key=key,
            etag="fake-etag",
            sensitivity=Sensitivity.REDACTED,
            retention_class="build-config",
        )


class _HeadOnlyStore:
    """A store that can presence-check but not publish (the no-S3 degrade case)."""

    def head_present(self, key: str) -> bool:
        return False


def _doc(name: str, content: str, description: str = "") -> InventoryDoc:
    return InventoryDoc.parse(
        {
            "schema_version": 2,
            "build_config": [{"name": name, "content": content, "description": description}],
        }
    )


def test_create_publishes_and_writes_config(migrated_url: str) -> None:
    async def _run() -> None:
        store = _FakeStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_build_configs(conn, _doc("kdump", "y\n", "d"), store)
            prov = await read_build_config_provenance(conn, "kdump")
        assert [r.name for r in diff.created] == ["kdump"]
        assert prov == (hashlib.sha256(b"y\n").hexdigest(), "config", "d")
        assert store.objects[_KEY] == b"y\n"

    asyncio.run(_run())


def test_identical_reassert_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        store = _FakeStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_build_configs(conn, _doc("kdump", "y\n", "d"), store)
            diff = await reconcile_build_configs(conn, _doc("kdump", "y\n", "d"), store)
        assert diff.created == [] and diff.updated == [] and diff.warned == []

    asyncio.run(_run())


def test_description_only_edit_reasserts_without_warn(migrated_url: str) -> None:
    async def _run() -> None:
        store = _FakeStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_build_configs(conn, _doc("kdump", "y\n", "old"), store)
            diff = await reconcile_build_configs(conn, _doc("kdump", "y\n", "new"), store)
            prov = await read_build_config_provenance(conn, "kdump")
        assert [r.name for r in diff.updated] == ["kdump"]
        assert diff.warned == []
        assert prov is not None and prov[2] == "new"

    asyncio.run(_run())


def test_reassert_over_operator_warns(migrated_url: str) -> None:
    async def _run() -> None:
        store = _FakeStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await upsert_operator_build_config(conn, "kdump", "k", "opsha", "od")
            diff = await reconcile_build_configs(conn, _doc("kdump", "y\n", "d"), store)
            prov = await read_build_config_provenance(conn, "kdump")
        assert [r.name for r in diff.warned] == ["kdump"]
        assert prov is not None and prov[1] == "config"

    asyncio.run(_run())


def test_benign_seed_adoption_does_not_warn(migrated_url: str) -> None:
    async def _run() -> None:
        store = _FakeStore()
        sha = hashlib.sha256(b"y\n").hexdigest()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await upsert_seed_build_config(conn, "kdump", "k", sha, "d")
            diff = await reconcile_build_configs(conn, _doc("kdump", "y\n", "d"), store)
            prov = await read_build_config_provenance(conn, "kdump")
        assert diff.warned == []
        assert prov is not None and prov[1] == "config"

    asyncio.run(_run())


def test_over_cap_skips_with_warn(migrated_url: str, monkeypatch) -> None:  # noqa: ANN001
    async def _run() -> None:
        store = _FakeStore()
        monkeypatch.setenv("KDIVE_MAX_BUILD_CONFIG_BYTES", "5")
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_build_configs(conn, _doc("kdump", "way too long content"), store)
            prov = await read_build_config_provenance(conn, "kdump")
        assert [r.name for r in diff.warned] == ["kdump"]
        assert prov is None  # never published
        assert store.objects == {}

    asyncio.run(_run())


def test_store_cannot_publish_degrades(migrated_url: str) -> None:
    async def _run() -> None:
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_build_configs(conn, _doc("kdump", "y\n"), _HeadOnlyStore())
            prov = await read_build_config_provenance(conn, "kdump")
        assert [r.name for r in diff.warned] == ["kdump"]
        assert prov is None

    asyncio.run(_run())


def test_reconcile_all_publishes_build_config_through_pipeline(migrated_url: str) -> None:
    """The full reconcile_all pipeline threads the store into the build-config pass and publishes.

    Covers the wiring seam (reconcile_all -> reconcile_build_configs(store)) end-to-end, not just
    the pass in isolation: a doc carrying a [[build_config]] reconciled via reconcile_all must
    land the catalog row source='config' with the object bytes present at the reserved key.
    """
    from kdive.inventory.reconcile.pipeline import reconcile_all

    async def _run() -> None:
        store = _FakeStore()
        doc = InventoryDoc.parse(
            {
                "schema_version": 2,
                "build_config": [
                    {"name": "kdump", "content": "CONFIG_KEXEC=y\n", "description": "d"}
                ],
            }
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_all(conn, doc, store)
            prov = await read_build_config_provenance(conn, "kdump")
        assert [r.name for r in diff.created] == ["kdump"]
        assert prov == (hashlib.sha256(b"CONFIG_KEXEC=y\n").hexdigest(), "config", "d")
        assert store.objects[_KEY] == b"CONFIG_KEXEC=y\n"

    asyncio.run(_run())
