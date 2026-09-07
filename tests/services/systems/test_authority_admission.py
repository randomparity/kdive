"""Connected admission tests for authority-owned initial Systems (ADR-0623)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

import kdive.services.systems.authority_owned as authority_owned
from kdive.components.references import ROOTFS_COMPONENT
from kdive.components.validation import ComponentSourceCapabilities
from kdive.domain.capacity.state import SystemState
from kdive.mcp.tools import jobs as jobs_tools
from kdive.mcp.tools.lifecycle.control.registrar import power_system
from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.mcp.tools.lifecycle.systems.provision import SystemProvisionHandlers
from kdive.mcp.tools.lifecycle.systems.snapshot import restore_system, snapshot_system
from kdive.mcp.tools.lifecycle.systems.ssh_access import check_ssh_reachable
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy
from kdive.reconciler.repairs.systems import repair_orphaned_systems
from kdive.security.authz.rbac import Role
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)
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


def _remote_catalog_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 8,
        "boot_method": "disk-image",
        "provider": {
            "remote-libvirt": {
                "base_image_source": {
                    "kind": "catalog",
                    "provider": "remote-libvirt",
                    "name": "authority-base",
                }
            }
        },
    }


def _remote_local_only_sources() -> ComponentSourceCapabilities:
    return ComponentSourceCapabilities(
        provider="remote-libvirt",
        accepted_component_sources={ROOTFS_COMPONENT: frozenset({"local"})},
    )


async def _prepare_remote_allocation(pool: AsyncConnectionPool, allocation_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE resources SET kind='remote-libvirt',name='remote-a' WHERE id=("
            "SELECT resource_id FROM allocations WHERE id=%s)",
            (allocation_id,),
        )
        await conn.execute(
            "INSERT INTO image_catalog "
            "(id,provider,name,arch,format,root_device,object_key,digest,provenance,"
            "visibility,state) VALUES "
            "(%s,'remote-libvirt','authority-base','x86_64','qcow2','/dev/vda',"
            "'authority-base.qcow2',%s,%s,'public','registered')",
            (uuid4(), _DIGEST, Jsonb(_ROOT)),
        )


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


def test_remote_catalog_root_is_admitted_only_through_configured_authority(
    migrated_url: str,
) -> None:
    async def run() -> None:
        validated_worker_paths: list[object] = []
        authority = SystemProvisionHandlers(
            RemoteLibvirtProfilePolicy(),
            _remote_local_only_sources(),
            validated_worker_paths.append,
            authority_route=lambda _kind, _name: "authority-a",
        )
        ordinary = SystemProvisionHandlers(
            RemoteLibvirtProfilePolicy(),
            _remote_local_only_sources(),
            validated_worker_paths.append,
            authority_route=lambda _kind, _name: None,
        )
        async with systems_support.pool(migrated_url) as pool:
            ordinary_allocation = await systems_support.granted_allocation(pool)
            authority_allocation = await systems_support.granted_allocation(pool)
            await _prepare_remote_allocation(pool, ordinary_allocation)
            rejected = await ordinary.provision_system(
                pool,
                systems_support.ctx(),
                allocation_id=ordinary_allocation,
                profile=_remote_catalog_profile(),
            )
            assert rejected.status == "error"
            assert rejected.error_category == "configuration_error"

            admitted = await authority.provision_system(
                pool,
                systems_support.ctx(),
                allocation_id=authority_allocation,
                profile=_remote_catalog_profile(),
            )
            assert admitted.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT system.provisioning_profile,root.image_digest,job.payload "
                    "FROM systems AS system "
                    "JOIN system_root_provenance AS root ON root.system_id=system.id "
                    "JOIN jobs AS job ON job.id=%s WHERE system.id=%s",
                    (admitted.object_id, admitted.data["system_id"]),
                )
                row = await cursor.fetchone()
            assert row is not None
            source = row["provisioning_profile"]["provider"]["remote-libvirt"]["base_image_source"]
            assert source == {
                "kind": "catalog",
                "provider": "remote-libvirt",
                "name": "authority-base",
            }
            assert row["image_digest"] == _DIGEST
            assert row["payload"]["authority_system_v1"]["root_identity"] == _DIGEST
            assert validated_worker_paths == []

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


def test_orphan_repair_routes_preactivation_system_to_authority_teardown(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
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
            provision = await handlers.provision_system(
                pool,
                systems_support.ctx(role=Role.CONTRIBUTOR),
                allocation_id=allocation_id,
                profile=_profile(),
            )
            system_id = str(provision.data["system_id"])
            ordinary_allocation_id = await systems_support.granted_allocation(pool)
            ordinary_system_id = await systems_support.seed_system(
                pool, ordinary_allocation_id, SystemState.READY
            )
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE allocations SET state='released' WHERE id IN (%s,%s)",
                    (allocation_id, ordinary_allocation_id),
                )
            async with (
                AsyncConnectionPool(
                    authority_role_dsns("kdive_reconciler"), min_size=1, max_size=2
                ) as reconciler_pool,
                reconciler_pool.connection() as conn,
            ):
                first = await repair_orphaned_systems(conn)
                second = await repair_orphaned_systems(conn)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT kind,payload FROM jobs WHERE payload->>'system_id'=%s "
                    "ORDER BY created_at",
                    (system_id,),
                )
                jobs = await cursor.fetchall()
                await cursor.execute(
                    "SELECT payload FROM jobs WHERE dedup_key=%s",
                    (f"{ordinary_system_id}:teardown",),
                )
                ordinary_job = await cursor.fetchone()

        assert first == 2
        assert second == 0
        assert [job["kind"] for job in jobs] == ["provision", "teardown"]
        assert jobs[1]["payload"]["authority_system_v1"]["operation"] == ("preactivation-teardown")
        assert ordinary_job is not None
        assert ordinary_job["payload"].get("authority_system_v1") is None

    asyncio.run(run())


def test_preactivation_authority_system_fences_cancel_and_ordinary_mutations(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before first activation, cancellation becomes authority teardown and other mutations stop."""

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
                systems_support.ctx(role=Role.CONTRIBUTOR),
                allocation_id=allocation_id,
                profile=_profile(),
            )
            system_id = str(provision.data["system_id"])
            async with pool.connection() as conn:
                await conn.execute("UPDATE systems SET state='ready' WHERE id=%s", (system_id,))
                resolver = systems_support.provider_resolver()
                runtime = await resolver.runtime_for_system(conn, UUID(system_id))
                assert await authority_owned.ordinary_mutation_is_fenced(conn, UUID(system_id))

            async def audit_failure(*_args: object, **_kwargs: object) -> None:
                raise RuntimeError("audit failure")

            monkeypatch.setattr(jobs_tools.audit, "record", audit_failure)
            with pytest.raises(RuntimeError, match="audit failure"):
                await jobs_tools.cancel_job(
                    pool, systems_support.ctx(role=Role.CONTRIBUTOR), provision.object_id
                )
            monkeypatch.undo()
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT state FROM authority_system_ownership WHERE system_id=%s", (system_id,)
                )
                before_cancel = await cursor.fetchone()
                await cursor.execute(
                    "SELECT count(*) AS n FROM jobs WHERE payload->>'system_id'=%s", (system_id,)
                )
                before_jobs = await cursor.fetchone()

            canceled = await jobs_tools.cancel_job(
                pool, systems_support.ctx(role=Role.CONTRIBUTOR), provision.object_id
            )
            snapshot = await snapshot_system(
                pool,
                systems_support.ctx(role=Role.CONTRIBUTOR),
                runtime,
                system_id=system_id,
                name="before-activation",
                include_memory=True,
            )
            restored = await restore_system(
                pool,
                systems_support.ctx(role=Role.CONTRIBUTOR),
                runtime,
                system_id=system_id,
                name="before-activation",
                start_paused=False,
            )
            powered = await power_system(
                pool,
                systems_support.ctx(role=Role.CONTRIBUTOR),
                system_id=system_id,
                action="off",
            )
            ssh_check = await check_ssh_reachable(
                pool,
                systems_support.ctx(role=Role.CONTRIBUTOR),
                system_id,
                resolver=resolver,
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT id,kind,payload,state FROM jobs WHERE payload->>'system_id'=%s "
                    "ORDER BY created_at",
                    (system_id,),
                )
                jobs = await cursor.fetchall()
                await cursor.execute(
                    "SELECT state FROM authority_system_ownership WHERE system_id=%s", (system_id,)
                )
                owner = await cursor.fetchone()
            teardown_cancel = await jobs_tools.cancel_job(
                pool,
                systems_support.ctx(role=Role.OPERATOR),
                str(jobs[1]["id"]),
            )
            async with pool.connection() as conn:
                teardown_state = await (
                    await conn.execute("SELECT state FROM jobs WHERE id=%s", (jobs[1]["id"],))
                ).fetchone()

        assert canceled.status == "canceled"
        assert before_cancel == {"state": "provisioning"}
        assert before_jobs == {"n": 1}
        assert snapshot.error_category == "conflict"
        assert restored.error_category == "conflict"
        assert powered.error_category == "conflict"
        assert ssh_check.error_category == "configuration_error"
        assert ssh_check.data["reason"] == "authority_system_preactivation_mutation_fenced"
        assert [job["kind"] for job in jobs] == ["provision", "teardown"]
        assert jobs[1]["payload"]["authority_system_v1"]["operation"] == "preactivation-teardown"
        assert owner == {"state": "teardown-requested"}
        assert teardown_cancel.error_category == "conflict"
        assert teardown_cancel.data["reason"] == (
            "authority_system_preactivation_teardown_not_cancelable"
        )
        assert teardown_state == ("queued",)

    asyncio.run(run())
