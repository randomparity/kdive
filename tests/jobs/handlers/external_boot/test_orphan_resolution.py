"""Connected MCP-to-worker recovery-object disposition proof (#2204)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from psycopg.rows import dict_row

from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.external_boot.orphan import resolve_recovery_orphan_handler
from kdive.mcp.tools.external_boot.recovery_requests import resolve_recovery_orphan
from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    OpaqueProviderRef,
    RecoveryObjectBinding,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole
from tests.mcp.lifecycle.runs_support import pool
from tests.mcp.systems_support import provider_resolver
from tests.reconciler.conftest import connect, seed_run, seed_system
from tests.services.external_boot.conftest import seed_activation

_DIGEST = "sha256:" + "a" * 64


def _ctx() -> RequestContext:
    return RequestContext(
        principal="admin",
        agent_session="s",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )


def test_delete_flows_from_admin_admission_through_real_queue_and_fault_provider(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        conn = await connect(migrated_url)
        try:
            async with pool(migrated_url) as conn_pool:
                system_id = await seed_system(conn)
                run_id = await seed_run(conn, system_id)
                async with conn.transaction():
                    seeded = await seed_activation(
                        conn,
                        state=ExternalBootActivationState.ACTIVE,
                        system_id=system_id,
                        run_id=run_id,
                    )
                resource = await (
                    await conn.execute(
                        "SELECT a.resource_id FROM systems s JOIN allocations a "
                        "ON a.id = s.allocation_id WHERE s.id = %s",
                        (system_id,),
                    )
                ).fetchone()
                assert resource is not None
                record_id = uuid4()
                object_binding = RecoveryObjectBinding(
                    record_id=str(record_id),
                    binding=ExternalBootActivationBinding(
                        system_id=str(system_id),
                        run_id=str(run_id),
                        activation_id=str(seeded.activation.id),
                    ),
                    kind="modules",
                    reference=OpaqueProviderRef(ref="modules/quarantined-a"),
                    ownership_digest=_DIGEST,
                )
                provider = FaultInjectExternalBoot()
                observation = provider.register_recovery_object(object_binding)
                await conn.execute(
                    "INSERT INTO external_boot_recovery_quarantine "
                    "(id, object_identity, resource_id, system_id, activation_id, provider_kind, "
                    "authority_instance, object_kind, object_reference, ownership_digest, "
                    "observed_digest, reserved_bytes) "
                    "VALUES (%s, %s, %s, %s, %s, 'local-libvirt', %s, %s, %s, %s, %s, 4096)",
                    (
                        record_id,
                        "quarantine-a",
                        resource[0],
                        system_id,
                        seeded.activation.id,
                        "authority/a",
                        object_binding.kind,
                        object_binding.reference.ref,
                        object_binding.ownership_digest,
                        observation.observed_digest,
                    ),
                )
                resolver = provider_resolver(external_boot_recovery_objects=provider)
                first = await resolve_recovery_orphan(
                    conn_pool,
                    _ctx(),
                    resolver=resolver,
                    system_id=str(system_id),
                    object_identities=["quarantine-a"],
                    disposition="delete",
                )
                second = await resolve_recovery_orphan(
                    conn_pool,
                    _ctx(),
                    resolver=resolver,
                    system_id=str(system_id),
                    object_identities=["quarantine-a"],
                    disposition="delete",
                )
                assert first.object_id == second.object_id
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SELECT * FROM jobs WHERE id = %s", (first.object_id,))
                    row = await cur.fetchone()
                assert row is not None
                result = await resolve_recovery_orphan_handler(
                    conn, Job.model_validate(row), resolver=resolver
                )
                assert result["objects"] == 1
                assert provider.recovery_object_mutations == [("delete", str(record_id))]
                persisted = await (
                    await conn.execute(
                        "SELECT status, reserved_bytes FROM external_boot_recovery_quarantine "
                        "WHERE id = %s",
                        (record_id,),
                    )
                ).fetchone()
                assert persisted == ("deleted", 4096)
        finally:
            await conn.close()

    asyncio.run(_run())
