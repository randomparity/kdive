"""A remote quarantine row published under an adopted identity is admin-resolvable (#3473).

The remote analogue of ``test_orphan_resolution.py``: a retried release adopts an earlier
generation's cleanup, the remote inventory reopens the receipt that cleanup left, the authority
publishes it under the later generation's binding, and the admin tool disposes it against the same
private store. The pieces are covered separately; this joins them over PostgreSQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.external_boot_authority_journal import AuthorityBinding
from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.tools.external_boot.recovery_requests import resolve_recovery_orphan
from kdive.providers.external_boot_authority.journal import record_digest
from kdive.providers.external_boot_authority.orphan import RecoveryOrphanAuthorityService
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCleanupEvidenceContextV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    AuthorityRecoveryOrphanDispositionRequestV1,
    JournalPhase,
    JournalRecordV1,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import AuthenticatedPeer
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole
from tests.jobs.handlers.external_boot.seeding import insert_release_authority, seed_case
from tests.jobs.handlers.external_boot.vehicle import build_vehicle
from tests.mcp.lifecycle.runs_support import pool
from tests.providers.remote_libvirt.external_boot_authority_support import (
    _native_adapter,
    _native_context,
    _native_request,
    _record,
)
from tests.reconciler.conftest import connect

_AUTHORITY_INSTANCE = "remote-a"
_PROVIDER_KIND = "remote-libvirt"


@asynccontextmanager
async def _authority_connection(dsn: str) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
        yield connection


def _admin() -> RequestContext:
    return RequestContext(
        principal="admin",
        agent_session="s",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )


class _RemoteResolver:
    """Binds the System to the remote-libvirt kind with a recovery port composed on the server."""

    async def binding_for_system(self, conn: object, system_id: object) -> Any:
        return SimpleNamespace(
            kind=ResourceKind.REMOTE_LIBVIRT,
            runtime=SimpleNamespace(external_boot_recovery_objects=object()),
        )


class _UnusedPorts:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the host executor owns provider I/O, not the port: {name}")


@pytest.mark.parametrize("disposition", ["delete", "adopt"])
def test_admin_resolves_a_remote_row_published_under_an_adopted_identity(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
    disposition: str,
) -> None:
    asyncio.run(_run(migrated_url, authority_role_dsns, tmp_path, disposition))


async def _run(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
    disposition: str,
) -> None:
    vehicle = build_vehicle()
    seed_record = _record(
        system_id=str(vehicle.system_id),
        run_id=str(vehicle.run_id),
        activation_id=str(vehicle.activation_id),
    )
    adapter, operations, record, point, _ = _native_adapter(
        tmp_path, restored=True, record=seed_record
    )
    operations.state = replace(
        operations.state, definition="source", active=False, recovery_objects_absent=True
    )
    conn = await connect(migrated_url)
    try:
        case = await seed_case(
            conn, vehicle, purpose="release", operation="release", activation_state="active"
        )
        await conn.execute(
            "UPDATE resources SET kind = 'remote-libvirt' "
            "WHERE id = (SELECT resource_id FROM allocations WHERE id = %s)",
            (case.allocation_id,),
        )
        await conn.execute(
            "UPDATE runs SET target_kind = 'remote-libvirt' WHERE id = %s", (vehicle.run_id,)
        )
        _, adopted_identity, _ = await insert_release_authority(
            conn,
            vehicle,
            case,
            generation=1,
            state="superseded",
            provider_kind=_PROVIDER_KIND,
            authority_instance=_AUTHORITY_INSTANCE,
        )
        authority_id, cleanup_identity, cleanup_digest = await insert_release_authority(
            conn,
            vehicle,
            case,
            generation=2,
            state="current",
            provider_kind=_PROVIDER_KIND,
            authority_instance=_AUTHORITY_INSTANCE,
        )

        # The first release attempt ran the cleanup under generation 1 and left its receipt.
        ran = _native_request(
            record, point, purpose="release", operation=AuthorityOperation.CLEANUP, named=True
        ).model_copy(update={"operation_identity": adopted_identity})
        ran_context = _native_context(ran)
        await adapter.commit_cleanup(
            ran,
            ran_context,
            AuthorityCleanupEvidenceContextV1(
                operation_identity=adopted_identity,
                attempt_id=ran.attempt_id,
                operation_nonce=record.module_recovery.operation_nonce,
                cleanup_state="discharged",
                recovery_reference_json=record.module_recovery.model_dump_json(),
            ),
        )

        # The retry runs under generation 2 and inventories the receipt through the adopted proof.
        retry = ran.model_copy(
            update={
                "authority_id": authority_id,
                "generation": 2,
                "attempt_id": uuid4(),
                "operation_identity": cleanup_identity,
                "operation_digest": cleanup_digest,
            }
        )
        observations = await adapter.cleanup_quarantine_inventory(retry, ran_context)
        (observation,) = observations
        assert observation.binding.operation_identity == adopted_identity
        assert observation.binding.attempt_id == str(ran.attempt_id)

        await _publish(
            authority_role_dsns, conn, case.worker_incarnation, authority_id, retry, observations
        )
        row = await (
            await conn.execute(
                "SELECT id, object_identity, operation_identity, authority_instance, provider_kind "
                "FROM external_boot_recovery_quarantine WHERE activation_id = %s",
                (vehicle.activation_id,),
            )
        ).fetchone()
        assert row is not None
        assert row[2:] == (adopted_identity, _AUTHORITY_INSTANCE, _PROVIDER_KIND)

        async with pool(migrated_url) as conn_pool:
            admitted = await resolve_recovery_orphan(
                conn_pool,
                _admin(),
                resolver=cast(Any, _RemoteResolver()),
                system_id=str(vehicle.system_id),
                object_identities=[row[1]],
                disposition=disposition,
            )
        assert admitted.status == "queued"
        request = await _claim_orphan_job(conn, case.worker_incarnation, UUID(admitted.object_id))

        service = RecoveryOrphanAuthorityService(
            lambda: _authority_connection(authority_role_dsns("kdive_provider_authority")),
            cast(Any, _UnusedPorts()),
            adapter,
            authority_instance=_AUTHORITY_INSTANCE,
        )
        response = await service.resolve_recovery_orphan(
            AuthenticatedPeer(case.worker_incarnation), request
        )

        assert (response.disposition, response.objects) == (disposition, 1)
        status = await (
            await conn.execute(
                "SELECT status FROM external_boot_recovery_quarantine WHERE id = %s", (row[0],)
            )
        ).fetchone()
        assert status == ("deleted" if disposition == "delete" else "adopted",)
        receipt = adapter._coordinator.cleanup_quarantine(  # noqa: SLF001
            record.binding, record.plan_identity
        )
        if disposition == "delete":
            assert receipt is None
        else:
            assert receipt is not None and receipt.managed
        assert await adapter.cleanup_quarantine_inventory(retry, ran_context) == ()
    finally:
        adapter.close()
        await conn.close()


async def _publish(
    authority_role_dsns: Callable[[str], str],
    conn: psycopg.AsyncConnection,
    worker: str,
    authority_id: UUID,
    retry: AuthorityMutationRequestV1,
    observations: tuple[Any, ...],
) -> None:
    binding = AuthorityBinding(
        peer_incarnation_id=worker,
        authority_id=authority_id,
        generation=2,
        system_id=retry.system_id,
        activation_id=retry.activation_id,
        run_id=retry.run_id,
        plan_identity=retry.plan_identity,
        purpose="release",
        operation=AuthorityOperation.CLEANUP,
        provider_kind=_PROVIDER_KIND,
        authority_instance=_AUTHORITY_INSTANCE,
        operation_identity=retry.operation_identity,
        operation_digest=retry.operation_digest,
        state="current",
    )
    terminal = JournalRecordV1(
        authority_id=authority_id,
        generation=2,
        system_id=retry.system_id,
        activation_id=retry.activation_id,
        run_id=retry.run_id,
        plan_identity=retry.plan_identity,
        purpose="release",
        operation=AuthorityOperation.CLEANUP,
        provider_kind=_PROVIDER_KIND,
        authority_instance=_AUTHORITY_INSTANCE,
        operation_identity=retry.operation_identity,
        operation_digest=retry.operation_digest,
        attempt_id=retry.attempt_id,
        expected_source_identity=retry.expected_source_identity,
        intended_target_identity=retry.intended_target_identity,
        sequence=12,
        previous_digest="sha256:" + "c" * 64,
        phase=JournalPhase.TERMINAL,
        observation=AuthorityObservationV1(
            observation_id=uuid4(), category="absent", composite_state="sha256:" + "f" * 64
        ),
        outcome="absent",
    )
    await conn.execute(
        "INSERT INTO external_boot_authority_journal_heads "
        "(authority_instance, system_id, sequence, digest, phase, authority_id, generation, "
        "operation_identity, head_record) VALUES (%s,%s,%s,%s,'terminal',%s,2,%s,'{}'::jsonb)",
        (
            _AUTHORITY_INSTANCE,
            retry.system_id,
            terminal.sequence,
            record_digest(terminal),
            authority_id,
            retry.operation_identity,
        ),
    )
    repository = DatabaseAuthorityRepository(
        lambda: _authority_connection(authority_role_dsns("kdive_provider_authority"))
    )
    await repository.publish_cleanup_quarantine(
        AuthenticatedPeer(worker), binding, terminal, observations
    )
    await conn.execute(
        "UPDATE external_boot_authorities SET state = 'retired', retired_at = now() WHERE id = %s",
        (authority_id,),
    )


async def _claim_orphan_job(
    conn: psycopg.AsyncConnection, worker: str, job_id: UUID
) -> AuthorityRecoveryOrphanDispositionRequestV1:
    await conn.execute(
        "UPDATE jobs SET state = 'running', attempt = 1, worker_id = %s, "
        "lease_expires_at = now() + interval '5 minutes', heartbeat_at = now() WHERE id = %s",
        (worker, job_id),
    )
    row = await (
        await conn.execute(
            "SELECT id FROM external_boot_recovery_orphan_requests WHERE job_id = %s", (job_id,)
        )
    ).fetchone()
    assert row is not None
    return AuthorityRecoveryOrphanDispositionRequestV1(
        request_id=row[0], job_id=job_id, job_attempt=1
    )
