"""Connected admission tests for authority-owned initial Systems (ADR-0623)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.mcp.tools.lifecycle.systems.provision import SystemProvisionHandlers
from kdive.security.authz.rbac import Role
from tests.mcp import systems_support

_DIGEST = "sha256:" + "a" * 64
_ROOT = {
    "root_spec": {
        "schema": "root-spec-v1",
        "architecture": "x86_64",
        "root": "UUID=authority-root",
        "arguments": ["root=UUID=authority-root", "rootfstype=xfs"],
        "authority": "stage-inspection",
        "source": {"kind": "staged-image", "identity": _DIGEST},
    }
}


def _profile() -> dict[str, object]:
    profile = systems_support.provisioning_profile()
    profile["provider"]["local-libvirt"]["rootfs"]["sha256"] = _DIGEST
    return profile


def test_authority_admission_atomically_marks_job_and_registers_ownership(
    migrated_url: str,
) -> None:
    async def run() -> None:
        handlers = SystemProvisionHandlers(
            systems_support.TEST_PROFILE_POLICY,
            systems_support.TEST_COMPONENT_SOURCES,
            lambda _root: None,
            authority_route=lambda _kind, _name: "authority-a",
        )
        async with systems_support.pool(migrated_url) as pool:
            allocation_id = await systems_support.granted_allocation(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET name='host-a' WHERE id=("
                    "SELECT resource_id FROM allocations WHERE id=%s)",
                    (allocation_id,),
                )
                await conn.execute(
                    "INSERT INTO image_catalog "
                    "(id,provider,name,arch,format,root_device,object_key,digest,provenance,"
                    "visibility,state) VALUES "
                    "(%s,'local-libvirt','authority-base','x86_64','qcow2','/dev/vda',"
                    "'authority-base.qcow2',%s,%s,'public','registered')",
                    (uuid4(), _DIGEST, Jsonb(_ROOT)),
                )
            response = await handlers.provision_system(
                pool,
                systems_support.ctx(),
                allocation_id=allocation_id,
                profile=_profile(),
            )
            assert response.status == "queued"
            system_id = response.data["system_id"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT job.payload,owner.* FROM jobs AS job "
                    "JOIN authority_system_ownership AS owner "
                    "ON owner.system_id=(job.payload->>'system_id')::uuid "
                    "WHERE job.id=%s",
                    (response.object_id,),
                )
                row = await cursor.fetchone()
            assert row is not None
            marker = row["payload"]["authority_system_v1"]
            assert marker["system_id"] == system_id
            assert marker["operation"] == "provision"
            assert marker["authority_instance"] == "authority-a"
            assert marker["root_identity"] == _DIGEST
            assert row["authority_instance"] == "authority-a"
            assert row["profile_identity"] == marker["profile_identity"]
            assert row["root_identity"] == marker["root_identity"]

    asyncio.run(run())


def test_authority_admission_without_verified_root_writes_nothing(migrated_url: str) -> None:
    async def run() -> None:
        handlers = SystemProvisionHandlers(
            systems_support.TEST_PROFILE_POLICY,
            systems_support.TEST_COMPONENT_SOURCES,
            lambda _root: None,
            authority_route=lambda _kind, _name: "authority-a",
        )
        async with systems_support.pool(migrated_url) as pool:
            allocation_id = await systems_support.granted_allocation(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET name='host-a' WHERE id=("
                    "SELECT resource_id FROM allocations WHERE id=%s)",
                    (allocation_id,),
                )
            response = await handlers.provision_system(
                pool,
                systems_support.ctx(),
                allocation_id=allocation_id,
                profile=systems_support.provisioning_profile(),
            )
            assert response.status == "error"
            assert response.error_category == "configuration_error"
            async with pool.connection() as conn:
                counts = await (
                    await conn.execute(
                        "SELECT (SELECT count(*) FROM systems), (SELECT count(*) FROM jobs), "
                        "(SELECT count(*) FROM authority_system_ownership)"
                    )
                ).fetchone()
            assert counts == (0, 0, 0)

    asyncio.run(run())


def test_preactivation_teardown_uses_immutable_authority_binding(migrated_url: str) -> None:
    async def run() -> None:
        handlers = SystemProvisionHandlers(
            systems_support.TEST_PROFILE_POLICY,
            systems_support.TEST_COMPONENT_SOURCES,
            lambda _root: None,
            authority_route=lambda _kind, _name: "authority-a",
        )
        async with systems_support.pool(migrated_url) as pool:
            allocation_id = await systems_support.granted_allocation(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET name='host-a' WHERE id=("
                    "SELECT resource_id FROM allocations WHERE id=%s)",
                    (allocation_id,),
                )
                await conn.execute(
                    "INSERT INTO image_catalog "
                    "(id,provider,name,arch,format,root_device,object_key,digest,provenance,"
                    "visibility,state) VALUES "
                    "(%s,'local-libvirt','authority-base','x86_64','qcow2','/dev/vda',"
                    "'authority-base.qcow2',%s,%s,'public','registered')",
                    (uuid4(), _DIGEST, Jsonb(_ROOT)),
                )
            provision = await handlers.provision_system(
                pool,
                systems_support.ctx(),
                allocation_id=allocation_id,
                profile=_profile(),
            )
            system_id = str(provision.data["system_id"])
            teardown = await teardown_system(pool, systems_support.ctx(role=Role.ADMIN), system_id)
            assert teardown.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT job.payload,owner.state FROM jobs AS job "
                    "JOIN authority_system_ownership AS owner ON owner.system_id=%s "
                    "WHERE job.id=%s",
                    (system_id, teardown.object_id),
                )
                row = await cursor.fetchone()
            assert row is not None
            marker = row["payload"]["authority_system_v1"]
            assert marker["system_id"] == system_id
            assert marker["operation"] == "preactivation-teardown"
            assert marker["authority_instance"] == "authority-a"
            assert row["state"] == "teardown-requested"

    asyncio.run(run())
