"""Connected MCP-to-worker recovery-object disposition proof (#2204)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.external_boot_recovery_quarantine import record_cleanup_quarantine
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import DEFAULT_JOB_DISPATCH_LANE, JobKind
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.handlers.external_boot.orphan import resolve_recovery_orphan_handler
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker, WorkerConfig
from kdive.mcp.tools.external_boot.recovery_requests import resolve_recovery_orphan
from kdive.providers.external_boot_authority.orphan import RecoveryOrphanAuthorityService
from kdive.providers.external_boot_authority.protocol import (
    AuthorityRecoveryOrphanDispositionRequestV1,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityMutationAdapter,
    ExternalBootAuthorityService,
)
from kdive.providers.external_boot_authority.transport import _dispatch
from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    OpaqueProviderRef,
    RecoveryObjectBinding,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.db.external_boot_authority_support import authority_role_dsns as _authority_role_dsns
from tests.mcp.lifecycle.runs_support import pool
from tests.mcp.systems_support import provider_resolver
from tests.reconciler.conftest import connect, seed_run, seed_system
from tests.services.external_boot.conftest import seed_activation

_DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def authority_role_dsns(migrated_url: str) -> Generator[Callable[[str], str]]:
    fixture = cast(Any, _authority_role_dsns).__wrapped__(migrated_url)
    yield next(fixture)
    fixture.close()


@asynccontextmanager
async def _authority_connection(dsn: str) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
        yield connection


class _UnusedMutationAdapter:
    async def observe(self, request: object) -> object:
        raise AssertionError("orphan requests must not use mutation authority")

    async def commit(self, request: object, context: object) -> object:
        raise AssertionError("orphan requests must not use mutation authority")


def _ctx() -> RequestContext:
    return RequestContext(
        principal="admin",
        agent_session="s",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )


@pytest.mark.parametrize("disposition", ("delete", "adopt"))
def test_disposition_flows_from_admin_admission_through_real_queue_and_fault_provider(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    disposition: str,
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
                sibling = f"worker-{uuid4()}"
                await conn.execute(
                    "INSERT INTO worker_incarnations "
                    "(incarnation, authority_kind, authority_binding, credential_hash, "
                    "fence_protocol) VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
                    (worker, hashlib.sha256(b"credential").digest()),
                )
                await conn.execute(
                    "INSERT INTO worker_incarnations "
                    "(incarnation, authority_kind, authority_binding, credential_hash, "
                    "fence_protocol) VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
                    (sibling, hashlib.sha256(b"sibling-credential").digest()),
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
                    disposition=disposition,
                )
                second = await resolve_recovery_orphan(
                    conn_pool,
                    _ctx(),
                    resolver=resolver,
                    system_id=str(system_id),
                    object_identities=list(identities),
                    disposition=disposition,
                )
                assert first.object_id == second.object_id
                service = ExternalBootAuthorityService(
                    repository=DatabaseAuthorityRepository(
                        lambda: _authority_connection(
                            authority_role_dsns("kdive_provider_authority")
                        )
                    ),
                    journal_factory=lambda _: (_ for _ in ()).throw(AssertionError("no journal")),
                    adapter=cast(AuthorityMutationAdapter, _UnusedMutationAdapter()),
                    recovery_orphans=RecoveryOrphanAuthorityService(
                        lambda: _authority_connection(
                            authority_role_dsns("kdive_provider_authority")
                        ),
                        provider,
                    ),
                )

                async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
                    peers = {"credential": worker, "sibling-credential": sibling}
                    return AuthenticatedPeer(peers[credential.get_secret_value()])

                delivery = {"lost_response": True}

                class Backend:
                    async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
                        assert deadline > asyncio.get_running_loop().time()
                        response = await _dispatch(envelope, authenticate, service)
                        if delivery["lost_response"]:
                            delivery["lost_response"] = False
                            raise TimeoutError("authority response lost after disposition")
                        return response

                sender = AuthorityRequestSender(Backend, lambda: SecretStr("credential"))
                sibling_sender = AuthorityRequestSender(
                    Backend, lambda: SecretStr("sibling-credential")
                )
                async with await psycopg.AsyncConnection.connect(
                    authority_role_dsns("kdive_worker"), autocommit=True
                ) as worker_role:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        await worker_role.execute("SELECT * FROM external_boot_recovery_quarantine")

                def registry_for(sender: AuthorityRequestSender) -> HandlerRegistry:
                    registry = HandlerRegistry()
                    registry.register(
                        JobKind.RESOLVE_RECOVERY_ORPHAN,
                        lambda handler_conn, job: resolve_recovery_orphan_handler(
                            handler_conn, job, sender_factory=lambda: sender
                        ),
                    )
                    return registry

                async with AsyncConnectionPool(
                    authority_role_dsns("kdive_worker"), min_size=1, max_size=4
                ) as worker_pool:
                    runner = Worker(
                        worker_pool,
                        registry_for(sender),
                        worker_id=worker,
                        incarnation_credential=SecretStr("credential"),
                        secret_registry=SecretRegistry(),
                        config=WorkerConfig(accepted_lanes=(DEFAULT_JOB_DISPATCH_LANE,)),
                    )
                    interrupted = await runner.run_once(DEFAULT_JOB_DISPATCH_LANE)
                    assert interrupted is not None and str(interrupted.id) == first.object_id
                    assert provider.recovery_object_mutations == [(disposition, str(record_id))]
                    with pytest.raises(CategorizedError):
                        await sender.resolve_recovery_orphan(
                            AuthorityRecoveryOrphanDispositionRequestV1(
                                request_id=UUID(str(interrupted.payload["request_id"])),
                                job_id=interrupted.id,
                                job_attempt=interrupted.attempt,
                            ),
                            deadline=asyncio.get_running_loop().time() + 1,
                        )
                async with AsyncConnectionPool(
                    authority_role_dsns("kdive_worker"), min_size=1, max_size=4
                ) as sibling_pool:
                    sibling_runner = Worker(
                        sibling_pool,
                        registry_for(sibling_sender),
                        worker_id=sibling,
                        incarnation_credential=SecretStr("sibling-credential"),
                        secret_registry=SecretRegistry(),
                        config=WorkerConfig(accepted_lanes=(DEFAULT_JOB_DISPATCH_LANE,)),
                    )
                    completed = await sibling_runner.run_once(DEFAULT_JOB_DISPATCH_LANE)
                assert completed is not None and str(completed.id) == first.object_id
                assert completed.attempt == interrupted.attempt + 1
                final = await (
                    await conn.execute("SELECT state FROM jobs WHERE id = %s", (completed.id,))
                ).fetchone()
                assert final == ("succeeded",)
                assert provider.recovery_object_mutations == [(disposition, str(record_id))]
                persisted = await (
                    await conn.execute(
                        "SELECT status, reserved_bytes FROM external_boot_recovery_quarantine "
                        "WHERE id = %s",
                        (record_id,),
                    )
                ).fetchone()
                assert persisted == (("deleted" if disposition == "delete" else "adopted"), 4096)
                replay = await resolve_recovery_orphan(
                    conn_pool,
                    _ctx(),
                    system_id=str(system_id),
                    object_identities=list(identities),
                    disposition=disposition,
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
