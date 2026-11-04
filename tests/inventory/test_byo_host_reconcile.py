"""BYO inventory preserves runtime state and survives export/writeback (ADR-0813)."""

from __future__ import annotations

import asyncio
import tomllib
from typing import Any, cast

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RESOURCES
from kdive.domain.errors import CategorizedError
from kdive.inventory.model import InventoryDoc
from kdive.inventory.overrides import (
    InventoryOverrideDisposition,
    InventorySourceKind,
    OverrideIdentity,
    lookup_many,
    set_override,
)
from kdive.inventory.reconcile.images import ImageHeadStore
from kdive.inventory.reconcile.pipeline import reconcile_all
from kdive.inventory.serialize import read_inventory_snapshot, serialize_inventory
from kdive.inventory.writeback import FakeWriteback
from kdive.mcp.tools.ops.inventory.inventory_export import export_systems_toml
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole
from kdive.services.allocation.admission.affinity import resource_supports_arch
from kdive.services.systems.validation import resolve_accel


class _UnavailableStore:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"BYO-only inventory used object store: {name}")


_STORE = cast("ImageHeadStore", _UnavailableStore())


def _doc(**changes: Any) -> InventoryDoc:
    return InventoryDoc.parse(
        {
            "schema_version": 2,
            "byo_host": [
                {
                    "name": "target",
                    "arch": "ppc64le",
                    "ssh_target": "root@target.example",
                    "ssh_key_ref": "target-key",
                    "baseline_kernel": "6.17.0-baseline",
                    "console_device": "hvc0",
                    "vcpus": 8,
                    "memory_mb": 32768,
                    "oob": {
                        "kind": "hmc",
                        "endpoint": "hmc.example",
                        "managed_system": "managed-a",
                        "lpar_name": "partition-a",
                        "username_ref": "hmc-user",
                        "password_ref": "hmc-password",  # pragma: allowlist secret
                    },
                    **changes,
                }
            ],
        }
    )


async def _row(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM resources WHERE kind='byo-host' AND name='target'")
        return await cur.fetchone()


def test_reconcile_is_uuid_stable_preserves_runtime_and_arch_readers(migrated_url: str) -> None:
    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            first = await reconcile_all(conn, _doc(), _STORE)
            assert len(first.created) == 1
            before = await _row(conn)
            assert before is not None
            await conn.execute(
                "UPDATE resources SET cordoned=true,capabilities=capabilities || %s WHERE id=%s",
                (Jsonb({"runtime_fact": "retained", "cordon_reason": "operator"}), before["id"]),
            )
            second = await reconcile_all(conn, _doc(), _STORE)
            assert not second.created and not second.updated
            changed = _doc(vcpus=16, baseline_kernel="6.18.0-baseline", pool="power-pool")
            await reconcile_all(conn, changed, _STORE)
            after = await _row(conn)
            assert after is not None and after["id"] == before["id"] and after["cordoned"]
            assert after["capabilities"]["runtime_fact"] == "retained"
            assert after["capabilities"]["cordon_reason"] == "operator"
            assert after["capabilities"]["vcpus"] == 16
            assert after["capabilities"]["byo_host"]["baseline_kernel"] == "6.18.0-baseline"
            resource = await RESOURCES.get(conn, before["id"])
            assert resource is not None
            assert resource_supports_arch(resource, "ppc64le")
            assert not resource_supports_arch(resource, "x86_64")
            assert resolve_accel(resource.capability_view.guest_arches(), "ppc64le") == "none"
            assert resource.capability_view.guest_arches()["ppc64le"]["emulator"] == "none"
            assert resource.capability_view.pseries_fadump() is False
            with pytest.raises(CategorizedError):
                resolve_accel(resource.capability_view.guest_arches(), "x86_64")

    asyncio.run(run())


@pytest.mark.parametrize(
    "disposition", [InventoryOverrideDisposition.DETACHED, InventoryOverrideDisposition.REMOVED]
)
def test_byo_override_survives_full_pipeline_and_export_converges(
    migrated_url: str, disposition: InventoryOverrideDisposition
) -> None:
    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await reconcile_all(conn, _doc(), _STORE)
            before = await _row(conn)
            assert before is not None
            identity = OverrideIdentity(
                source_kind=InventorySourceKind.RESOURCE, resource_kind="byo-host", name="target"
            )
            await set_override(
                conn, identity, disposition=disposition, reason="test", actor="operator"
            )
            changed = _doc(baseline_kernel="different", pool="different")
            await reconcile_all(conn, changed, _STORE)
            await reconcile_all(conn, changed, _STORE)
            overrides = await lookup_many(conn, InventorySourceKind.RESOURCE)
            assert ("byo-host", "target") in overrides
            row = await _row(conn)
            if disposition is InventoryOverrideDisposition.REMOVED:
                assert row is None
            else:
                assert row is not None and row["id"] == before["id"]
                assert row["capabilities"]["byo_host"]["baseline_kernel"] == "6.17.0-baseline"
                # Live economic overlays must not be shadowed by the stored declaration copy.
                await conn.execute(
                    "UPDATE resources SET pool='runtime-pool' WHERE id=%s", (row["id"],)
                )
                exported = serialize_inventory(await read_inventory_snapshot(conn))
                doc = InventoryDoc.parse(tomllib.loads(exported))
                assert doc.byo_host[0].pool == "runtime-pool"
                await reconcile_all(conn, doc, _STORE)
                assert not await lookup_many(conn, InventorySourceKind.RESOURCE)

    asyncio.run(run())


def test_departed_allocated_host_is_cordoned_then_pruned(migrated_url: str) -> None:
    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await reconcile_all(conn, _doc(), _STORE)
            row = await _row(conn)
            assert row is not None
            alloc = await conn.execute(
                "INSERT INTO allocations(resource_id,state,principal,project) "
                "VALUES (%s,'active','actor','project') RETURNING id",
                (row["id"],),
            )
            allocation = await alloc.fetchone()
            empty = InventoryDoc.parse({"schema_version": 2})
            await reconcile_all(conn, empty, _STORE)
            row = await _row(conn)
            assert row is not None and row["cordoned"]
            assert allocation is not None
            await conn.execute("DELETE FROM allocations WHERE id=%s", (allocation[0],))
            await reconcile_all(conn, empty, _STORE)
            assert await _row(conn) is None

    asyncio.run(run())


@pytest.mark.parametrize("text", ['quote"line\nslash\\', "kernel\x7flabel"])
def test_byo_export_parse_reconcile_and_writeback_round_trip(migrated_url: str, text: str) -> None:
    async def run() -> None:
        doc = _doc(baseline_kernel=text, pool=text)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await reconcile_all(conn, doc, _STORE)
            original = await _row(conn)
            exported = serialize_inventory(await read_inventory_snapshot(conn))
            restored = InventoryDoc.parse(tomllib.loads(exported))
            assert restored.byo_host == doc.byo_host
            await reconcile_all(conn, restored, _STORE)
            current = await _row(conn)
            assert current is not None and original is not None and current["id"] == original["id"]
        target = FakeWriteback()
        operator = RequestContext(
            principal="operator",
            agent_session="session",
            projects=(),
            platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            response = await export_systems_toml(
                pool, operator, persist=True, resolve_target=lambda: target
            )
        assert response.status == "ok" and target.written is not None
        assert InventoryDoc.parse(tomllib.loads(target.written)).byo_host == doc.byo_host

    asyncio.run(run())


def test_byo_export_refuses_missing_stored_declaration(migrated_url: str) -> None:
    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await reconcile_all(conn, _doc(), _STORE)
            await conn.execute(
                "UPDATE resources SET capabilities=capabilities-'byo_host' WHERE kind='byo-host'"
            )
            with pytest.raises(ValueError, match="declaration"):
                await read_inventory_snapshot(conn)

    asyncio.run(run())
