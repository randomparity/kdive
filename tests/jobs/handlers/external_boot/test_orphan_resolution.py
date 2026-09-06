"""Connected MCP-to-worker recovery-object disposition proof (#2204)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.db.external_boot_recovery_quarantine import record_cleanup_quarantine
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
                    operation_identity="cleanup-a",
                    attempt_id=str(uuid4()),
                    mutation_journal_sequence=7,
                    mutation_journal_digest="sha256:" + "b" * 64,
                    reserved_bytes=4096,
                )
                provider = FaultInjectExternalBoot()
                provider.register_recovery_object(object_binding)
                authority_job = await (
                    await conn.execute(
                        "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, "
                        "dedup_key) VALUES ('boot', %s, 'succeeded', 3, %s, %s) RETURNING id",
                        (
                            Jsonb({"run_id": str(run_id)}),
                            Jsonb({"principal": "admin", "agent_session": None, "project": "proj"}),
                            str(uuid4()),
                        ),
                    )
                ).fetchone()
                assert authority_job is not None
                worker = f"worker-{uuid4()}"
                await conn.execute(
                    "INSERT INTO worker_incarnations "
                    "(incarnation, authority_kind, authority_binding, credential_hash, "
                    "fence_protocol) VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
                    (worker, b"1" * 32),
                )
                await conn.execute(
                    "INSERT INTO external_boot_authorities "
                    "(system_id, allocation_id, activation_id, run_id, plan_identity, job_id, "
                    "job_attempt, purpose, provider_kind, authority_instance, worker_incarnation, "
                    "operation, operation_identity, operation_digest, generation, state, "
                    "acknowledged_at, retired_at) SELECT %s, s.allocation_id, %s, %s, %s, %s, 1, "
                    "'teardown', 'local-libvirt', 'authority/a', %s, 'cleanup', 'cleanup-a', %s, "
                    "1, 'retired', now(), now() FROM systems s WHERE s.id = %s",
                    (
                        system_id,
                        seeded.activation.id,
                        run_id,
                        seeded.activation.plan_identity,
                        authority_job[0],
                        worker,
                        _DIGEST,
                        system_id,
                    ),
                )
                identities = await record_cleanup_quarantine(
                    conn,
                    activation_id=seeded.activation.id,
                    observations=provider.quarantined_objects(
                        object_binding.binding, OpaqueProviderRef(ref="authority/a")
                    ),
                )
                assert len(identities) == 1
                resolver = provider_resolver(external_boot_recovery_objects=provider)
                first = await resolve_recovery_orphan(
                    conn_pool,
                    _ctx(),
                    resolver=resolver,
                    system_id=str(system_id),
                    object_identities=list(identities),
                    disposition="delete",
                )
                second = await resolve_recovery_orphan(
                    conn_pool,
                    _ctx(),
                    resolver=resolver,
                    system_id=str(system_id),
                    object_identities=list(identities),
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
                replay = await resolve_recovery_orphan(
                    conn_pool,
                    _ctx(),
                    system_id=str(system_id),
                    object_identities=list(identities),
                    disposition="delete",
                    resolver=resolver,
                )
                assert replay.object_id == first.object_id
                assert (
                    replay.data["recovery_readiness_deadline"]
                    == first.data["recovery_readiness_deadline"]
                )
        finally:
            await conn.close()

    asyncio.run(_run())
