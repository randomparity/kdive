"""Integration tests for the inventory reconcile engine (M2.6 #390, ADR-0112).

Exercises ``reconcile_images`` against a disposable migrated Postgres (ADR-0019) plus a
narrow fake object store. Each test encodes one spec invariant from plan Task 1.4:

* 1 — never overwrites a build-realized row's runtime-owned ``object_key``/``digest``/``state``;
* 2/3 — prune touches only ``managed_by='config'`` rows (runtime/private untouched);
* 5 — prune of an in-use image cordons (does not delete the row);
* 7 — the relaxed ``image_object_present`` CHECK rejects both/neither object_key+volume;
* 8 — an ``s3`` source without a digest stays ``defined`` + warns.

Plus: idempotency (a second pass is a clean no-op), the s3 store-unreachable degrade (the
row stays ``defined`` and the pass succeeds rather than aborting), the kind-aware cordon
guard (a live **remote** System on a staged base image cordons, not deletes — Task 1.5,
load-bearing now that ``repair_leaked_images`` GCs an orphaned object after the row is gone),
and the concurrent-pass serialization (two passes do not abort on the identity constraint).

Prune is **row-delete-only** (ADR-0112): reconcile never calls ``store.delete`` — orphaned
objects are reclaimed by the existing ``repair_leaked_images`` reconciler sweep. The fake
store therefore records no ``delete`` calls from a reconcile pass; an asserted empty
``deleted`` list is the regression guard against re-introducing inline reclaim.

Seeding uses an autocommit connection (each insert self-commits); reconcile runs on a
non-autocommit pool connection so the real transaction framing is exercised.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import ObjectListing
from kdive.domain.catalog.images import ImageCatalogEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.capability_signals import render_direct_kernel_signal
from kdive.images.kdump_support import KernelVersion
from kdive.images.staged_provenance import sidecar_path, write_sidecar
from kdive.inventory.loader import load_inventory
from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile.coefficients import reconcile_coefficients
from kdive.inventory.reconcile.images import reconcile_images
from kdive.inventory.reconcile.records import ReconcileDiff
from kdive.inventory.reconcile.resources import reconcile_resources
from kdive.providers.infra.reaping import NullReaper
from kdive.reconciler.inventory import InventoryReconcilePass, _cwd_inventory_shadowed
from kdive.reconciler.loop import ReconcileConfig, reconcile_once
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole

# `migrated_url` is provided as a fixture by tests/integration/conftest.py (re-exported from
# tests.db.conftest), resolved by pytest at call time — no import (avoids the F811 shadow).

# --- fakes / helpers -----------------------------------------------------------------


class _FakeImageStore:
    """A narrow object-store stand-in (structural match for the reconcile store port).

    ``present`` is the set of keys a HEAD reports as existing. ``unreachable=True`` makes
    every ``head_present`` raise the infrastructure error a real store throws when the
    bucket is unconfigured/unreachable (a connection failure, not a clean 404). ``deleted``
    records ``delete`` calls — reconcile must never append to it (prune is row-delete-only).
    """

    def __init__(self, present: set[str] | None = None, *, unreachable: bool = False) -> None:
        self._present = set(present or ())
        self._unreachable = unreachable
        self.deleted: list[str] = []

    def head_present(self, key: str) -> bool:
        if self._unreachable:
            raise CategorizedError(
                "object store unreachable",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        return key in self._present

    def list_image_objects(self) -> list[ObjectListing]:
        # Empty so the loop's sibling image sweeps (leaked/dangling) are clean no-ops when
        # this fake is used as the full ImageSweepStore in the loop-config tests.
        return []

    def delete(self, key: str) -> None:  # pragma: no cover - asserted never called
        self.deleted.append(key)


def _write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "systems.toml"
    path.write_text(body)
    return path


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _one(conn: psycopg.AsyncConnection, name: str) -> dict[str, object]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM image_catalog WHERE name = %s", (name,))
        row = await cur.fetchone()
    assert row is not None, f"no image_catalog row named {name!r}"
    return row


async def _exists(conn: psycopg.AsyncConnection, name: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM image_catalog WHERE name = %s", (name,))
        return await cur.fetchone() is not None


async def _insert_registered_build_row(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    object_key: str,
    digest: str,
    provider: str = "local-libvirt",
    arch: str = "x86_64",
    managed_by: str = "config",
    visibility: str = "public",
    owner: str | None = None,
) -> UUID:
    """Insert a build-realized ``registered`` row (object_key + digest set)."""
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
        " state, managed_by, expires_at) "
        "VALUES (%s, %s, %s, 'qcow2', '/dev/vda', %s, %s, %s, %s, 'registered', %s, %s) "
        "RETURNING id",
        (
            provider,
            name,
            arch,
            object_key,
            digest,
            visibility,
            owner,
            managed_by,
            None if visibility == "public" else "now() + interval '1 day'",
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_config_staged_row(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    volume: str,
    provider: str = "remote-libvirt",
    arch: str = "x86_64",
) -> UUID:
    """Insert a config-owned ``registered`` staged row (volume set, object_key NULL)."""
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, volume, visibility, state, managed_by) "
        "VALUES (%s, %s, %s, 'qcow2', '/dev/vda', %s, 'public', 'registered', 'config') "
        "RETURNING id",
        (provider, name, arch, volume),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_private_upload_row(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    object_key: str,
    provider: str = "local-libvirt",
    arch: str = "x86_64",
    owner: str = "proj",
) -> UUID:
    """Insert a runtime-owned project-private upload sharing an identity with a config image."""
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
        " expires_at, state, managed_by) "
        "VALUES (%s, %s, %s, 'qcow2', '/dev/vda', %s, 'sha256:priv', 'private', %s, "
        " now() + interval '1 day', 'registered', 'runtime') "
        "RETURNING id",
        (provider, name, arch, object_key, owner),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _seed_non_terminal_system(
    conn: psycopg.AsyncConnection, *, provisioning_profile: dict[str, object]
) -> UUID:
    """Insert resource -> allocation -> READY system with the given provisioning_profile."""
    resource_id = uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'p', 'c', 'available', 'qemu:///system')",
        (resource_id,),
    )
    allocation_id = uuid4()
    await conn.execute(
        "INSERT INTO allocations (id, principal, project, resource_id, state) "
        "VALUES (%s, 'alice', 'proj', %s, 'active')",
        (allocation_id, resource_id),
    )
    system_id = uuid4()
    await conn.execute(
        "INSERT INTO systems (id, principal, project, allocation_id, state, provisioning_profile) "
        "VALUES (%s, 'alice', 'proj', %s, 'ready', %s)",
        (system_id, allocation_id, Jsonb(provisioning_profile)),
    )
    return system_id


def _local_catalog_profile(provider: str, name: str) -> dict[str, object]:
    return {
        "version": 1,
        "arch": "x86_64",
        "vcpu": 1,
        "memory_mb": 1024,
        "disk_gb": 10,
        "boot_method": "direct-kernel",
        "provider": {
            "local-libvirt": {"rootfs": {"kind": "catalog", "provider": provider, "name": name}}
        },
    }


def _remote_base_volume_profile(volume: str) -> dict[str, object]:
    return {
        "version": 1,
        "arch": "x86_64",
        "vcpu": 1,
        "memory_mb": 1024,
        "disk_gb": 10,
        "boot_method": "disk-image",
        "provider": {"remote-libvirt": {"base_image_volume": volume}},
    }


# --- tests ---------------------------------------------------------------------------


_STAGED_PATH_HEAD = (
    "schema_version = 2\n"
    "[[image]]\n"
    'provider = "local-libvirt"\n'
    'name = "local-rootfs"\n'
    'arch = "x86_64"\n'
    'format = "qcow2"\n'
    'root_device = "/dev/vda"\n'
    'visibility = "public"\n'
)
_STAGED_PATH_SOURCE = (
    '[image.source]\nkind = "staged-path"\npath = "/var/lib/kdive/rootfs/local-rootfs.qcow2"\n'
)


def _staged_path_toml(description: str | None = None) -> str:
    body = _STAGED_PATH_HEAD
    if description is not None:
        body += f'description = "{description}"\n'
    return body + _STAGED_PATH_SOURCE


def test_reconcile_creates_row_with_description(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _staged_path_toml("RHEL debug + SLES setup")))
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "local-rootfs")
        assert row["description"] == "RHEL debug + SLES setup"

    asyncio.run(_run())


def test_reconcile_updates_then_clears_description(migrated_url: str, tmp_path: Path) -> None:
    async def _reconcile(store: _FakeImageStore, description: str | None) -> None:
        doc = load_inventory(_write_toml(tmp_path, _staged_path_toml(description)))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_images(conn, doc, store)

    async def _description() -> object:
        async with await _connect(migrated_url) as check:
            return (await _one(check, "local-rootfs"))["description"]

    async def _run() -> None:
        store = _FakeImageStore()
        await _reconcile(store, "first")
        await _reconcile(store, "second")
        assert await _description() == "second"
        await _reconcile(store, None)
        assert await _description() is None

    asyncio.run(_run())


def test_reconcile_descriptionless_image_is_idempotent(migrated_url: str, tmp_path: Path) -> None:
    # A description-less image stores NULL while entry.description defaults to ""; without
    # normalization the change-detector would report config_changed on every pass (NULL != "").
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _staged_path_toml(None)))
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_images(conn, doc, store)
            second = await reconcile_images(conn, doc, store)
        assert "local-rootfs" not in {u.name for u in second.updated}

    asyncio.run(_run())


def test_staged_path_image_seeds_registered_with_path(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "local-rootfs"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "staged-path"\n'
                'path = "/var/lib/kdive/rootfs/local-rootfs.qcow2"\n',
            )
        )
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "local-rootfs")
        assert row["state"] == "registered"
        assert row["path"] == "/var/lib/kdive/rootfs/local-rootfs.qcow2"
        assert row["object_key"] is None
        assert row["volume"] is None
        assert row["digest"] is None
        assert row["managed_by"] == "config"
        assert "local-rootfs" in {c.name for c in diff.created}
        assert store.deleted == []

    asyncio.run(_run())


def test_staged_image_registers_with_volume(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "remote-libvirt"\n'
                'name = "base"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "staged"\n'
                'volume = "base.qcow2"\n',
            )
        )
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "base")
        assert row["state"] == "registered"
        assert row["volume"] == "base.qcow2"
        assert row["object_key"] is None
        assert row["managed_by"] == "config"
        assert "base" in {c.name for c in diff.created}
        assert store.deleted == []

    asyncio.run(_run())


def test_s3_image_without_digest_stays_defined(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "i"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "s3"\n'
                'object_key = "images/local-libvirt/i/x86_64.qcow2"\n',
            )
        )
        store = _FakeImageStore(present={"images/local-libvirt/i/x86_64.qcow2"})
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "i")
        assert row["state"] == "defined"
        assert row["object_key"] is None
        assert any("i" in w.entry for w in diff.warned)

    asyncio.run(_run())


def test_s3_with_digest_and_present_object_registers(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/i/x86_64.qcow2"
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "i"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "s3"\n'
                f'object_key = "{key}"\n'
                'digest = "sha256:beef"\n',
            )
        )
        store = _FakeImageStore(present={key})
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "i")
        assert row["state"] == "registered"
        assert row["object_key"] == key
        assert row["digest"] == "sha256:beef"

    asyncio.run(_run())


def test_s3_store_unreachable_degrades_to_defined(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "i"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "s3"\n'
                'object_key = "images/local-libvirt/i/x86_64.qcow2"\n'
                'digest = "sha256:beef"\n',
            )
        )
        store = _FakeImageStore(unreachable=True)
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)  # must not raise
        async with await _connect(migrated_url) as check:
            row = await _one(check, "i")
        assert row["state"] == "defined"  # degraded, not aborted
        assert any("i" in w.entry for w in diff.warned)

    asyncio.run(_run())


def test_reconcile_never_overwrites_realized_object_key(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_registered_build_row(
                seed,
                name="built",
                object_key="images/local-libvirt/built/x86_64.qcow2",
                digest="sha256:dead",
            )
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "built"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "build"\n'
                'base = "fedora-43"\n',
            )
        )
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "built")
        assert row["state"] == "registered"  # NOT downgraded to defined
        assert row["object_key"] == "images/local-libvirt/built/x86_64.qcow2"
        assert row["digest"] == "sha256:dead"
        assert store.deleted == []

    asyncio.run(_run())


def test_prune_removes_only_config_rows_absent_from_config(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_registered_build_row(
                seed,
                name="runtime-img",
                object_key="images/local-libvirt/runtime-img/x86_64.qcow2",
                digest="sha256:1",
                managed_by="runtime",
            )
            await _insert_config_staged_row(seed, name="stale-config", volume="v.qcow2")
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))  # nothing declared
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            assert await _exists(check, "runtime-img")  # runtime row untouched
            assert not await _exists(check, "stale-config")  # config row pruned (idle)
        assert "stale-config" in {p.name for p in diff.pruned}
        assert store.deleted == []  # row-delete-only; GC reclaims any object

    asyncio.run(_run())


def test_prune_skips_private_upload_sharing_identity(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        # A config image and a project-private upload share (provider,name,arch); the empty
        # config must prune the config row but leave the private upload untouched.
        async with await _connect(migrated_url) as seed:
            await _insert_config_staged_row(
                seed, name="shared", volume="v.qcow2", provider="local-libvirt"
            )
            await _insert_private_upload_row(
                seed,
                name="shared",
                object_key="images/local-libvirt__proj/shared/x86_64.qcow2",
                provider="local-libvirt",
            )
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check, check.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT visibility, managed_by FROM image_catalog WHERE name = 'shared'"
            )
            rows = await cur.fetchall()
        kinds = {(r["visibility"], r["managed_by"]) for r in rows}
        assert kinds == {("private", "runtime")}  # config row pruned, private upload kept
        assert store.deleted == []

    asyncio.run(_run())


def test_prune_of_in_use_image_cordons_not_deletes(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_config_staged_row(
                seed, name="busy", volume="v.qcow2", provider="local-libvirt"
            )
            await _seed_non_terminal_system(
                seed,
                provisioning_profile=_local_catalog_profile("local-libvirt", "busy"),
            )
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            assert await _exists(check, "busy")  # NOT deleted
        assert "busy" in {c.name for c in diff.cordoned}
        assert "busy" not in {p.name for p in diff.pruned}
        assert store.deleted == []

    asyncio.run(_run())


def test_prune_of_in_use_remote_staged_image_cordons_not_deletes(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 1.5: a live REMOTE System references its base image by base_image_volume (the
    # image's `volume`), NOT by (provider,name) catalog ref. The generalized guard must
    # cordon it; deleting the row would let repair_leaked_images GC nothing here (staged has
    # no object) but the same path for an s3 remote base would lose bytes.
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_config_staged_row(
                seed, name="remote-base", volume="base.qcow2", provider="remote-libvirt"
            )
            await _seed_non_terminal_system(
                seed,
                provisioning_profile=_remote_base_volume_profile("base.qcow2"),
            )
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            assert await _exists(check, "remote-base")  # cordoned, not deleted
        assert "remote-base" in {c.name for c in diff.cordoned}
        assert store.deleted == []

    asyncio.run(_run())


def test_relaxed_check_rejects_both_or_neither(migrated_url: str) -> None:
    async def _run() -> None:
        async def _raw_insert(
            conn: psycopg.AsyncConnection,
            *,
            state: str,
            object_key: str | None,
            volume: str | None,
            name: str,
        ) -> None:
            await conn.execute(
                "INSERT INTO image_catalog "
                "(provider, name, arch, format, root_device, object_key, volume, visibility, "
                " state, managed_by, digest) "
                "VALUES ('p', %s, 'x86_64', 'qcow2', '/dev/vda', %s, %s, 'public', %s, "
                " 'config', %s)",
                (
                    name,
                    object_key,
                    volume,
                    state,
                    None if state == "defined" else "sha256:x",
                ),
            )

        async with await _connect(migrated_url) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):  # both
                await _raw_insert(conn, state="registered", object_key="k", volume="v", name="both")
            with pytest.raises(psycopg.errors.CheckViolation):  # neither
                await _raw_insert(
                    conn, state="registered", object_key=None, volume=None, name="neither"
                )
            with pytest.raises(psycopg.errors.CheckViolation):  # defined w/ key
                await _raw_insert(
                    conn, state="defined", object_key="k", volume=None, name="def-key"
                )
            # valid shapes succeed:
            await _raw_insert(conn, state="registered", object_key="k", volume=None, name="ok-key")
            await _raw_insert(conn, state="registered", object_key=None, volume="v", name="ok-vol")

    asyncio.run(_run())


def test_reconcile_rejects_connection_with_open_transaction(
    migrated_url: str, tmp_path: Path
) -> None:
    # The pass toggles autocommit + holds a session lock across transactions, so it must own a
    # transaction-free connection; calling it inside an open transaction fails fast with a
    # clear error rather than psycopg's opaque ProgrammingError.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        store = _FakeImageStore()
        conn = await psycopg.AsyncConnection.connect(migrated_url, autocommit=False)
        try:
            async with conn.transaction():
                await conn.execute("SELECT 1")  # force an open transaction
                with pytest.raises(RuntimeError, match="no open transaction"):
                    await reconcile_images(conn, doc, store)
        finally:
            await conn.close()

    asyncio.run(_run())


def test_reconcile_is_idempotent(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        body = (
            "schema_version = 2\n"
            "[[image]]\n"
            'provider = "remote-libvirt"\n'
            'name = "base"\n'
            'arch = "x86_64"\n'
            'format = "qcow2"\n'
            'root_device = "/dev/vda"\n'
            'visibility = "public"\n'
            "[image.source]\n"
            'kind = "staged"\n'
            'volume = "base.qcow2"\n'
        )
        doc = load_inventory(_write_toml(tmp_path, body))
        store = _FakeImageStore()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_images(conn, doc, store)
            async with pool.connection() as conn:
                diff2 = await reconcile_images(conn, doc, store)
        assert not diff2.created
        assert not diff2.updated
        assert not diff2.pruned

    asyncio.run(_run())


def test_concurrent_passes_do_not_abort_on_identity(migrated_url: str, tmp_path: Path) -> None:
    # Two reconcile passes in flight must serialize on the session inventory lock: no
    # unique-violation abort, and the second is a clean no-op.
    async def _run() -> None:
        body = (
            "schema_version = 2\n"
            "[[image]]\n"
            'provider = "remote-libvirt"\n'
            'name = "base"\n'
            'arch = "x86_64"\n'
            'format = "qcow2"\n'
            'root_device = "/dev/vda"\n'
            'visibility = "public"\n'
            "[image.source]\n"
            'kind = "staged"\n'
            'volume = "base.qcow2"\n'
        )
        doc = load_inventory(_write_toml(tmp_path, body))
        store = _FakeImageStore()
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=4) as pool:

            async def _pass() -> ReconcileDiff:
                async with pool.connection() as conn:
                    return await reconcile_images(conn, doc, store)

            diffs = await asyncio.gather(_pass(), _pass())
        created_total = sum(len(d.created) for d in diffs)
        assert created_total == 1  # exactly one pass created the row; the other no-ops
        async with await _connect(migrated_url) as check, check.cursor() as cur:
            await cur.execute("SELECT count(*) FROM image_catalog WHERE name = 'base'")
            row = await cur.fetchone()
        assert row is not None and row[0] == 1

    asyncio.run(_run())


# --- loop pass: fault isolation (plan Task 1.6) --------------------------------------


def _config_with_inventory_spec() -> ReconcileConfig:
    """A reconcile config that wires an image store, so the inventory pass is in the plan.

    The inventory pass needs an :class:`ImageHeadStore`; the loop only adds the spec when
    ``image_store`` is set (mirroring the image-sweep specs), so the fault-isolation tests
    must hand one in for the ``reconcile_inventory`` pass to run at all.
    """
    return ReconcileConfig(image_store=_FakeImageStore())


def test_loop_inventory_pass_is_fault_isolated(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A malformed systems.toml must NOT abort sibling reaper repairs: the inventory pass is
    # recorded in report.failures while every other repair in the plan still ran (loop.py
    # 350-356 contract). An inventory failure must never raise out of reconcile_once.
    async def _run() -> None:
        bad = tmp_path / "systems.toml"
        bad.write_text("schema_version = 2\n[[image]\n")  # malformed TOML
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(bad))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
        assert "reconcile_inventory" in report.failures  # this pass failed
        assert report.reaped_active_allocations >= 0  # siblings still ran

    asyncio.run(_run())


def test_loop_inventory_pass_skips_quietly_when_default_file_absent(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # systems.toml is gitignored; an absent DEFAULT file is the normal pre-config state and
    # must NOT mark the pass failed every loop iteration.
    async def _run() -> None:
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "does-not-exist.toml"))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
        assert "reconcile_inventory" not in report.failures  # absent default != failure

    asyncio.run(_run())


def test_loop_inventory_pass_reconciles_a_present_file(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A present, valid file is reconciled into the catalog as a sibling repair: the config row
    # is created and the pass is not a failure.
    async def _run() -> None:
        path = _write_toml(
            tmp_path,
            "schema_version = 2\n"
            "[[image]]\n"
            'provider = "remote-libvirt"\n'
            'name = "loop-base"\n'
            'arch = "x86_64"\n'
            'format = "qcow2"\n'
            'root_device = "/dev/vda"\n'
            'visibility = "public"\n'
            "[image.source]\n"
            'kind = "staged"\n'
            'volume = "loop-base.qcow2"\n',
        )
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
        assert "reconcile_inventory" not in report.failures
        assert report.reconciled_inventory == 1
        async with await _connect(migrated_url) as check:
            row = await _one(check, "loop-base")
        assert row["state"] == "registered"
        assert row["managed_by"] == "config"

    asyncio.run(_run())


def test_loop_inventory_pass_absent_when_no_image_store(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With no image store the inventory pass cannot run (it needs the store to HEAD s3
    # objects), so even a malformed file is a no-op for the loop — the spec is simply absent.
    async def _run() -> None:
        bad = tmp_path / "systems.toml"
        bad.write_text("schema_version = 2\n[[image]\n")
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(bad))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper())  # default config: no image store
        assert "reconcile_inventory" not in report.failures

    asyncio.run(_run())


def test_loop_inventory_pass_unreadable_file_is_fault_isolated(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A present-but-unreadable path (here a directory at the configured path) must surface as a
    # failed-this-pass spec via the loader's OSError->InventoryError wrap, not crash the pass —
    # the hash-read fast path catches OSError and defers to the loader.
    async def _run() -> None:
        as_dir = tmp_path / "systems.toml"
        as_dir.mkdir()  # a directory: read_bytes raises IsADirectoryError (an OSError)
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(as_dir))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
        assert "reconcile_inventory" in report.failures

    asyncio.run(_run())


def test_inventory_pass_repairs_drift_on_unchanged_file(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ADR-0021 drift repair must NOT be gated on the file hash: a config-owned row manually
    # deleted out from under an UNCHANGED systems.toml is re-created on the next pass. The
    # content-hash cache may skip only the parse step; the reconcile-against-DB step runs every
    # pass. The file's mtime/bytes never change between the two passes (a cache hit), so a
    # re-created row proves the reconcile step is not skipped on a cache hit.
    async def _run() -> None:
        path = _write_toml(
            tmp_path,
            "schema_version = 2\n"
            "[[image]]\n"
            'provider = "remote-libvirt"\n'
            'name = "drift-base"\n'
            'arch = "x86_64"\n'
            'format = "qcow2"\n'
            'root_device = "/dev/vda"\n'
            'visibility = "public"\n'
            "[image.source]\n"
            'kind = "staged"\n'
            'volume = "drift-base.qcow2"\n',
        )
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
        store = _FakeImageStore()
        pass_ = InventoryReconcilePass()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            async with pool.connection() as conn:
                created = await pass_.run(conn, store)
            assert created == 1  # first pass creates + caches the parse by hash
            async with await _connect(migrated_url) as drift:
                await drift.execute("DELETE FROM image_catalog WHERE name = 'drift-base'")
            async with pool.connection() as conn:
                repaired = await pass_.run(conn, store)  # same file → cache hit, must still repair
            assert repaired == 1  # the deleted config row is re-created (drift repaired)
            async with await _connect(migrated_url) as check:
                assert await _exists(check, "drift-base")

    asyncio.run(_run())


# --- loop pass: CWD-shadow upgrade warning (ADR-0112 fallback removal) ----------------


def test_cwd_inventory_shadowed_detects_unloaded_repo_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # var unset + XDG default absent + ./systems.toml present in CWD == the silent-drop-off case.
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n")
    absent_default = tmp_path / "xdg" / "kdive" / "systems.toml"
    assert _cwd_inventory_shadowed(absent_default) is True


def test_cwd_inventory_shadowed_false_when_var_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An explicit KDIVE_SYSTEMS_TOML means the operator chose the path; nothing is shadowed.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "elsewhere.toml"))
    assert _cwd_inventory_shadowed(tmp_path / "xdg" / "systems.toml") is False


def test_cwd_inventory_shadowed_false_when_default_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The resolved default exists, so it IS being loaded; a CWD file is irrelevant.
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n")
    present_default = tmp_path / "default.toml"
    present_default.write_text("schema_version = 2\n")
    assert _cwd_inventory_shadowed(present_default) is False


def test_cwd_inventory_shadowed_false_when_no_cwd_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No ./systems.toml at all: the normal pre-config state, not an upgrade regression.
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _cwd_inventory_shadowed(tmp_path / "xdg" / "systems.toml") is False


def test_cwd_inventory_shadowed_true_when_var_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An explicitly-empty KDIVE_SYSTEMS_TOML resolves to the XDG default just like unset, so it
    # is still the shadow case — the helper must treat "" as unset (falsiness, not `is None`).
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", "")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n")
    assert _cwd_inventory_shadowed(tmp_path / "xdg" / "systems.toml") is True


def test_inventory_pass_warns_once_about_shadowed_cwd_file(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The shadowed-CWD warning must fire exactly once across passes. This is the regression
    # guard for the reset()/warn-once interaction: _load() calls reset() every pass while the
    # file is absent (the shadow condition), so a flag cleared in reset() would re-warn each
    # loop. KDIVE_SYSTEMS_TOML unset + XDG default absent (autouse sandbox) + ./systems.toml in
    # CWD == shadowed; the CWD file is intentionally NOT loaded, so the pass is a quiet no-op.
    async def _run() -> None:
        monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "systems.toml").write_text("schema_version = 2\n")
        store = _FakeImageStore()
        pass_ = InventoryReconcilePass()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with caplog.at_level("WARNING", logger="kdive.reconciler.inventory"):
                async with pool.connection() as conn:
                    assert await pass_.run(conn, store) == 0  # shadowed file is not loaded
                async with pool.connection() as conn:
                    assert await pass_.run(conn, store) == 0
        warnings = [r for r in caplog.records if "no longer auto-loaded" in r.getMessage()]
        assert len(warnings) == 1

    asyncio.run(_run())


# --- resources: config overlay + #385 (Phase 2, plan Tasks 2.1-2.3) ------------------
#
# These exercise reconcile_resources against a disposable migrated Postgres. The contract:
# managed_by governs existence (config for fault_inject/remote_libvirt, discovery for
# host-probed local-libvirt); a config overlay writes cost_class to the COLUMN and
# vcpus/memory_mb/cap to the capabilities JSONB; prune is cordon-not-delete for a live row.


def _fault_inject_toml(
    *,
    name: str = "fi-1",
    cost_class: str = "local",
    vcpus: int = 8,
    memory_mb: int = 16384,
    cap: int = 1,
    pool: str | None = None,
) -> str:
    pool_line = f'pool = "{pool}"\n' if pool is not None else ""
    return (
        "schema_version = 2\n"
        "[[fault_inject]]\n"
        f'name = "{name}"\n'
        f'cost_class = "{cost_class}"\n'
        f"vcpus = {vcpus}\n"
        f"memory_mb = {memory_mb}\n"
        f"concurrent_allocation_cap = {cap}\n"
        f"{pool_line}"
    )


def _remote_libvirt_toml(
    *,
    name: str,
    base_image: str = "base",
    vcpus: int = 8,
    memory_mb: int = 16384,
    cost_class: str = "remote",
    pool: str | None = None,
) -> str:
    pool_line = f'pool = "{pool}"\n' if pool is not None else ""
    return (
        "schema_version = 2\n"
        "[[image]]\n"
        'provider = "remote-libvirt"\n'
        f'name = "{base_image}"\n'
        'arch = "x86_64"\n'
        'format = "qcow2"\n'
        'root_device = "/dev/vda"\n'
        'visibility = "public"\n'
        "[image.source]\n"
        'kind = "staged"\n'
        f'volume = "{base_image}.qcow2"\n'
        "[[remote_libvirt]]\n"
        f'name = "{name}"\n'
        'uri = "qemu+tls://h1/system"\n'
        'gdb_addr = "10.0.0.1"\n'
        'gdbstub_range = "47000:47099"\n'
        'client_cert_ref = "c.pem"\n'
        'client_key_ref = "k.pem"\n'  # pragma: allowlist secret - filename ref
        'ca_cert_ref = "ca.pem"\n'  # pragma: allowlist secret - filename ref
        f'base_image = "{base_image}"\n'
        f'cost_class = "{cost_class}"\n'
        f"vcpus = {vcpus}\n"
        f"memory_mb = {memory_mb}\n"
        "concurrent_allocation_cap = 1\n"
        f"{pool_line}"
    )


async def _resource_by_name(conn: psycopg.AsyncConnection, name: str) -> dict[str, object]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM resources WHERE name = %s", (name,))
        row = await cur.fetchone()
    assert row is not None, f"no resources row named {name!r}"
    return row


def _row_caps(row: dict[str, object]) -> dict[str, Any]:
    """Narrow the jsonb ``capabilities`` column to a string-keyed dict for assertions."""
    caps = row["capabilities"]
    assert isinstance(caps, dict)
    return cast("dict[str, Any]", caps)


async def _resource_count(conn: psycopg.AsyncConnection, *, kind: str, host_uri: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM resources WHERE kind = %s AND host_uri = %s",
            (kind, host_uri),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _insert_discovered_local(
    conn: psycopg.AsyncConnection,
    *,
    host_uri: str = "qemu:///system",
    vcpus: int = 16,
    memory_mb: int = 65536,
) -> UUID:
    """Insert a discovery-owned local-libvirt row (the discovery/registrar insert path)."""
    rid = uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, capabilities, pool, cost_class, status, host_uri, "
        " managed_by) "
        "VALUES (%s, 'local-libvirt', %s, 'local-libvirt', 'local', 'available', %s, 'discovery')",
        (rid, Jsonb({"vcpus": vcpus, "memory_mb": memory_mb, "pcie": ["0000:00:1f.0"]}), host_uri),
    )
    return rid


async def _seed_live_allocation_on(conn: psycopg.AsyncConnection, resource_id: UUID) -> None:
    """Attach a non-terminal (active) allocation so the resource counts as live."""
    await conn.execute(
        "INSERT INTO allocations (id, principal, project, resource_id, state) "
        "VALUES (%s, 'alice', 'proj', %s, 'active')",
        (uuid4(), resource_id),
    )


def test_fault_inject_overlay_lands_caps_in_jsonb_and_cost_class_in_column(
    migrated_url: str, tmp_path: Path
) -> None:
    # Invariant 1 (load-bearing): cost_class -> the COLUMN; vcpus/memory_mb/cap -> the JSONB.
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(tmp_path, _fault_inject_toml(vcpus=8, memory_mb=16384, cap=3))
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-1")
        assert row["managed_by"] == "config"
        assert row["cost_class"] == "local"  # the COLUMN, not jsonb
        assert row["status"] == "available"
        assert row["pool"] == "default"
        caps = _row_caps(row)
        assert caps["vcpus"] == 8
        assert caps["memory_mb"] == 16384
        assert caps["concurrent_allocation_cap"] == 3
        assert "cost_class" not in caps  # never written into jsonb
        assert "fi-1" in {c.name for c in diff.created}

    asyncio.run(_run())


def test_fault_inject_resource_is_admitted_not_configuration_error(
    migrated_url: str, tmp_path: Path
) -> None:
    # #385 regression headline: after reconcile, allocations.request(kind=fault-inject) is
    # ADMITTED, not configuration_error (the vcpus=None denial the issue reports).
    from kdive.domain.catalog.resources import ResourceKind
    from kdive.security.authz.context import RequestContext
    from kdive.services.allocation.admission.request import AdmissionRequestSpec, request_admission

    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(vcpus=8, memory_mb=16384)))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "INSERT INTO budgets (project, limit_kcu, spent_kcu) "
                    "VALUES ('proj', 1000000, 0)"
                )
                await seed.execute(
                    "INSERT INTO quotas (project, max_concurrent_allocations, "
                    " max_concurrent_systems) VALUES ('proj', 100, 100)"
                )
            async with pool.connection() as conn:
                result = await request_admission(
                    conn,
                    RequestContext(principal="alice", agent_session="s", projects=("proj",)),
                    project="proj",
                    spec=AdmissionRequestSpec(
                        resource_id=None,
                        kind=ResourceKind.FAULT_INJECT,
                        pool=None,
                        shape="small",
                        vcpus=None,
                        memory_gb=None,
                        disk_gb=None,
                        window=None,
                        pcie_devices=(),
                        on_capacity="deny",
                    ),
                )
        assert result.error is None, f"unexpected error: {result.error}"
        assert result.category is None, f"unexpected denial category: {result.category}"
        assert result.allocation is not None, f"not admitted: {result.denial}"

    asyncio.run(_run())


def test_remote_libvirt_overlay_lands_vcpus_memory_in_caps(
    migrated_url: str, tmp_path: Path
) -> None:
    # Tool-feedback regression: a config remote-libvirt host carries vcpus/memory_mb in the
    # capabilities jsonb so admission's ≤-resource-caps check has a ceiling to read.
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(tmp_path, _remote_libvirt_toml(name="rl-1", vcpus=8, memory_mb=16384))
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "rl-1")
        caps = _row_caps(row)
        assert caps["vcpus"] == 8
        assert caps["memory_mb"] == 16384
        assert caps["concurrent_allocation_cap"] == 1

    asyncio.run(_run())


def test_declared_pool_lands_in_pool_column_else_default(migrated_url: str, tmp_path: Path) -> None:
    # ADR-0186: a declared `pool` is written to the resources.pool column; absent → 'default'.
    # One doc declares a pooled remote host and a pool-less fault-inject host (single remote
    # instance keeps the still-present singleton guard happy until #395 relaxes it).
    combined = (
        _remote_libvirt_toml(name="rl-pool", pool="big-remote") + "[[fault_inject]]\n"
        'name = "fi-default"\n'
        'cost_class = "local"\n'
        "vcpus = 8\n"
        "memory_mb = 16384\n"
        "concurrent_allocation_cap = 1\n"
    )

    async def _run() -> tuple[str, str]:
        doc = load_inventory(_write_toml(tmp_path, combined))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            pooled = await _resource_by_name(check, "rl-pool")
            defaulted = await _resource_by_name(check, "fi-default")
        return str(pooled["pool"]), str(defaulted["pool"])

    pooled_pool, default_pool = asyncio.run(_run())
    assert pooled_pool == "big-remote"
    assert default_pool == "default"


def test_remote_libvirt_resource_is_admitted_not_configuration_error(
    migrated_url: str, tmp_path: Path
) -> None:
    # Tool-feedback regression headline: allocations.request(kind=remote-libvirt) is ADMITTED,
    # not configuration_error{vcpus=None} — the universal wall the agent run hit. The seeded
    # remote-libvirt host now declares a vcpus/memory_mb ceiling, so shape "small" (1 vcpu) fits.
    from kdive.domain.catalog.resources import ResourceKind
    from kdive.security.authz.context import RequestContext
    from kdive.services.allocation.admission.request import AdmissionRequestSpec, request_admission

    async def _run() -> None:
        doc = load_inventory(
            _write_toml(tmp_path, _remote_libvirt_toml(name="rl-1", vcpus=8, memory_mb=16384))
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "INSERT INTO budgets (project, limit_kcu, spent_kcu) "
                    "VALUES ('proj', 1000000, 0)"
                )
                await seed.execute(
                    "INSERT INTO quotas (project, max_concurrent_allocations, "
                    " max_concurrent_systems) VALUES ('proj', 100, 100)"
                )
            async with pool.connection() as conn:
                result = await request_admission(
                    conn,
                    RequestContext(principal="alice", agent_session="s", projects=("proj",)),
                    project="proj",
                    spec=AdmissionRequestSpec(
                        resource_id=None,
                        kind=ResourceKind.REMOTE_LIBVIRT,
                        pool=None,
                        shape="small",
                        vcpus=None,
                        memory_gb=None,
                        disk_gb=None,
                        window=None,
                        pcie_devices=(),
                        on_capacity="deny",
                    ),
                )
        assert result.error is None, f"unexpected error: {result.error}"
        assert result.category is None, f"unexpected denial category: {result.category}"
        assert result.allocation is not None, f"not admitted: {result.denial}"

    asyncio.run(_run())


def test_remote_libvirt_is_sole_creator_no_duplicate_with_legacy_discovery(
    migrated_url: str, tmp_path: Path
) -> None:
    # One-creator-per-kind (invariant 4): config + a legacy-discovery insert for one remote
    # host must converge to EXACTLY ONE row, never a duplicate / (kind,name) unique violation.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _remote_libvirt_toml(name="r1")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            # Legacy env-based discovery would bind-only (non-creating) in Phase 2; simulate a
            # second reconcile pass to prove idempotency does not create a second row.
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
        uri = "qemu+tls://h1/system"
        async with await _connect(migrated_url) as check:
            assert await _resource_count(check, kind="remote-libvirt", host_uri=uri) == 1

    asyncio.run(_run())


def test_remote_libvirt_adopts_legacy_discovery_row(migrated_url: str, tmp_path: Path) -> None:
    # A row the legacy env-based discovery already inserted (name NULL) for the same host_uri is
    # ADOPTED (flipped to config + named), not duplicated — the Phase-2→3 one-creator invariant.
    async def _run() -> None:
        uri = "qemu+tls://h1/system"
        async with await _connect(migrated_url) as seed:
            await seed.execute(
                "INSERT INTO resources (id, kind, capabilities, pool, cost_class, status, "
                " host_uri, managed_by) "
                "VALUES (%s, 'remote-libvirt', %s, 'remote', 'remote', 'available', %s, "
                " 'discovery')",
                (uuid4(), Jsonb({"vcpus": 32, "memory_mb": 131072}), uri),
            )
        doc = load_inventory(_write_toml(tmp_path, _remote_libvirt_toml(name="r1")))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            assert await _resource_count(check, kind="remote-libvirt", host_uri=uri) == 1
            row = await _resource_by_name(check, "r1")
            assert row["managed_by"] == "config"  # adopted
            caps = _row_caps(row)
            assert caps["vcpus"] == 32  # discovery-contributed hardware fact preserved
            assert caps["memory_mb"] == 131072

    asyncio.run(_run())


def test_remote_libvirt_uri_change_updates_in_place_no_duplicate(
    migrated_url: str, tmp_path: Path
) -> None:
    # Regression: changing a declared remote host's uri (same name) must UPDATE the row in
    # place, not INSERT a second row that collides on the (kind, name) partial-unique index.
    async def _run() -> None:
        first = load_inventory(_write_toml(tmp_path, _remote_libvirt_toml(name="r1")))
        moved_body = _remote_libvirt_toml(name="r1").replace(
            "qemu+tls://h1/system", "qemu+tls://h2/system"
        )
        moved = load_inventory(_write_toml(tmp_path, moved_body))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, first)
            async with pool.connection() as conn:
                await reconcile_resources(conn, moved)  # must not raise a unique violation
        old_uri, new_uri = "qemu+tls://h1/system", "qemu+tls://h2/system"
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "r1")
            assert row["host_uri"] == new_uri  # uri change propagated
            assert await _resource_count(check, kind="remote-libvirt", host_uri=old_uri) == 0
            assert await _resource_count(check, kind="remote-libvirt", host_uri=new_uri) == 1

    asyncio.run(_run())


def test_local_libvirt_overlay_preserves_discovery_owned_hardware(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 2.3 + invariant (no-overwrite): a discovered local-libvirt row receives the cost/cap
    # overlay and inherits the config name WITHOUT its discovery-owned vcpus/memory/PCIe changing.
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_discovered_local(
                seed, host_uri="qemu:///system", vcpus=16, memory_mb=65536
            )
        body = (
            "schema_version = 2\n"
            "[[local_libvirt]]\n"
            'name = "lab-host"\n'
            'host_uri = "qemu:///system"\n'
            'cost_class = "local"\n'
            "concurrent_allocation_cap = 4\n"
        )
        doc = load_inventory(_write_toml(tmp_path, body))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "lab-host")
        assert row["managed_by"] == "discovery"  # existence stays discovery-owned
        assert row["name"] == "lab-host"  # inherited from config
        assert row["cost_class"] == "local"
        caps = _row_caps(row)
        assert caps["vcpus"] == 16  # discovery-owned, NOT overwritten
        assert caps["memory_mb"] == 65536  # discovery-owned, NOT overwritten
        assert caps["pcie"] == ["0000:00:1f.0"]  # discovery-owned, NOT overwritten
        assert caps["concurrent_allocation_cap"] == 4  # overlaid from config

    asyncio.run(_run())


def test_discovered_local_with_no_config_gets_deterministic_name(
    migrated_url: str, tmp_path: Path
) -> None:
    # A discovered host with no config instance gets a deterministic name from host_uri and is
    # NOT pruned (it is discovery-owned, not config-owned).
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            rid = await _insert_discovered_local(seed, host_uri="qemu:///system")
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check, check.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM resources WHERE id = %s", (rid,))
            row = await cur.fetchone()
        assert row is not None  # discovery-owned row never pruned
        assert row["managed_by"] == "discovery"
        assert row["name"] is not None and row["name"] != ""  # deterministic name assigned

    asyncio.run(_run())


def test_prune_removes_only_config_resources(migrated_url: str, tmp_path: Path) -> None:
    # Invariant 2/3 for resources: prune touches only managed_by='config' rows; a discovery
    # row and a runtime row sharing the field-space are untouched.
    async def _run() -> None:
        # First create a config fault-inject row, plus a discovery local row.
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-keep")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with await _connect(migrated_url) as seed:
                disc = await _insert_discovered_local(seed)
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            # Now reconcile an EMPTY doc: the config fault-inject row must be pruned, the
            # discovery row untouched.
            empty = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
            async with pool.connection() as conn:
                diff = await reconcile_resources(conn, empty)
        async with await _connect(migrated_url) as check:
            assert (
                await _resource_count(check, kind="fault-inject", host_uri="fault-inject://local")
                == 0
            )
            async with check.cursor() as cur:
                await cur.execute("SELECT 1 FROM resources WHERE id = %s", (disc,))
                assert await cur.fetchone() is not None  # discovery row survives
        assert "fi-keep" in {p.name for p in diff.pruned}

    asyncio.run(_run())


def test_prune_of_live_config_resource_cordons_not_deletes(
    migrated_url: str, tmp_path: Path
) -> None:
    # Invariant 5 for resources: a config resource with a live (non-terminal) allocation is
    # CORDONED, not deleted, when it leaves config.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-busy")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                busy = await _resource_by_name(seed, "fi-busy")
                busy_id = busy["id"]
                assert isinstance(busy_id, UUID)
                await _seed_live_allocation_on(seed, busy_id)
            empty = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
            async with pool.connection() as conn:
                diff = await reconcile_resources(conn, empty)
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-busy")  # still present
        assert row["cordoned"] is True
        assert "fi-busy" in {c.name for c in diff.cordoned}
        assert "fi-busy" not in {p.name for p in diff.pruned}

    asyncio.run(_run())


def test_reconcile_resources_is_idempotent(migrated_url: str, tmp_path: Path) -> None:
    # A second pass over an unchanged doc is a clean no-op (change-detecting upserts).
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml()))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with pool.connection() as conn:
                diff2 = await reconcile_resources(conn, doc)
        assert not diff2.created and not diff2.updated and not diff2.pruned

    asyncio.run(_run())


def test_discovery_insert_path_writes_managed_by_discovery(migrated_url: str) -> None:
    # Invariant 5 (load-bearing): a host discovered AFTER the migration must insert at
    # 'discovery', not the column default 'runtime'. Exercises the real registrar insert path.
    from kdive.db.resource_discovery import register_discovered_resource
    from kdive.domain.capacity.state import ResourceStatus
    from kdive.domain.catalog.discovery import ResourceRecord
    from kdive.domain.catalog.resources import ResourceKind

    async def _run() -> None:
        record = ResourceRecord(
            resource_id="qemu:///system",
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={"vcpus": 8, "memory_mb": 8192},
            status=ResourceStatus.AVAILABLE,
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await register_discovered_resource(
                conn, record, pool="local-libvirt", cost_class="local"
            )
        async with await _connect(migrated_url) as check, check.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT managed_by FROM resources WHERE host_uri = 'qemu:///system'")
            row = await cur.fetchone()
        assert row is not None
        assert row["managed_by"] == "discovery"  # NOT 'runtime' (the column default)

    asyncio.run(_run())


# --- Phase 3 Task 3.1: array-of-tables multi-instance --------------------------------


def _two_fault_inject_toml() -> str:
    # Two [[fault_inject]] instances sharing the synthetic host_uri (fault-inject://local),
    # distinguished only by their (kind, name) identity — the Phase-3 multi-instance goal.
    return (
        "schema_version = 2\n"
        "[[fault_inject]]\n"
        'name = "fi-a"\n'
        'cost_class = "local"\n'
        "vcpus = 4\n"
        "memory_mb = 4096\n"
        "[[fault_inject]]\n"
        'name = "fi-b"\n'
        'cost_class = "local"\n'
        "vcpus = 8\n"
        "memory_mb = 8192\n"
    )


def test_two_fault_inject_instances_reconcile_to_two_distinct_rows(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 3.1: N rows per kind. Two [[fault_inject]] sharing host_uri='fault-inject://local'
    # coexist via the (kind, name) unique index — two distinct rows, one per name.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _two_fault_inject_toml()))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            assert (
                await _resource_count(check, kind="fault-inject", host_uri="fault-inject://local")
                == 2
            )
            row_a = await _resource_by_name(check, "fi-a")
            row_b = await _resource_by_name(check, "fi-b")
        assert row_a["id"] != row_b["id"]  # two distinct resource rows
        assert _row_caps(row_a)["vcpus"] == 4
        assert _row_caps(row_b)["vcpus"] == 8
        assert {"fi-a", "fi-b"} <= {c.name for c in diff.created}

    asyncio.run(_run())


def test_two_fault_inject_instances_are_each_independently_allocatable(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 3.1: both instances are independently allocatable. Targeting each by resource_id
    # admits an allocation on it — no allocation-API change, selection by resource_id.
    from kdive.domain.catalog.resources import ResourceKind
    from kdive.security.authz.context import RequestContext
    from kdive.services.allocation.admission.request import AdmissionRequestSpec, request_admission

    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _two_fault_inject_toml()))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "INSERT INTO budgets (project, limit_kcu, spent_kcu) "
                    "VALUES ('proj', 1000000, 0)"
                )
                await seed.execute(
                    "INSERT INTO quotas (project, max_concurrent_allocations, "
                    " max_concurrent_systems) VALUES ('proj', 100, 100)"
                )
                row_a = await _resource_by_name(seed, "fi-a")
                row_b = await _resource_by_name(seed, "fi-b")
            ids = [row_a["id"], row_b["id"]]
            assert all(isinstance(i, UUID) for i in ids)
            results = []
            for resource_id in ids:
                async with pool.connection() as conn:
                    results.append(
                        await request_admission(
                            conn,
                            RequestContext(
                                principal="alice", agent_session="s", projects=("proj",)
                            ),
                            project="proj",
                            spec=AdmissionRequestSpec(
                                resource_id=cast(UUID, resource_id),
                                kind=ResourceKind.FAULT_INJECT,
                                pool=None,
                                shape="small",
                                vcpus=None,
                                memory_gb=None,
                                disk_gb=None,
                                window=None,
                                pcie_devices=(),
                                on_capacity="deny",
                            ),
                        )
                    )
        for resource_id, result in zip(ids, results, strict=True):
            assert result.error is None, f"{resource_id}: unexpected error {result.error}"
            assert result.allocation is not None, f"{resource_id}: not admitted: {result.denial}"
        # The two allocations landed on the two distinct resources.
        landed = {str(r.allocation.resource_id) for r in results if r.allocation is not None}
        assert landed == {str(ids[0]), str(ids[1])}

    asyncio.run(_run())


# --- Phase 4 Task 4.4: adopt-on-collision (runtime row -> config) --------------------
#
# A config identity (name) matching a managed_by='runtime' row is ADOPTED: managed_by flips
# to 'config', the runtime lease (lease_expires_at) is cleared (config rows carry no lease),
# and the config-declared affinity is taken — the model declares no per-instance scope, so a
# config row is always global (owner_project NULL, empty allowlist), widening a previously
# project-scoped runtime resource. Registration and reconcile serialize on the (kind, name)
# identity (a RESOURCE advisory lock keyed by name) so prune cannot race a re-register.


async def _insert_runtime_fault_inject(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    owner_project: str | None,
    affinity_allowlist: list[str] | None = None,
    vcpus: int = 8,
    memory_mb: int = 16384,
) -> UUID:
    """Insert a leased, project-scoped runtime fault-inject row."""
    from datetime import UTC, datetime, timedelta

    rid = uuid4()
    lease = datetime.now(UTC) + timedelta(hours=1)
    await conn.execute(
        "INSERT INTO resources (id, kind, name, capabilities, pool, cost_class, status, "
        " host_uri, managed_by, owner_project, affinity_allowlist, lease_expires_at) "
        "VALUES (%s, 'fault-inject', %s, %s, 'default', 'local', 'available', "
        " 'fault-inject://local', 'runtime', %s, %s, %s)",
        (
            rid,
            name,
            Jsonb({"vcpus": vcpus, "memory_mb": memory_mb, "concurrent_allocation_cap": 1}),
            owner_project,
            affinity_allowlist or [],
            lease,
        ),
    )
    return rid


def test_adopt_runtime_row_clears_lease_and_widens_to_global(
    migrated_url: str, tmp_path: Path
) -> None:
    # Invariant 6: a config identity matching a project-scoped, leased runtime row ADOPTS it —
    # managed_by flips to config, the lease is cleared, and (no per-instance scope in the file)
    # affinity widens to global (owner_project NULL, empty allowlist).
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            rid = await _insert_runtime_fault_inject(
                seed, name="fi-adopt", owner_project="proj-a", affinity_allowlist=["proj-b"]
            )
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-adopt")))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check, check.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM resources WHERE id = %s", (rid,))
            row = await cur.fetchone()
        assert row is not None  # adopted in place — same row, no duplicate
        assert row["managed_by"] == "config"  # flipped from runtime
        assert row["lease_expires_at"] is None  # lease cleared (config rows carry no lease)
        assert row["owner_project"] is None  # widened to global
        assert list(row["affinity_allowlist"]) == []  # config affinity (default global)
        assert "fi-adopt" in {u.name for u in diff.updated}

    asyncio.run(_run())


def test_adopt_does_not_create_duplicate_row(migrated_url: str, tmp_path: Path) -> None:
    # Adoption updates the existing runtime row in place — exactly one (kind, name) row remains,
    # never a second config row colliding on the partial-unique index.
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_runtime_fault_inject(seed, name="fi-dup", owner_project="proj-a")
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-dup")))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check, check.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM resources WHERE kind = 'fault-inject' AND name = 'fi-dup'"
            )
            row = await cur.fetchone()
        assert row is not None
        assert int(row[0]) == 1  # adopted in place, not duplicated

    asyncio.run(_run())


def test_adopted_row_is_not_pruned_when_still_in_config(migrated_url: str, tmp_path: Path) -> None:
    # A re-run over the same config (the adopted row is now managed_by='config') is a clean
    # no-op: the adopted row stays, never re-flagged as a phantom change.
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_runtime_fault_inject(seed, name="fi-steady", owner_project="proj-a")
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-steady")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)  # adopt
            async with pool.connection() as conn:
                diff2 = await reconcile_resources(conn, doc)  # steady-state re-run
        assert not diff2.created and not diff2.updated and not diff2.pruned
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-steady")
        assert row["managed_by"] == "config"

    asyncio.run(_run())


def test_reconcile_serializes_with_register_on_the_resource_name(
    migrated_url: str, tmp_path: Path
) -> None:
    # Registration + reconcile serialize on the (kind, name) identity: while a holder holds the
    # RESOURCE advisory lock keyed by the resource name, a concurrent reconcile of that name
    # BLOCKS (cannot race the adopt/prune), then proceeds once the holder releases.
    from kdive.db.locks import LockScope, advisory_xact_lock
    from kdive.domain.catalog.resources import ResourceKind
    from tests.db_waits import wait_until_any_backend_waiting

    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_runtime_fault_inject(seed, name="fi-race", owner_project="proj-a")
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-race")))
        lock_key = f"{ResourceKind.FAULT_INJECT.value}:fi-race"
        async with (
            AsyncConnectionPool(migrated_url, min_size=2, max_size=4) as pool,
            pool.connection() as holder,
        ):
            async with (
                holder.transaction(),
                advisory_xact_lock(holder, LockScope.RESOURCE, lock_key),
            ):
                task = asyncio.create_task(_reconcile_on(pool, doc))
                await wait_until_any_backend_waiting(holder, locktype="advisory")
                assert not task.done(), "reconcile did not block on the held resource-name lock"
            diff = await task
        assert "fi-race" in {u.name for u in diff.updated}  # adopted once the lock released
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-race")
        assert row["managed_by"] == "config"
        assert row["lease_expires_at"] is None

    asyncio.run(_run())


async def _reconcile_on(pool: AsyncConnectionPool, doc: Any) -> ReconcileDiff:
    async with pool.connection() as conn:
        return await reconcile_resources(conn, doc)


# --- ADR-0115 Finding-1: coefficient pricing across BOTH reconcile orchestrators -----
#
# A config host declaring a novel cost_class PLUS its matching [[cost_class]] block must be
# priced in the SAME reconcile that creates the host — no unpriced-cost_class wall — via BOTH
# resource-reconciling paths (the background loop AND the on-demand ops.reconcile_systems tool,
# the one that silently skipped pricing before Task 4). Plus the seed-floor and a drift-under-
# concurrency case.


def _priced_remote_toml(coeff: str) -> str:
    # A remote host on a novel cost_class plus its matching [[cost_class]] block.
    return _remote_libvirt_toml(name="h1", cost_class="premium") + (
        f'[[cost_class]]\nname = "premium"\ncoeff = {coeff}\n'
    )


async def _coeff_row(pool: AsyncConnectionPool, name: str) -> Decimal | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s", (name,)
        )
        row = await cur.fetchone()
    return Decimal(row[0]) if row else None


def test_loop_prices_before_creating_the_host(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The REAL background-loop entry point (InventoryReconcilePass.run, via reconcile_once) —
    # NOT reconcile_all directly — so Task 4's loop wiring is what's under test. After one
    # loop pass the coefficient row exists and the host is priced: no unpriced-cost_class wall.
    async def _run() -> None:
        path = _write_toml(tmp_path, _priced_remote_toml("3.0"))
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
            assert "reconcile_inventory" not in report.failures  # the loop pass succeeded
            assert await _coeff_row(pool, "premium") == Decimal("3.0")
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "h1")
        assert row["cost_class"] == "premium"

    asyncio.run(_run())


def test_on_demand_reconcile_systems_also_prices(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The path that silently skipped pricing before Task 4 — pin it explicitly.
    import kdive.config as config
    from kdive.mcp.tools.ops import reconcile_systems as rs

    async def _run() -> None:
        path = _write_toml(tmp_path, _priced_remote_toml("4.0"))
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
        config.load()
        ctx = RequestContext(
            principal="admin-1",
            agent_session="s",
            projects=(),
            platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            resp = await rs.reconcile_systems(pool, ctx, image_store=None)
            assert resp.status == "ok"
            assert await _coeff_row(pool, "premium") == Decimal("4.0")

    asyncio.run(_run())


def test_absent_file_leaves_seed_floor_priced(migrated_url: str) -> None:
    # Floor: the 0002/0032 seeds survive with no file, so resolve_coeff succeeds for them.
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            assert await _coeff_row(pool, "local") == Decimal("1.0")
            assert await _coeff_row(pool, "remote") == Decimal("1.0")

    asyncio.run(_run())


def test_drift_detected_under_concurrent_ops_override(migrated_url: str) -> None:
    # A reconcile clobbering a differing prior value (the ops-override case) emits drift.
    async def _run() -> None:
        doc = InventoryDoc.parse(
            {"schema_version": 2, "cost_class": [{"name": "premium", "coeff": 1.0}]}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "INSERT INTO cost_class_coefficients (cost_class, coeff) "
                    "VALUES ('premium', 8.0) "
                    "ON CONFLICT (cost_class) DO UPDATE SET coeff = EXCLUDED.coeff"
                )
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, doc)
            assert [r.name for r in diff.warned] == ["premium"]
            assert await _coeff_row(pool, "premium") == Decimal("1.0")

    asyncio.run(_run())


# --- Inventory override ledger (ADR-0199, #638) ------------------------------------------
#
# Sub-issue A: the reconcile inventory pass consults inventory_overrides so a runtime mutation
# wins over systems.toml without losing drift repair for an identity with NO ledger entry.
# These tests hand-insert ledger rows (the operator-facing mutation tools land in sub-issue B)
# and assert each disposition's reconcile behavior plus the no-entry drift-repair regression.


async def _set_ledger(
    conn: psycopg.AsyncConnection,
    *,
    source_kind: str,
    resource_kind: str,
    name: str,
    disposition: str,
) -> None:
    from kdive.inventory.overrides import (
        InventoryOverrideDisposition,
        InventorySourceKind,
        OverrideIdentity,
        set_override,
    )

    identity = OverrideIdentity(
        source_kind=InventorySourceKind(source_kind), resource_kind=resource_kind, name=name
    )
    await set_override(
        conn,
        identity,
        disposition=InventoryOverrideDisposition(disposition),
        reason="test",
        actor="operator",
    )


async def _ledger_exists(
    conn: psycopg.AsyncConnection, *, source_kind: str, resource_kind: str, name: str
) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM inventory_overrides "
            "WHERE source_kind = %s AND resource_kind = %s AND name = %s",
            (source_kind, resource_kind, name),
        )
        return await cur.fetchone() is not None


async def reconcile_all_for_test(conn: psycopg.AsyncConnection, doc: InventoryDoc) -> None:
    """Run the resource pass then the override GC (the GC's input is the doc).

    The full ``reconcile_all`` also runs the image/coefficient passes, which need a store; these
    ledger tests touch neither, so this helper drives only the resource path plus the GC step the
    GC tests assert.
    """
    from kdive.inventory.reconcile.overrides import reconcile_overrides_gc

    await reconcile_resources(conn, doc)
    await reconcile_overrides_gc(conn, doc)


def test_removed_ledger_skips_recreate_across_passes(migrated_url: str, tmp_path: Path) -> None:
    # A `removed` entry for a still-declared fault-inject identity: reconcile does not (re)create
    # the row, across two passes — the file still declares it, so this is ledger-driven, not
    # file-departure.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-gone")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with await _connect(migrated_url) as seed:
                await _set_ledger(
                    seed,
                    source_kind="resource",
                    resource_kind="fault-inject",
                    name="fi-gone",
                    disposition="removed",
                )
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            assert (
                await _resource_count(check, kind="fault-inject", host_uri="fault-inject://local")
                == 0
            )

    asyncio.run(_run())


def test_removed_ledger_cordons_a_live_row_not_deletes(migrated_url: str, tmp_path: Path) -> None:
    # A `removed` entry whose row is live: the row is cordoned (not deleted). The file still
    # declares the identity, so the cordon is ledger-driven, not file-departure.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-live")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                row = await _resource_by_name(seed, "fi-live")
                rid = row["id"]
                assert isinstance(rid, UUID)
                await _seed_live_allocation_on(seed, rid)
                await _set_ledger(
                    seed,
                    source_kind="resource",
                    resource_kind="fault-inject",
                    name="fi-live",
                    disposition="removed",
                )
            async with pool.connection() as conn:
                diff = await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            cordoned = await _resource_by_name(check, "fi-live")  # still present, cordoned
        assert cordoned["cordoned"] is True
        assert "fi-live" in {c.name for c in diff.cordoned}
        assert "fi-live" not in {p.name for p in diff.pruned}

    asyncio.run(_run())


def test_removed_ledger_deletes_a_never_allocated_row(migrated_url: str, tmp_path: Path) -> None:
    # A `removed` entry whose row never held an allocation is hard-deleted (FK-safe). A resource
    # that ever held an allocation keeps an accounting FK and cannot be row-deleted — it stays
    # cordoned (see test_removed_ledger_cordons_a_live_row_not_deletes); a never-allocated one is
    # deletable, so the ledger-driven delete fires.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-del")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                await _set_ledger(
                    seed,
                    source_kind="resource",
                    resource_kind="fault-inject",
                    name="fi-del",
                    disposition="removed",
                )
            async with pool.connection() as conn:
                diff = await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            assert (
                await _resource_count(check, kind="fault-inject", host_uri="fault-inject://local")
                == 0
            )
        assert "fi-del" in {p.name for p in diff.pruned}

    asyncio.run(_run())


def test_detached_ledger_preserves_runtime_cap_over_file(migrated_url: str, tmp_path: Path) -> None:
    # A `detached` entry: a file concurrent_allocation_cap that differs from the live row does
    # NOT overwrite the runtime cap. The row's existence/identity is kept; only the field
    # overwrite is skipped.
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            # Seed the row at cap=1 (no ledger), then runtime-bump it to cap=5 and detach.
            doc1 = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-det", cap=1)))
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc1)
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "UPDATE resources SET capabilities = "
                    "jsonb_set(capabilities, '{concurrent_allocation_cap}', '5') "
                    "WHERE name = 'fi-det'"
                )
                await _set_ledger(
                    seed,
                    source_kind="resource",
                    resource_kind="fault-inject",
                    name="fi-det",
                    disposition="detached",
                )
            # Reconcile a file that declares cap=1 again: the runtime cap=5 must survive.
            doc2 = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-det", cap=1)))
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc2)
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-det")
        assert _row_caps(row)["concurrent_allocation_cap"] == 5  # runtime value preserved

    asyncio.run(_run())


def test_detached_hand_deleted_row_is_gced_then_reasserted(
    migrated_url: str, tmp_path: Path
) -> None:
    # A `detached` entry whose row was hand-deleted: A3 skips the re-insert (no stale resurrect),
    # A4 GCs the entry, and the following no-entry pass re-asserts the file. Two-pass behavior.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-hd", cap=2)))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                await _set_ledger(
                    seed,
                    source_kind="resource",
                    resource_kind="fault-inject",
                    name="fi-hd",
                    disposition="detached",
                )
                await seed.execute("DELETE FROM resources WHERE name = 'fi-hd'")
            # Pass N: absent row + detached -> A3 skips re-insert; the GC drops the entry.
            async with pool.connection() as conn:
                await reconcile_all_for_test(conn, doc)
            async with await _connect(migrated_url) as check:
                assert not await _ledger_exists(
                    check, source_kind="resource", resource_kind="fault-inject", name="fi-hd"
                )
            # Pass N+1: no entry -> the file is re-asserted, row returns at the file values.
            async with pool.connection() as conn:
                await reconcile_all_for_test(conn, doc)
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-hd")
        assert _row_caps(row)["concurrent_allocation_cap"] == 2  # re-asserted file value

    asyncio.run(_run())


def test_no_entry_identity_is_still_drift_repaired(migrated_url: str, tmp_path: Path) -> None:
    # Regression guarding ADR-0021: an identity with NO ledger entry is still fully drift-repaired
    # — re-created when its row is hand-deleted, and pruned when it leaves the file.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-drift")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            # Hand-delete the row: the next pass re-creates it (no ledger entry).
            async with await _connect(migrated_url) as seed:
                await seed.execute("DELETE FROM resources WHERE name = 'fi-drift'")
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as check:
                assert (
                    await _resource_count(
                        check, kind="fault-inject", host_uri="fault-inject://local"
                    )
                    == 1
                )
            # Drop it from the file: the next pass prunes it (no ledger entry).
            empty = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
            async with pool.connection() as conn:
                diff = await reconcile_resources(conn, empty)
        async with await _connect(migrated_url) as check:
            assert (
                await _resource_count(check, kind="fault-inject", host_uri="fault-inject://local")
                == 0
            )
        assert "fi-drift" in {p.name for p in diff.pruned}

    asyncio.run(_run())


def test_removed_ledger_gced_when_identity_leaves_file(migrated_url: str, tmp_path: Path) -> None:
    # A4 GC: a `removed` entry whose identity is no longer declared in the file is dropped (the
    # operator exported + re-applied, so file-departure prune now owns the removal).
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with await _connect(migrated_url) as seed:
                await _set_ledger(
                    seed,
                    source_kind="resource",
                    resource_kind="fault-inject",
                    name="fi-settled",
                    disposition="removed",
                )
            empty = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
            async with pool.connection() as conn:
                await reconcile_all_for_test(conn, empty)
        async with await _connect(migrated_url) as check:
            assert not await _ledger_exists(
                check, source_kind="resource", resource_kind="fault-inject", name="fi-settled"
            )

    asyncio.run(_run())


def test_detached_ledger_gced_when_file_matches_live(migrated_url: str, tmp_path: Path) -> None:
    # A4 GC: a `detached` entry whose file values now equal the live row is dropped (the override
    # has converged with the file). A still-divergent detached entry is retained.
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-conv", cap=3)))
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                await _set_ledger(
                    seed,
                    source_kind="resource",
                    resource_kind="fault-inject",
                    name="fi-conv",
                    disposition="detached",
                )
            # File values already equal the live row (cap=3), so the override is a no-op -> GC'd.
            async with pool.connection() as conn:
                await reconcile_all_for_test(conn, doc)
        async with await _connect(migrated_url) as check:
            assert not await _ledger_exists(
                check, source_kind="resource", resource_kind="fault-inject", name="fi-conv"
            )

    asyncio.run(_run())


def test_detached_ledger_retained_when_file_still_diverges(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            doc1 = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-div", cap=1)))
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc1)
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "UPDATE resources SET capabilities = "
                    "jsonb_set(capabilities, '{concurrent_allocation_cap}', '9') "
                    "WHERE name = 'fi-div'"
                )
                await _set_ledger(
                    seed,
                    source_kind="resource",
                    resource_kind="fault-inject",
                    name="fi-div",
                    disposition="detached",
                )
            # File still declares cap=1, live row is 9 -> divergent -> entry retained.
            doc2 = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-div", cap=1)))
            async with pool.connection() as conn:
                await reconcile_all_for_test(conn, doc2)
        async with await _connect(migrated_url) as check:
            assert await _ledger_exists(
                check, source_kind="resource", resource_kind="fault-inject", name="fi-div"
            )

    asyncio.run(_run())


# A runtime-added host (managed_by='runtime') is never pruned by the inventory reconcile, which
# only sweeps managed_by='config' rows (ADR-0199, M2.7 B #639). This locks in the runtime-add
# acceptance criterion: register_remote_libvirt -> schedulable without restart, survives passes.


async def _insert_runtime_remote_libvirt(conn: psycopg.AsyncConnection, *, name: str) -> UUID:
    """Insert a leased runtime remote-libvirt row (what register_remote_libvirt creates)."""
    from datetime import UTC, datetime, timedelta

    rid = uuid4()
    lease = datetime.now(UTC) + timedelta(hours=1)
    await conn.execute(
        "INSERT INTO resources (id, kind, name, capabilities, pool, cost_class, status, "
        " host_uri, managed_by, lease_expires_at) "
        "VALUES (%s, 'remote-libvirt', %s, %s, 'default', 'remote', 'available', "
        " 'qemu+tls://rt/system', 'runtime', %s)",
        (
            rid,
            name,
            Jsonb({"vcpus": 8, "memory_mb": 16384, "concurrent_allocation_cap": 1}),
            lease,
        ),
    )
    return rid


def test_runtime_added_remote_libvirt_survives_reconcile_passes(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            rid = await _insert_runtime_remote_libvirt(seed, name="rt-survivor")
        # The file declares a DIFFERENT config remote host, so the prune sweep runs but must not
        # touch the runtime row (it is not managed_by='config').
        doc = load_inventory(_write_toml(tmp_path, _remote_libvirt_toml(name="cfg-other")))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
            await reconcile_resources(conn, doc)  # a second pass: still not pruned
        async with await _connect(migrated_url) as check, check.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT managed_by FROM resources WHERE id = %s", (rid,))
            row = await cur.fetchone()
        assert row is not None  # survived both passes
        assert row["managed_by"] == "runtime"  # still runtime-owned, unchanged
        # and no override ledger entry was created for the runtime add
        async with await _connect(migrated_url) as check:
            assert not await _ledger_exists(
                check, source_kind="resource", resource_kind="remote-libvirt", name="rt-survivor"
            )

    asyncio.run(_run())


# --- staged-path provenance sidecar (#977, ADR-0296) ---------------------------------

_ANY_KERNEL = KernelVersion(6, 11)


def _staged_path_body(path: str, *, name: str = "local-rootfs") -> str:
    return (
        "schema_version = 2\n"
        "[[image]]\n"
        'provider = "local-libvirt"\n'
        f'name = "{name}"\n'
        'arch = "x86_64"\n'
        'format = "qcow2"\n'
        'root_device = "/dev/vda"\n'
        'visibility = "public"\n'
        "[image.source]\n"
        'kind = "staged-path"\n'
        f'path = "{path}"\n'
    )


async def _reconcile(url: str, doc: InventoryDoc, store: _FakeImageStore) -> ReconcileDiff:
    async with (
        AsyncConnectionPool(url, min_size=1, max_size=2) as pool,
        pool.connection() as conn,
    ):
        return await reconcile_images(conn, doc, store)


async def _set_provenance(
    conn: psycopg.AsyncConnection, name: str, prov: dict[str, object]
) -> None:
    await conn.execute(
        "UPDATE image_catalog SET provenance = %s WHERE name = %s", (Jsonb(prov), name)
    )


def test_staged_path_persists_sidecar_provenance(migrated_url: str, tmp_path: Path) -> None:
    """A staged-path row with a valid sidecar carries the sidecar provenance (crit 2/3)."""

    async def _run() -> None:
        qcow2 = tmp_path / "img.qcow2"
        provenance: dict[str, object] = {
            "plane": "local-libvirt",
            "boot_kernel_count": 1,
            "makedumpfile_version": "1.7.7",
        }
        write_sidecar(qcow2, provenance=provenance)
        doc = load_inventory(_write_toml(tmp_path, _staged_path_body(str(qcow2))))
        await _reconcile(migrated_url, doc, _FakeImageStore())
        async with await _connect(migrated_url) as check:
            row = await _one(check, "local-rootfs")
        assert row["provenance"] == provenance
        entry = ImageCatalogEntry.model_validate(row)
        assert render_direct_kernel_signal(entry, _ANY_KERNEL)["status"] == "provisionable"

    asyncio.run(_run())


def test_staged_path_without_sidecar_stays_unverified(migrated_url: str, tmp_path: Path) -> None:
    """No sidecar → row provenance stays {} and the signal reads unverified (crit 4)."""

    async def _run() -> None:
        qcow2 = tmp_path / "img.qcow2"  # no sidecar written
        doc = load_inventory(_write_toml(tmp_path, _staged_path_body(str(qcow2))))
        await _reconcile(migrated_url, doc, _FakeImageStore())
        async with await _connect(migrated_url) as check:
            row = await _one(check, "local-rootfs")
        assert row["provenance"] == {}
        entry = ImageCatalogEntry.model_validate(row)
        assert render_direct_kernel_signal(entry, _ANY_KERNEL)["status"] == "unverified"

    asyncio.run(_run())


def test_staged_path_absent_sidecar_preserves_existing_provenance(
    migrated_url: str, tmp_path: Path
) -> None:
    """An absent sidecar preserves a populated row and reports no spurious update (crit 4/6)."""

    async def _run() -> None:
        qcow2 = tmp_path / "img.qcow2"
        provenance: dict[str, object] = {"boot_kernel_count": 1}
        write_sidecar(qcow2, provenance=provenance)
        doc = load_inventory(_write_toml(tmp_path, _staged_path_body(str(qcow2))))
        await _reconcile(migrated_url, doc, _FakeImageStore())  # first pass persists it
        sidecar_path(qcow2).unlink()  # sidecar removed out-of-band
        diff = await _reconcile(migrated_url, doc, _FakeImageStore())
        async with await _connect(migrated_url) as check:
            row = await _one(check, "local-rootfs")
        assert row["provenance"] == provenance  # preserved, not wiped
        assert "local-rootfs" not in {r.name for r in diff.updated}  # no phantom drift

    asyncio.run(_run())


def test_staged_path_rebuild_refreshes_provenance(migrated_url: str, tmp_path: Path) -> None:
    """A rebuild that changes the sidecar refreshes the row on the next reconcile (crit 6)."""

    async def _run() -> None:
        qcow2 = tmp_path / "img.qcow2"
        write_sidecar(qcow2, provenance={"boot_kernel_count": 2})
        doc = load_inventory(_write_toml(tmp_path, _staged_path_body(str(qcow2))))
        await _reconcile(migrated_url, doc, _FakeImageStore())
        write_sidecar(qcow2, provenance={"boot_kernel_count": 1})  # rebuilt: now single-kernel
        diff = await _reconcile(migrated_url, doc, _FakeImageStore())
        async with await _connect(migrated_url) as check:
            row = await _one(check, "local-rootfs")
        assert row["provenance"] == {"boot_kernel_count": 1}
        assert "local-rootfs" in {r.name for r in diff.updated}

    asyncio.run(_run())


def test_staged_path_steady_state_is_clean_no_op(migrated_url: str, tmp_path: Path) -> None:
    """An unchanged sidecar makes a second pass a clean no-op (crit 6)."""

    async def _run() -> None:
        qcow2 = tmp_path / "img.qcow2"
        write_sidecar(qcow2, provenance={"boot_kernel_count": 1})
        doc = load_inventory(_write_toml(tmp_path, _staged_path_body(str(qcow2))))
        await _reconcile(migrated_url, doc, _FakeImageStore())
        diff = await _reconcile(migrated_url, doc, _FakeImageStore())
        assert diff.updated == []
        assert diff.created == []

    asyncio.run(_run())


def test_reconcile_never_overwrites_build_row_provenance(migrated_url: str, tmp_path: Path) -> None:
    """A build/s3 row's publish-written provenance is left untouched by reconcile (crit 5)."""

    async def _run() -> None:
        publish_prov: dict[str, object] = {"boot_kernel_count": 1, "source": "publish"}
        async with await _connect(migrated_url) as seed:
            await _insert_registered_build_row(
                seed, name="built-img", object_key="images/x.qcow2", digest="sha256:built"
            )
            await _set_provenance(seed, "built-img", publish_prov)
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "built-img"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "build"\n'
                'base = "some-base"\n',
            )
        )
        await _reconcile(migrated_url, doc, _FakeImageStore())
        async with await _connect(migrated_url) as check:
            row = await _one(check, "built-img")
        assert row["provenance"] == publish_prov  # unchanged

    asyncio.run(_run())


# --- operator-attested s3 provenance (#1065, ADR-0323) --------------------------------


def _s3_body(*, name: str = "attested-img", attested: str = "", digest: str = "") -> str:
    digest_line = f'digest = "{digest}"\n' if digest else ""
    return (
        "schema_version = 2\n"
        "[[image]]\n"
        'provider = "local-libvirt"\n'
        f'name = "{name}"\n'
        'arch = "x86_64"\n'
        'format = "qcow2"\n'
        'root_device = "/dev/vda"\n'
        'visibility = "public"\n'
        'capabilities = ["kdump"]\n'
        "[image.source]\n"
        'kind = "s3"\n'
        f'object_key = "rootfs/local/{name}.qcow2"\n'
        f"{digest_line}"
        f"{attested}"
    )


_ATTEST_44 = '[image.attested]\nboot_kernel_count = 1\nmakedumpfile_version = "1.7.9"\n'


def test_s3_attested_synthesizes_operator_provenance(migrated_url: str, tmp_path: Path) -> None:
    """An un-digested (defined) s3 image with [image.attested] gets an actionable signal."""

    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _s3_body(attested=_ATTEST_44)))
        await _reconcile(migrated_url, doc, _FakeImageStore())
        async with await _connect(migrated_url) as check:
            row = await _one(check, "attested-img")
        assert row["state"] == "defined"  # no digest yet, but still characterized
        assert row["provenance"] == {"boot_kernel_count": 1, "makedumpfile_version": "1.7.9"}
        assert row["provenance_attested"] is True
        entry = ImageCatalogEntry.model_validate(row)
        block = render_direct_kernel_signal(entry, _ANY_KERNEL)
        assert block["status"] == "provisionable"
        assert block["basis"] == "operator_attested"  # a claim, not a verified fact

    asyncio.run(_run())


def test_s3_without_attested_stays_unverified(migrated_url: str, tmp_path: Path) -> None:
    """An s3 image with no [image.attested] carries no provenance and reads unverified (crit 3)."""

    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _s3_body()))
        await _reconcile(migrated_url, doc, _FakeImageStore())
        async with await _connect(migrated_url) as check:
            row = await _one(check, "attested-img")
        assert row["provenance"] == {}
        assert row["provenance_attested"] is False
        entry = ImageCatalogEntry.model_validate(row)
        assert render_direct_kernel_signal(entry, _ANY_KERNEL)["status"] == "unverified"

    asyncio.run(_run())


def test_s3_attested_change_detection_and_steady_state(migrated_url: str, tmp_path: Path) -> None:
    """Editing an attested operand updates the row; an unchanged one is a clean no-op (crit 6)."""

    async def _run() -> None:
        two = "[image.attested]\nboot_kernel_count = 2\n"
        doc2 = load_inventory(_write_toml(tmp_path, _s3_body(attested=two)))
        await _reconcile(migrated_url, doc2, _FakeImageStore())
        one = "[image.attested]\nboot_kernel_count = 1\n"
        doc1 = load_inventory(_write_toml(tmp_path, _s3_body(attested=one)))
        diff = await _reconcile(migrated_url, doc1, _FakeImageStore())
        async with await _connect(migrated_url) as check:
            row = await _one(check, "attested-img")
        assert row["provenance"] == {"boot_kernel_count": 1}
        assert "attested-img" in {r.name for r in diff.updated}
        steady = await _reconcile(migrated_url, doc1, _FakeImageStore())
        assert steady.updated == []

    asyncio.run(_run())


def test_s3_removing_attestation_table_preserves_prior_attestation(
    migrated_url: str, tmp_path: Path
) -> None:
    """Removing [image.attested] preserves a prior attestation (like an absent sidecar, ADR-0323).

    ``ops.export_systems_toml`` does not re-emit the table, so a clear-on-absence would strip an
    attestation on an export round-trip; the reconciler therefore never wipes a populated row.
    """

    async def _run() -> None:
        attested_doc = load_inventory(_write_toml(tmp_path, _s3_body(attested=_ATTEST_44)))
        await _reconcile(migrated_url, attested_doc, _FakeImageStore())
        bare_doc = load_inventory(_write_toml(tmp_path, _s3_body()))
        diff = await _reconcile(migrated_url, bare_doc, _FakeImageStore())
        async with await _connect(migrated_url) as check:
            row = await _one(check, "attested-img")
        assert row["provenance"] == {"boot_kernel_count": 1, "makedumpfile_version": "1.7.9"}
        assert row["provenance_attested"] is True  # preserved, not wiped
        assert "attested-img" not in {r.name for r in diff.updated}  # no phantom drift

    asyncio.run(_run())


def test_published_s3_provenance_survives_unattested_reconcile(
    migrated_url: str, tmp_path: Path
) -> None:
    """A registered s3 row's publish provenance is never cleared by the un-attest path (crit 5)."""

    async def _run() -> None:
        key = "rootfs/local/attested-img.qcow2"
        doc = load_inventory(_write_toml(tmp_path, _s3_body(digest="sha256:beef")))
        await _reconcile(migrated_url, doc, _FakeImageStore(present={key}))  # registers
        publish_prov: dict[str, object] = {"boot_kernel_count": 1, "source": "publish"}
        async with await _connect(migrated_url) as seed:
            await _set_provenance(seed, "attested-img", publish_prov)
        # Reconcile again with the same un-attested doc: the row was never operator-attested, so
        # publish_image's provenance must be preserved, not treated as a removed attestation.
        await _reconcile(migrated_url, doc, _FakeImageStore(present={key}))
        async with await _connect(migrated_url) as check:
            row = await _one(check, "attested-img")
        assert row["provenance"] == publish_prov  # untouched
        assert row["provenance_attested"] is False

    asyncio.run(_run())
