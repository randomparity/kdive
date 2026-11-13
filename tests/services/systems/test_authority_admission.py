"""Connected admission tests for authority-owned initial Systems (ADR-0623)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

import kdive.services.systems.authority_owned as authority_owned
from kdive.components.references import ROOTFS_COMPONENT
from kdive.components.validation import ComponentSourceCapabilities
from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing
from kdive.mcp.tools import jobs as jobs_tools
from kdive.mcp.tools.lifecycle.control.registrar import power_system
from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.mcp.tools.lifecycle.systems.provision import SystemProvisionHandlers
from kdive.mcp.tools.lifecycle.systems.snapshot import restore_system, snapshot_system
from kdive.mcp.tools.lifecycle.systems.ssh_access import check_ssh_reachable
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy
from kdive.reconciler.repairs.jobs import repair_terminal_authority_system_attempts
from kdive.reconciler.repairs.systems import repair_orphaned_systems
from kdive.security.authz.rbac import Role
from tests.db.authority_system_support import _seed_direct_terminal_attempt, retain_round
from tests.db.external_boot_authority_support import _RoleDsns
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


@pytest.mark.parametrize("worker_fails_mid_call", [False, True])
def test_preactivation_teardown_recycle_keeps_first_principal_and_audits_requester(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, worker_fails_mid_call: bool
) -> None:
    """#3544, ADR-0447: a recycle leaves `authorizing` alone and audits the recycling caller.

    ``worker_fails_mid_call`` dead-letters the job just before the service reads it, as a worker
    finalizing under a different lock can; the audit must follow the restart, not an earlier read.
    """
    handlers = SystemProvisionHandlers(
        systems_support.TEST_PROFILE_POLICY,
        systems_support.TEST_COMPONENT_SOURCES,
        lambda _root: None,
        authority_route=lambda _kind, _name: "authority-a",
    )
    audit_sql = (
        "SELECT principal FROM audit_log WHERE tool='systems.teardown' "
        "AND transition='teardown:recycled' AND object_id=%s"
    )

    async def run() -> None:
        first = systems_support.ctx(role=Role.ADMIN)
        second = replace(first, principal="user-2")
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
                pool, systems_support.ctx(), allocation_id=allocation_id, profile=_profile()
            )
            system_id = UUID(str(provision.data["system_id"]))
            queued = await teardown_system(pool, first, str(system_id))
            replayed = await teardown_system(pool, second, str(system_id))
            async with pool.connection() as conn:
                before = await (await conn.execute(audit_sql, (system_id,))).fetchall()
            fail_job = (
                "UPDATE jobs SET state='failed',attempt=max_attempts,"
                "error_category='conflict' WHERE id=%s"
            )
            if worker_fails_mid_call:
                real_dedup_job = authority_owned._dedup_job

                async def dead_letter_then_read(conn: psycopg.AsyncConnection, key: str) -> object:
                    with psycopg.connect(migrated_url, autocommit=True) as other:
                        other.execute(fail_job, (queued.object_id,))
                    return await real_dedup_job(conn, key)

                monkeypatch.setattr(authority_owned, "_dedup_job", dead_letter_then_read)
            else:
                async with pool.connection() as conn:
                    await conn.execute(fail_job, (queued.object_id,))
            recycled = await teardown_system(pool, second, str(system_id))
            async with pool.connection() as conn:
                principal = await (
                    await conn.execute(
                        "SELECT authorizing->>'principal',state FROM jobs WHERE id=%s",
                        (queued.object_id,),
                    )
                ).fetchone()
                after = await (await conn.execute(audit_sql, (system_id,))).fetchall()
        assert replayed.object_id == recycled.object_id == queued.object_id
        assert recycled.status == "queued"
        assert before == []
        assert principal == ("user-1", "queued")
        assert after == [("user-2",)]

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


def test_failed_capped_teardown_restarts_and_completes(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    handlers = SystemProvisionHandlers(
        systems_support.TEST_PROFILE_POLICY,
        systems_support.TEST_COMPONENT_SOURCES,
        lambda _root: None,
        authority_route=lambda _kind, _name: "authority-a",
    )

    async def provision_and_request_teardown() -> tuple[str, str]:
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
                pool, systems_support.ctx(), allocation_id=allocation_id, profile=_profile()
            )
            system_id = str(provision.data["system_id"])
            teardown = await teardown_system(pool, systems_support.ctx(role=Role.ADMIN), system_id)
            assert teardown.status == "queued"
            return system_id, str(teardown.object_id)

    async def retry_teardown(system_id: str) -> str:
        async with systems_support.pool(migrated_url) as pool:
            response = await teardown_system(pool, systems_support.ctx(role=Role.ADMIN), system_id)
            assert response.status == "queued"
            return str(response.object_id)

    async def automatic_sweep_state(system_id: str) -> str:
        async with systems_support.pool(migrated_url) as pool, pool.connection() as conn:
            system = await SYSTEMS.get(conn, UUID(system_id))
            assert system is not None
            job = await authority_owned.enqueue_control_teardown(
                conn,
                system,
                Authorizing(principal="user-1", agent_session="s", project="proj"),
                recycle=queue.JobRecyclePolicy.NEVER,
            )
            return job.state.value

    system_id, job_id = asyncio.run(provision_and_request_teardown())
    worker = f"docker:capped-teardown-{uuid4()}"
    credential = uuid4().bytes + uuid4().bytes
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation,authority_kind,authority_binding,fence_protocol,credential_hash,state) "
            "VALUES (%s,'docker','{}',4,%s,'active')",
            (worker, credential),
        )
        row = conn.execute(
            "UPDATE jobs SET state='running',attempt=1,worker_id=%s,"
            "lease_expires_at=clock_timestamp()+interval '5 minutes' WHERE id=%s "
            "RETURNING payload->'authority_system_v1'",
            (worker, job_id),
        ).fetchone()
        conn.commit()
    assert row is not None
    case: dict[str, object] = {
        "system_id": UUID(system_id),
        "job_id": UUID(job_id),
        "worker": worker,
        "credential": credential,
        "marker": row[0],
    }

    outcomes = [retain_round(migrated_url, role_dsns, case, n)[1] for n in range(1, 5)]
    assert outcomes == ["queued", "queued", "queued", "failed"]

    assert asyncio.run(automatic_sweep_state(system_id)) == "failed"
    assert asyncio.run(retry_teardown(system_id)) == job_id
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT job.state,job.attempt,owner.state FROM jobs AS job, "
            "authority_system_ownership AS owner WHERE job.id=%s AND owner.system_id=%s",
            (job_id, system_id),
        ).fetchone() == ("queued", 0, "teardown-requested")

    with psycopg.connect(migrated_url) as conn:
        conn.execute("UPDATE jobs SET state='canceled' WHERE kind='provision'")
        conn.commit()
    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        claimed = worker_conn.execute(
            "SELECT id,attempt FROM claim_worker_job(%s,%s,interval '1 minute',ARRAY['default'])",
            (worker, credential),
        ).fetchone()
        assert claimed == (UUID(job_id), 1)
        worker_conn.commit()
    with psycopg.connect(migrated_url) as conn:
        _, _, receipt, head_digest, _ = _seed_direct_terminal_attempt(
            conn, case, disposition="preactivation-absent", generation=5
        )
        conn.commit()
        authority_id = conn.execute(
            "SELECT current_attempt_id FROM authority_system_ownership WHERE system_id=%s",
            (system_id,),
        ).fetchone()
    assert authority_id is not None
    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        assert worker_conn.execute(
            "SELECT * FROM finalize_authority_system_attempt(%s,%s,1,%s,5,2,%s,%s)",
            (credential, job_id, authority_id[0], head_digest, receipt),
        ).fetchone() == ("applied", "succeeded", "torn_down")


async def _provision_authority_system(pool: AsyncConnectionPool) -> UUID:
    """Provision an authority-owned System that has not activated and return its id."""
    handlers = SystemProvisionHandlers(
        systems_support.TEST_PROFILE_POLICY,
        systems_support.TEST_COMPONENT_SOURCES,
        lambda _root: None,
        authority_route=lambda _kind, _name: "authority-a",
    )
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
        pool, systems_support.ctx(), allocation_id=allocation_id, profile=_profile()
    )
    return UUID(str(provision.data["system_id"]))


def _register_worker(migrated_url: str, name: str) -> tuple[str, bytes]:
    worker = f"docker:{name}-{uuid4()}"
    credential = uuid4().bytes + uuid4().bytes
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation,authority_kind,authority_binding,fence_protocol,credential_hash,state) "
            "VALUES (%s,'docker','{}',4,%s,'active')",
            (worker, credential),
        )
    return worker, credential


@pytest.mark.parametrize("lease", ["lapsed", "live"])
def test_exhausted_preactivation_teardown_recycles_only_after_lease_lapse(
    migrated_url: str, request: pytest.FixtureRequest, lease: str
) -> None:
    """#3617, ADR-0620: `systems.teardown` restarts a final attempt that died without a receipt.

    The exhausted state is seeded: the worker leaves a marked job `running` when it fails without
    a terminal receipt (`jobs/worker.py`), and the contract here is how admission treats that row.
    """
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    audit_sql = (
        "SELECT principal FROM audit_log WHERE tool='systems.teardown' "
        "AND transition='teardown:recycled' AND object_id=%s"
    )
    row_sql = "SELECT state,attempt,max_attempts FROM jobs WHERE id=%s"
    first = systems_support.ctx(role=Role.ADMIN)
    second = replace(first, principal="user-2")

    async def provision_and_request_teardown() -> tuple[UUID, str]:
        async with systems_support.pool(migrated_url) as pool:
            system_id = await _provision_authority_system(pool)
            queued = await teardown_system(pool, first, str(system_id))
            assert queued.status == "queued"
            return system_id, str(queued.object_id)

    async def automatic_then_operator(system_id: UUID) -> tuple[str, str, bool]:
        async with systems_support.pool(migrated_url) as pool:
            async with pool.connection() as conn:
                system = await SYSTEMS.get(conn, system_id)
                assert system is not None
                swept = await authority_owned.enqueue_control_teardown(
                    conn,
                    system,
                    Authorizing(principal="reconciler", agent_session=None, project="proj"),
                    recycle=queue.JobRecyclePolicy.NEVER,
                )
            response = await teardown_system(pool, second, str(system_id))
            async with pool.connection() as conn:
                proven = await authority_owned.preactivation_absence_is_proven(conn, system_id)
        return swept.state.value, str(response.object_id), proven

    system_id, job_id = asyncio.run(provision_and_request_teardown())
    old_worker, old_credential = _register_worker(migrated_url, "exhausted-teardown")
    lease_at = "clock_timestamp() - interval '1 second'"
    if lease == "live":
        lease_at = "clock_timestamp() + interval '5 minutes'"
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state='running',attempt=3,max_attempts=3,worker_id=%s,"
            f"heartbeat_at=now(),lease_expires_at={lease_at} WHERE id=%s",
            (old_worker, job_id),
        )

    swept_state, response_id, proven = asyncio.run(automatic_then_operator(system_id))

    assert swept_state == "running"
    assert response_id == job_id
    assert proven is False
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(row_sql, (job_id,)).fetchone()
        audits = conn.execute(audit_sql, (system_id,)).fetchall()
        owner = conn.execute(
            "SELECT state FROM authority_system_ownership WHERE system_id=%s", (system_id,)
        ).fetchone()
    assert owner == ("teardown-requested",)
    if lease == "live":
        assert row == ("running", 3, 3)
        assert audits == []
        return
    assert row == ("queued", 3, 6)
    assert audits == [("user-2",)]

    # The dead final attempt can no longer renew its lease.
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as old:
        renewed = old.execute(
            "SELECT heartbeat_worker_job(%s,%s,3,interval '5 minutes')",
            (job_id, old_credential),
        ).fetchone()
    assert renewed == (False,)

    # A new attempt claims the restarted job and the authority's absence receipt completes it.
    worker, credential = _register_worker(migrated_url, "recycled-teardown")
    with psycopg.connect(migrated_url) as conn:
        conn.execute("UPDATE jobs SET state='canceled' WHERE kind='provision'")
        marker = conn.execute(
            "SELECT payload->'authority_system_v1' FROM jobs WHERE id=%s", (job_id,)
        ).fetchone()
    assert marker is not None
    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        claimed = worker_conn.execute(
            "SELECT id,attempt FROM claim_worker_job(%s,%s,interval '1 minute',ARRAY['default'])",
            (worker, credential),
        ).fetchone()
        worker_conn.commit()
    assert claimed == (UUID(job_id), 4)
    case: dict[str, object] = {
        "system_id": system_id,
        "job_id": UUID(job_id),
        "worker": worker,
        "credential": credential,
        "marker": marker[0],
    }
    with psycopg.connect(migrated_url) as conn:
        _, _, receipt, head_digest, _ = _seed_direct_terminal_attempt(
            conn, case, disposition="preactivation-absent", generation=5
        )
        conn.commit()
        authority_id = conn.execute(
            "SELECT current_attempt_id FROM authority_system_ownership WHERE system_id=%s",
            (system_id,),
        ).fetchone()
    assert authority_id is not None
    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        assert worker_conn.execute(
            "SELECT * FROM finalize_authority_system_attempt(%s,%s,4,%s,5,2,%s,%s)",
            (credential, job_id, authority_id[0], head_digest, receipt),
        ).fetchone() == ("applied", "succeeded", "torn_down")

    async def absence_proven() -> bool:
        async with systems_support.pool(migrated_url) as pool, pool.connection() as conn:
            return await authority_owned.preactivation_absence_is_proven(conn, system_id)

    assert asyncio.run(absence_proven()) is True


class _NoArtifacts:
    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        return True


@pytest.mark.parametrize("state", ["running", "failed"])
def test_exhausted_preactivation_teardown_keeps_a_pending_absence_receipt(
    migrated_url: str, request: pytest.FixtureRequest, state: str
) -> None:
    """#3627, ADR-0620: the repair, not a restart, settles a pending absence receipt."""
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    first = systems_support.ctx(role=Role.ADMIN)
    second = replace(first, principal="user-2")

    async def provision_and_request_teardown() -> tuple[UUID, str]:
        async with systems_support.pool(migrated_url) as pool:
            system_id = await _provision_authority_system(pool)
            queued = await teardown_system(pool, first, str(system_id))
            assert queued.status == "queued"
            return system_id, str(queued.object_id)

    async def operator_retry(system_id: UUID) -> str:
        async with systems_support.pool(migrated_url) as pool:
            response = await teardown_system(pool, second, str(system_id))
        return str(response.object_id)

    async def reconcile() -> int:
        async with await psycopg.AsyncConnection.connect(role_dsns("kdive_reconciler")) as conn:
            return await repair_terminal_authority_system_attempts(conn, cast(Any, _NoArtifacts()))

    system_id, job_id = asyncio.run(provision_and_request_teardown())
    worker, credential = _register_worker(migrated_url, "receipt-teardown")
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state=%s,attempt=3,max_attempts=3,worker_id=%s,"
            "heartbeat_at=now(),lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (state, worker, job_id),
        )
        marker = conn.execute(
            "SELECT payload->'authority_system_v1' FROM jobs WHERE id=%s", (job_id,)
        ).fetchone()
        assert marker is not None
        case: dict[str, object] = {
            "system_id": system_id,
            "job_id": UUID(job_id),
            "worker": worker,
            "credential": credential,
            "marker": marker[0],
        }
        _seed_direct_terminal_attempt(conn, case, disposition="preactivation-absent")

    assert asyncio.run(operator_retry(system_id)) == job_id
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state,attempt,max_attempts FROM jobs WHERE id=%s", (job_id,)
        ).fetchone() == (state, 3, 3)
        assert conn.execute(
            "SELECT count(*) FROM audit_log WHERE transition='teardown:recycled' AND object_id=%s",
            (system_id,),
        ).fetchone() == (0,)

    # The repair marks only a `running` job `succeeded`; a `failed` one stays `failed`.
    assert asyncio.run(reconcile()) >= 1
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute("SELECT state FROM jobs WHERE id=%s", (job_id,)).fetchone() == (
            "succeeded" if state == "running" else "failed",
        )
        assert conn.execute("SELECT state FROM systems WHERE id=%s", (system_id,)).fetchone() == (
            "torn_down",
        )


@pytest.mark.parametrize(
    "policy",
    [queue.JobRecyclePolicy.TERMINAL, queue.JobRecyclePolicy.TERMINAL_OR_CANCELED],
)
def test_preactivation_teardown_refuses_terminal_recycle(
    migrated_url: str, policy: queue.JobRecyclePolicy
) -> None:
    """A terminal recycle could restart a `succeeded` teardown whose receipt was consumed."""

    async def run() -> None:
        async with systems_support.pool(migrated_url) as pool:
            system_id = await _provision_authority_system(pool)
            async with pool.connection() as conn:
                system = await SYSTEMS.get(conn, system_id)
                binding = await authority_owned.authority_system_binding(conn, system_id)
                assert system is not None and binding is not None
                with pytest.raises(ValueError, match="cannot recycle"):
                    await authority_owned.enqueue_preactivation_teardown(
                        conn,
                        system,
                        binding,
                        Authorizing(principal="user-1", agent_session=None, project="proj"),
                        recycle=policy,
                    )

    asyncio.run(run())
