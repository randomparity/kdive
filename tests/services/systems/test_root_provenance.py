"""System root-provenance snapshot resolution tests (ADR-0583, #2106)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.services.systems.root_provenance import insert_root_provenance, resolve_root_provenance

_DIGEST = "sha256:" + "a" * 64


def _profile(*, checksum: str | None = _DIGEST, arch: str = "x86_64") -> ProvisioningProfile:
    source: dict[str, object] = {"kind": "local", "path": "/images/base.qcow2"}
    if checksum is not None:
        source["sha256"] = checksum
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": arch,
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "provider": {"remote-libvirt": {"base_image_source": source}},
        }
    )


def _row(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": uuid4(),
        "arch": "x86_64",
        "digest": _DIGEST,
        "provenance": {
            "root_spec": {
                "schema": "root-spec-v1",
                "architecture": "x86_64",
                "root": "UUID=abc",
                "arguments": ["root=UUID=abc", "rootfstype=xfs"],
                "authority": "stage-inspection",
                "source": {"kind": "staged-image", "identity": _DIGEST},
            }
        },
    }
    value.update(changes)
    return value


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, *_args: object) -> None:
        return None

    async def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return _Cursor(self.rows)


def _conn(rows: list[dict[str, object]]) -> AsyncConnection:
    return cast("AsyncConnection", _Connection(rows))


def test_resolves_verified_digest_bound_root() -> None:
    result = asyncio.run(resolve_root_provenance(_conn([_row()]), _profile(), "project-a"))
    assert result is not None
    assert result.project == "project-a"
    assert result.root_spec.root == "UUID=abc"


def test_missing_checksum_or_catalog_row_is_disk_grub_only() -> None:
    assert asyncio.run(resolve_root_provenance(_conn([]), _profile(), "p")) is None
    result = asyncio.run(resolve_root_provenance(_conn([_row()]), _profile(checksum=None), "p"))
    assert result is None


def test_duplicate_digest_authorities_fail_closed() -> None:
    with pytest.raises(CategorizedError) as exc:
        asyncio.run(resolve_root_provenance(_conn([_row(), _row()]), _profile(), "p"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["reason"] == "ambiguous_root_provenance"


@pytest.mark.parametrize(
    "row",
    [
        _row(arch="ppc64le"),
        _row(
            provenance={
                "root_spec": {
                    "schema": "root-spec-v1",
                    "architecture": "x86_64",
                    "root": "UUID=abc",
                    "arguments": ["root=UUID=abc", "rootfstype=xfs"],
                    "authority": "stage-inspection",
                    "source": {"kind": "staged-image", "identity": "sha256:" + "b" * 64},
                }
            }
        ),
    ],
)
def test_incompatible_or_stale_authority_fails_closed(row: dict[str, object]) -> None:
    with pytest.raises(CategorizedError) as exc:
        asyncio.run(resolve_root_provenance(_conn([row]), _profile(), "p"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_private_catalog_visibility_and_snapshot_transaction(migrated_url: str) -> None:
    async def run() -> None:
        image_id = uuid4()
        resource_id = uuid4()
        allocation_id = uuid4()
        system_id = uuid4()
        root = _row()["provenance"]
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            await conn.execute(
                "INSERT INTO image_catalog "
                "(id, provider, name, arch, format, root_device, object_key, digest, provenance, "
                " visibility, owner, expires_at, state) "
                "VALUES (%s, 'remote-libvirt', 'private-root', 'x86_64', 'qcow2', '/dev/vda', "
                " 'private.qcow2', %s, %s, 'private', 'project-b', now() + interval '1 day', "
                " 'registered')",
                (image_id, _DIGEST, Jsonb(root)),
            )
            await conn.commit()
            assert await resolve_root_provenance(conn, _profile(), "project-a") is None
            await conn.execute(
                "UPDATE image_catalog SET owner = 'project-a' WHERE id = %s",
                (image_id,),
            )
            await conn.commit()
            snapshot = await resolve_root_provenance(conn, _profile(), "project-a")
            assert snapshot is not None

            await conn.execute(
                "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
                "VALUES (%s, 'remote-libvirt', 'default', 'standard', 'available', "
                "'qemu+ssh://host/system')",
                (resource_id,),
            )
            await conn.commit()

            try:
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO allocations (id, resource_id, state, principal, project) "
                        "VALUES (%s, %s, 'granted', 'tester', 'project-a')",
                        (allocation_id, resource_id),
                    )
                    await conn.execute(
                        "INSERT INTO systems "
                        "(id, allocation_id, state, provisioning_profile, principal, project) "
                        "VALUES (%s, %s, 'provisioning', '{}', 'tester', 'project-a')",
                        (system_id, allocation_id),
                    )
                    await conn.execute(
                        "INSERT INTO system_root_provenance "
                        "(system_id, source_image_id, project, architecture, image_digest, "
                        "root_spec) "
                        "VALUES (%s, %s, 'project-a', 'bad-arch', %s, %s)",
                        (system_id, image_id, _DIGEST, Jsonb(root)),
                    )
            except psycopg.errors.CheckViolation:
                pass
            row = await (
                await conn.execute("SELECT id FROM systems WHERE id = %s", (system_id,))
            ).fetchone()
            assert row is None, "snapshot failure must roll back the System transaction"

            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO allocations (id, resource_id, state, principal, project) "
                    "VALUES (%s, %s, 'granted', 'tester', 'project-a')",
                    (allocation_id, resource_id),
                )
                await conn.execute(
                    "INSERT INTO systems "
                    "(id, allocation_id, state, provisioning_profile, principal, project) "
                    "VALUES (%s, %s, 'provisioning', '{}', 'tester', 'project-a')",
                    (system_id, allocation_id),
                )
                await insert_root_provenance(conn, system_id, snapshot)

            with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE system_root_provenance SET project = 'project-b' "
                        "WHERE system_id = %s",
                        (system_id,),
                    )
            persisted = await (
                await conn.execute(
                    "SELECT project FROM system_root_provenance WHERE system_id = %s",
                    (system_id,),
                )
            ).fetchone()
            assert persisted == ("project-a",)

    asyncio.run(run())
