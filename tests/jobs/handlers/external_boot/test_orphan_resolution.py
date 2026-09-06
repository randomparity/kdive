"""Connected MCP-to-worker recovery-object disposition proof (#2204)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.external_boot_authority_journal import AuthorityBinding
from kdive.db.external_boot_recovery_quarantine import record_cleanup_quarantine
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import DEFAULT_JOB_DISPATCH_LANE, JobKind
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.handlers.external_boot.orphan import resolve_recovery_orphan_handler
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker, WorkerConfig
from kdive.mcp.tools.external_boot.recovery_requests import resolve_recovery_orphan
from kdive.providers.external_boot_authority.journal import record_digest
from kdive.providers.external_boot_authority.orphan import RecoveryOrphanAuthorityService
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    AuthorityRecoveryOrphanDispositionRequestV1,
    JournalPhase,
    JournalRecordV1,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityMutationAdapter,
    ExternalBootAuthorityService,
)
from kdive.providers.external_boot_authority.transport import _dispatch
from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.local_libvirt.external_boot_authority import (
    LocalExternalBootAuthorityAdapter,
)
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    FinalizeCleanupProof,
    LocalLibvirtExternalBoot,
    RealLocalExternalBootIO,
    RecoveryMetadataStore,
    recovery_directory_name,
)
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    OpaqueProviderRef,
    RecoveryObjectBinding,
    RecoveryObjectObservation,
    RecoveryPoint,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.db.external_boot_authority_support import authority_role_dsns as _authority_role_dsns
from tests.mcp.lifecycle.runs_support import pool
from tests.mcp.systems_support import provider_resolver
from tests.providers.local_libvirt.external_boot_support import _metadata as _local_metadata
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


class _LocalSessionBoundary:
    def close(self) -> None:
        pass


class _LocalSessionFactoryBoundary:
    def open(self, _lease: object, _expected: object) -> _LocalSessionBoundary:
        return _LocalSessionBoundary()


class _InterruptBeforeSecondDisposition:
    """Interrupt one observe after the preceding object's provider and SQL commits."""

    def __init__(self, adapter: LocalExternalBootAuthorityAdapter) -> None:
        self._adapter = adapter
        self.interrupt_record_id: str | None = None
        self.interrupted = False
        self.mutations: list[tuple[str, str]] = []

    async def observe_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef
    ) -> RecoveryObjectObservation:
        if binding.record_id == self.interrupt_record_id and not self.interrupted:
            self.interrupted = True
            raise ConnectionError("worker lost authority response between selected objects")
        return await self._adapter.observe_recovery_object(binding, authority)

    async def delete_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef, digest: str
    ) -> RecoveryObjectObservation:
        self.mutations.append(("delete", binding.record_id))
        return await self._adapter.delete_recovery_object(binding, authority, digest)

    async def adopt_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef, digest: str
    ) -> RecoveryObjectObservation:
        self.mutations.append(("adopt", binding.record_id))
        return await self._adapter.adopt_recovery_object(binding, authority, digest)


def _local_ports(root: Path) -> tuple[LocalLibvirtExternalBoot, LocalExternalBootAuthorityAdapter]:
    io = RealLocalExternalBootIO(
        root,
        cast(Any, object()),
        cast(Any, object()),
        lambda _authority: cast(Any, object()),
        cast(Any, _LocalSessionFactoryBoundary()),
        32 * 1024**3,
    )
    ports = LocalLibvirtExternalBoot(io)
    return ports, LocalExternalBootAuthorityAdapter(ports)


def _local_point(metadata: Any) -> RecoveryPoint:
    binding = cast(ExternalBootActivationBinding, metadata.binding)
    return RecoveryPoint(
        binding=binding,
        plan_identity=metadata.plan_identity,
        materialization_identity=metadata.materialization_identity,
        recovery_ref=OpaqueProviderRef(
            ref=f"local-recovery-v1/{binding.system_id}/{binding.activation_id}"
        ),
        source_state=metadata.source_state,
        target_state=metadata.target_state,
    )


async def _publish_local_quarantine(
    conn: psycopg.AsyncConnection,
    repository: DatabaseAuthorityRepository,
    adapter: LocalExternalBootAuthorityAdapter,
    *,
    root: Path,
    publisher: str,
    system_id: UUID,
    run_id: UUID,
    activation_id: UUID,
    plan_identity: str,
    ordinal: int,
) -> tuple[str, UUID, ExternalBootActivationBinding, Path]:
    authority_id = uuid4()
    generation = ordinal + 1
    authority_instance = f"authority/local-{ordinal}"
    authority_job_id = uuid4()
    root_identity = "sha256:" + hashlib.sha256(f"root-{ordinal}".encode()).hexdigest()
    root_digest = "sha256:" + hashlib.sha256(f"root-digest-{ordinal}".encode()).hexdigest()
    await conn.execute(
        "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
        "(%s, 'boot', %s, 'running', 1, 3, %s, now() + interval '5 minutes', now(), %s, %s)",
        (
            authority_job_id,
            Jsonb({"run_id": str(run_id)}),
            publisher,
            Jsonb({"principal": "admin", "agent_session": None, "project": "proj"}),
            str(uuid4()),
        ),
    )
    await conn.execute(
        "INSERT INTO external_boot_authorities "
        "(id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id, "
        "job_attempt, purpose, provider_kind, authority_instance, worker_incarnation, "
        "operation, operation_identity, operation_digest, generation, state, acknowledged_at) "
        "SELECT %s, %s, s.allocation_id, %s, %s, %s, %s, 1, 'release', 'local-libvirt', "
        "%s, %s, 'release', %s, %s, %s, 'current', now() FROM systems s WHERE s.id = %s",
        (
            authority_id,
            system_id,
            activation_id,
            run_id,
            plan_identity,
            authority_job_id,
            authority_instance,
            publisher,
            root_identity,
            root_digest,
            generation,
            system_id,
        ),
    )
    root_binding = {
        "authority_id": str(authority_id),
        "generation": generation,
        "system_id": str(system_id),
        "activation_id": str(activation_id),
        "run_id": str(run_id),
        "plan_identity": plan_identity,
        "provider_kind": "local-libvirt",
        "authority_instance": authority_instance,
        "worker_incarnation": publisher,
        "root_operation_identity": root_identity,
        "root_operation_digest": root_digest,
    }
    derived = await (
        await conn.execute(
            "SELECT operation_identity, operation_digest "
            "FROM derive_external_boot_release_phase_binding(%s, 'cleanup')",
            (Jsonb(root_binding),),
        )
    ).fetchone()
    assert derived is not None
    cleanup_identity, cleanup_digest = derived
    attempt_id = uuid4()
    mutation_digest = "sha256:" + hashlib.sha256(f"mutation-{ordinal}".encode()).hexdigest()
    terminal = JournalRecordV1(
        authority_id=authority_id,
        generation=generation,
        system_id=system_id,
        activation_id=activation_id,
        run_id=run_id,
        plan_identity=plan_identity,
        purpose="release",
        operation=AuthorityOperation.CLEANUP,
        provider_kind="local-libvirt",
        authority_instance=authority_instance,
        operation_identity=cleanup_identity,
        operation_digest=cleanup_digest,
        sequence=9,
        previous_digest=mutation_digest,
        phase=JournalPhase.TERMINAL,
        attempt_id=attempt_id,
        expected_source_identity=_DIGEST,
        intended_target_identity="sha256:" + "b" * 64,
        observation=AuthorityObservationV1(
            observation_id=uuid4(), category="absent", composite_state="sha256:" + "c" * 64
        ),
        outcome="absent",
    )
    await conn.execute(
        "INSERT INTO external_boot_authority_journal_heads "
        "(authority_instance, system_id, sequence, digest, phase, authority_id, generation, "
        "operation_identity, head_record) VALUES (%s, %s, %s, %s, 'terminal', %s, %s, %s, %s)",
        (
            authority_instance,
            system_id,
            terminal.sequence,
            record_digest(terminal),
            authority_id,
            generation,
            cleanup_identity,
            Jsonb(terminal.model_dump(mode="json", by_alias=True)),
        ),
    )
    binding = ExternalBootActivationBinding(
        system_id=str(system_id), run_id=str(run_id), activation_id=str(activation_id)
    )
    metadata = _local_metadata("recovered").model_copy(
        update={"binding": binding, "plan_identity": plan_identity}
    )
    point = _local_point(metadata)
    proof = FinalizeCleanupProof(
        point_digest=LocalLibvirtExternalBoot.point_digest(point),
        binding=binding,
        operation_id=cleanup_identity,
        attempt_id=str(attempt_id),
        journal_sequence=8,
        journal_digest=mutation_digest,
        phase="mutation-started",
    )
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        store.publish_tombstone(reference, binding, metadata, proof.point_digest)
        store.record_cleanup_quarantine(point, proof)
    request = AuthorityMutationRequestV1(
        authority_id=authority_id,
        generation=generation,
        system_id=system_id,
        activation_id=activation_id,
        run_id=run_id,
        plan_identity=plan_identity,
        purpose="release",
        operation=AuthorityOperation.CLEANUP,
        provider_kind="local-libvirt",
        authority_instance=authority_instance,
        operation_identity=cleanup_identity,
        operation_digest=cleanup_digest,
        attempt_id=attempt_id,
        expected_source_identity=_DIGEST,
        intended_target_identity="sha256:" + "b" * 64,
        recovery_objects=(),
    )
    observations = await adapter.cleanup_quarantine_inventory(request)
    assert len(observations) == 1
    authority_binding = AuthorityBinding(
        peer_incarnation_id=publisher,
        authority_id=authority_id,
        generation=generation,
        system_id=system_id,
        activation_id=activation_id,
        run_id=run_id,
        plan_identity=plan_identity,
        purpose="release",
        operation=AuthorityOperation.CLEANUP,
        provider_kind="local-libvirt",
        authority_instance=authority_instance,
        operation_identity=cleanup_identity,
        operation_digest=cleanup_digest,
        state="current",
    )
    await repository.publish_cleanup_quarantine(
        AuthenticatedPeer(publisher), authority_binding, terminal, observations
    )
    await conn.execute(
        "UPDATE external_boot_authorities SET state = 'retired', retired_at = now() WHERE id = %s",
        (authority_id,),
    )
    row = await (
        await conn.execute(
            "SELECT id, object_identity FROM external_boot_recovery_quarantine "
            "WHERE activation_id = %s",
            (activation_id,),
        )
    ).fetchone()
    assert row is not None
    directory = root / recovery_directory_name(point.recovery_ref, binding)
    return row[1], row[0], binding, directory


def _ctx() -> RequestContext:
    return RequestContext(
        principal="admin",
        agent_session="s",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )


@pytest.mark.parametrize(
    ("disposition", "tamper"),
    (
        ("delete", None),
        ("adopt", None),
        ("delete", "ownership"),
        ("delete", "resource"),
        ("delete", "deadline"),
    ),
)
def test_disposition_flows_from_admin_admission_through_real_queue_and_fault_provider(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    disposition: str,
    tamper: str | None,
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
                sibling_record_id = uuid4()
                sibling_binding = object_binding.model_copy(
                    update={
                        "record_id": str(sibling_record_id),
                        "reference": OpaqueProviderRef(ref="modules/quarantined-b"),
                        "operation_identity": "cleanup-b",
                    }
                )
                provider = FaultInjectExternalBoot()
                provider.register_recovery_object(object_binding)
                provider.register_recovery_object(sibling_binding)
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
                assert len(identities) == 2
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
                if tamper == "ownership":
                    await conn.execute(
                        "UPDATE external_boot_recovery_quarantine SET ownership_digest = %s "
                        "WHERE id = %s",
                        ("sha256:" + "c" * 64, record_id),
                    )
                elif tamper == "resource":
                    replacement_resource = uuid4()
                    await conn.execute(
                        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
                        "VALUES (%s, 'local-libvirt', 'replacement', 'standard', 'available', "
                        "'qemu:///system')",
                        (replacement_resource,),
                    )
                    await conn.execute(
                        "UPDATE allocations SET resource_id = %s "
                        "WHERE id = (SELECT allocation_id FROM systems WHERE id = %s)",
                        (replacement_resource, system_id),
                    )
                elif tamper == "deadline":
                    request_row = await (
                        await conn.execute(
                            "SELECT id FROM external_boot_recovery_orphan_requests "
                            "WHERE job_id = %s",
                            (UUID(first.object_id),),
                        )
                    ).fetchone()
                    assert request_row is not None
                    await conn.execute(
                        "UPDATE external_boot_recovery_orphan_requests "
                        "SET readiness_deadline = clock_timestamp() - interval '1 second' "
                        "WHERE id = %s",
                        (request_row[0],),
                    )
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
                    if tamper is not None:
                        assert provider.recovery_object_mutations == []
                        states = await (
                            await conn.execute(
                                "SELECT status FROM external_boot_recovery_quarantine "
                                "WHERE id = ANY(%s) ORDER BY id",
                                ([record_id, sibling_record_id],),
                            )
                        ).fetchall()
                        assert states == [("quarantined",), ("quarantined",)]
                        return
                    assert set(provider.recovery_object_mutations) == {
                        (disposition, str(record_id)),
                        (disposition, str(sibling_record_id)),
                    }
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
                assert set(provider.recovery_object_mutations) == {
                    (disposition, str(record_id)),
                    (disposition, str(sibling_record_id)),
                }
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


@pytest.mark.parametrize("disposition", ["delete", "adopt"])
def test_admin_disposition_mutates_private_local_store_and_resumes_between_objects(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
    disposition: str,
) -> None:
    async def _run() -> None:
        root = tmp_path / "recovery"
        root.mkdir(mode=0o700)
        conn = await connect(migrated_url)
        local_ports, local_adapter = _local_ports(root)
        try:
            async with pool(migrated_url) as conn_pool:
                system_id = await seed_system(conn)
                publisher = f"publisher-{uuid4()}"
                await conn.execute(
                    "INSERT INTO worker_incarnations "
                    "(incarnation, authority_kind, authority_binding, credential_hash, "
                    "fence_protocol) VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
                    (publisher, hashlib.sha256(b"publisher-credential").digest()),
                )
                authority_repository = DatabaseAuthorityRepository(
                    lambda: _authority_connection(authority_role_dsns("kdive_provider_authority"))
                )
                published: list[tuple[str, UUID, ExternalBootActivationBinding, Path]] = []
                for ordinal in range(3):
                    run_id = await seed_run(conn, system_id)
                    async with conn.transaction():
                        activation = await seed_activation(
                            conn,
                            state=ExternalBootActivationState.RECOVERED,
                            cleanup_complete=True,
                            system_id=system_id,
                            run_id=run_id,
                        )
                    published.append(
                        await _publish_local_quarantine(
                            conn,
                            authority_repository,
                            local_adapter,
                            root=root,
                            publisher=publisher,
                            system_id=system_id,
                            run_id=run_id,
                            activation_id=activation.activation.id,
                            plan_identity=activation.activation.plan_identity,
                            ordinal=ordinal,
                        )
                    )

                selected = sorted(published, key=lambda item: item[0])[:2]
                sibling = next(item for item in published if item not in selected)
                executor = _InterruptBeforeSecondDisposition(local_adapter)
                executor.interrupt_record_id = str(selected[1][1])
                orphan_service = RecoveryOrphanAuthorityService(
                    lambda: _authority_connection(authority_role_dsns("kdive_provider_authority")),
                    local_ports,
                    executor,
                )
                service = ExternalBootAuthorityService(
                    repository=authority_repository,
                    journal_factory=lambda _: (_ for _ in ()).throw(
                        AssertionError("orphan disposition must not open a mutation journal")
                    ),
                    adapter=local_adapter,
                    recovery_orphans=orphan_service,
                )
                resolver = provider_resolver(external_boot_recovery_objects=local_ports)
                admitted = await resolve_recovery_orphan(
                    conn_pool,
                    _ctx(),
                    resolver=resolver,
                    system_id=str(system_id),
                    object_identities=[item[0] for item in selected],
                    disposition=disposition,
                )
                first_worker = f"worker-{uuid4()}"
                second_worker = f"worker-{uuid4()}"
                await conn.execute(
                    "INSERT INTO worker_incarnations "
                    "(incarnation, authority_kind, authority_binding, credential_hash, "
                    "fence_protocol) VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
                    (first_worker, hashlib.sha256(b"first-credential").digest()),
                )
                await conn.execute(
                    "INSERT INTO worker_incarnations "
                    "(incarnation, authority_kind, authority_binding, credential_hash, "
                    "fence_protocol) VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
                    (second_worker, hashlib.sha256(b"second-credential").digest()),
                )

                async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
                    peers = {
                        "first-credential": first_worker,
                        "second-credential": second_worker,
                    }
                    return AuthenticatedPeer(peers[credential.get_secret_value()])

                class Backend:
                    def __init__(self) -> None:
                        self.delivered_interruption = False

                    async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
                        assert deadline > asyncio.get_running_loop().time()
                        response = await _dispatch(envelope, authenticate, service)
                        if executor.interrupted and not self.delivered_interruption:
                            self.delivered_interruption = True
                            raise TimeoutError(
                                "worker lost response after first local disposition commit"
                            )
                        return response

                backend = Backend()

                def registry_for(sender: AuthorityRequestSender) -> HandlerRegistry:
                    registry = HandlerRegistry()
                    registry.register(
                        JobKind.RESOLVE_RECOVERY_ORPHAN,
                        lambda handler_conn, job: resolve_recovery_orphan_handler(
                            handler_conn, job, sender_factory=lambda: sender
                        ),
                    )
                    return registry

                first_sender = AuthorityRequestSender(
                    lambda: backend, lambda: SecretStr("first-credential")
                )
                async with AsyncConnectionPool(
                    authority_role_dsns("kdive_worker"), min_size=1, max_size=4
                ) as worker_pool:
                    first_runner = Worker(
                        worker_pool,
                        registry_for(first_sender),
                        worker_id=first_worker,
                        incarnation_credential=SecretStr("first-credential"),
                        secret_registry=SecretRegistry(),
                        config=WorkerConfig(accepted_lanes=(DEFAULT_JOB_DISPATCH_LANE,)),
                    )
                    interrupted = await first_runner.run_once(DEFAULT_JOB_DISPATCH_LANE)
                assert interrupted is not None and str(interrupted.id) == admitted.object_id
                expected_status = "deleted" if disposition == "delete" else "adopted"
                after_interruption = await (
                    await conn.execute(
                        "SELECT id, status FROM external_boot_recovery_quarantine "
                        "WHERE id = ANY(%s) ORDER BY object_identity",
                        ([item[1] for item in published],),
                    )
                ).fetchall()
                assert after_interruption == [
                    (selected[0][1], expected_status),
                    (selected[1][1], "quarantined"),
                    (sibling[1], "quarantined"),
                ]
                assert executor.mutations == [(disposition, str(selected[0][1]))]
                queued = await (
                    await conn.execute(
                        "SELECT state, attempt, worker_id, lease_expires_at "
                        "FROM jobs WHERE id = %s",
                        (interrupted.id,),
                    )
                ).fetchone()
                assert queued == ("queued", 1, None, None)

                with RecoveryMetadataStore(root) as store:
                    first_receipt = store.read_cleanup_quarantine(selected[0][2])
                    second_receipt = store.read_cleanup_quarantine(selected[1][2])
                    sibling_receipt = store.read_cleanup_quarantine(sibling[2])
                if disposition == "delete":
                    assert first_receipt is None and not selected[0][3].exists()
                else:
                    assert first_receipt is not None and first_receipt.managed
                assert second_receipt is not None and not second_receipt.managed
                assert sibling_receipt is not None and not sibling_receipt.managed
                assert sibling[3].is_dir()

                await conn.execute(
                    "UPDATE worker_incarnations SET state = 'terminated', terminated_at = now(), "
                    "outcome = 'killed' WHERE incarnation = %s",
                    (first_worker,),
                )
                second_sender = AuthorityRequestSender(
                    lambda: backend, lambda: SecretStr("second-credential")
                )
                async with AsyncConnectionPool(
                    authority_role_dsns("kdive_worker"), min_size=1, max_size=4
                ) as worker_pool:
                    second_runner = Worker(
                        worker_pool,
                        registry_for(second_sender),
                        worker_id=second_worker,
                        incarnation_credential=SecretStr("second-credential"),
                        secret_registry=SecretRegistry(),
                        config=WorkerConfig(accepted_lanes=(DEFAULT_JOB_DISPATCH_LANE,)),
                    )
                    completed = await second_runner.run_once(DEFAULT_JOB_DISPATCH_LANE)
                assert completed is not None and completed.attempt == interrupted.attempt + 1
                assert executor.mutations == [
                    (disposition, str(selected[0][1])),
                    (disposition, str(selected[1][1])),
                ]
                final_rows = await (
                    await conn.execute(
                        "SELECT id, status FROM external_boot_recovery_quarantine "
                        "WHERE id = ANY(%s) ORDER BY object_identity",
                        ([item[1] for item in published],),
                    )
                ).fetchall()
                assert final_rows == [
                    (selected[0][1], expected_status),
                    (selected[1][1], expected_status),
                    (sibling[1], "quarantined"),
                ]
                with RecoveryMetadataStore(root) as store:
                    selected_receipts = [
                        store.read_cleanup_quarantine(item[2]) for item in selected
                    ]
                    sibling_receipt = store.read_cleanup_quarantine(sibling[2])
                if disposition == "delete":
                    assert selected_receipts == [None, None]
                    assert not any(item[3].exists() for item in selected)
                else:
                    assert all(
                        receipt is not None and receipt.managed for receipt in selected_receipts
                    )
                assert sibling_receipt is not None and not sibling_receipt.managed
                assert sibling[3].is_dir()
                replay = await resolve_recovery_orphan(
                    conn_pool,
                    _ctx(),
                    resolver=resolver,
                    system_id=str(system_id),
                    object_identities=[item[0] for item in selected],
                    disposition=disposition,
                )
                assert replay.object_id == admitted.object_id
        finally:
            local_adapter.close()
            await conn.close()

    asyncio.run(_run())
