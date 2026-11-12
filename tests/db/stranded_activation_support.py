"""The stranded-activation fixture shared by the settlement and observation tests (ADR-0816)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.repositories import JOBS
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.external_boot.stranded import observe_stranded_activation_handler
from kdive.jobs.worker import _failure_context
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityStrandedObservationRequestV1,
    AuthorityStrandedObservationResponseV1,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    ExternalBootAuthorityService,
)
from kdive.providers.external_boot_authority.stranded import (
    StrandedAuthorityFacts,
    StrandedObservationRepository,
    StrandedTarget,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.db.external_boot_authority_support import (
    _allocate,
    _Allocated,
    _AuthorityCase,
    _RoleDsns,
    _seed_case,
)
from tests.providers.external_boot_authority.service_support import (
    _Adapter,
    _Repository,
    _takeover,
)

_DOMAIN = "kdive-stranded"
_SET_ALL_NAMED_OBJECTS = (
    "UPDATE external_boot_activations SET materialization = jsonb_build_object("
    "'schema', 'external-boot-materialization-v1', "
    "'ownership', jsonb_build_object('system_id', system_id::text, 'run_id', run_id::text), "
    "'plan_identity', plan_identity, 'artifacts', jsonb_build_object("
    "'kernel', jsonb_build_object('ref', 'held/kernel'), "
    "'modules', jsonb_build_object('ref', 'held/modules'), "
    "'initrd', jsonb_build_object('ref', 'held/initrd'), "
    "'debuginfo', jsonb_build_object('ref', 'held/debuginfo'))), "
    "recovery_point = jsonb_build_object('schema', 'external-boot-recovery-v1', "
    "'binding', jsonb_build_object('system_id', system_id::text, 'run_id', run_id::text, "
    "'activation_id', id::text), 'plan_identity', plan_identity, "
    "'recovery_ref', jsonb_build_object('ref', 'held/recovery-point')), "
    "pre_recovery_evidence = jsonb_build_object("
    "'schema', 'external-boot-pre-recovery-evidence-v1', 'activation_id', id::text, "
    "'system_id', system_id::text, 'run_id', run_id::text, 'plan_identity', plan_identity, "
    "'recovery_object', jsonb_build_object('ref', 'held/pre-recovery')) "
    "WHERE id = {activation}"
)
_ALL_NAMED_OBJECTS = (
    "held/kernel",
    "held/modules",
    "held/initrd",
    "held/debuginfo",
    "held/recovery-point",
    "held/pre-recovery",
)
_SET_ABANDONED = (
    "UPDATE external_boot_activations SET state = 'abandoned', terminal_evidence = "
    "jsonb_build_object('schema', 'external-boot-terminal-evidence-v1', "
    "'activation_id', id::text, 'system_id', system_id::text, 'outcome', 'abandoned') "
    "WHERE id = {activation}"
)


@dataclass(frozen=True, slots=True)
class _Stranded:
    case: _AuthorityCase
    observer_job: UUID
    authority: _Allocated | None


def _strand(
    migrated_url: str, role_dsns: _RoleDsns, *, suffix: str = "s", allocate: bool = True
) -> _Stranded:
    """Build the #3138 shape: a failed activate job over an unprepared activation."""
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix=suffix)
        conn.execute(
            "UPDATE external_boot_activations SET state = 'preparing', materialization = NULL, "
            "recovery_point = NULL WHERE id = %s",
            (case.activation_id,),
        )
        conn.execute("UPDATE systems SET domain_name = %s WHERE id = %s", (_DOMAIN, case.system_id))
        conn.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id, store_identity, owner_key, reserved_bytes, state) "
            "VALUES (%s, 'store/private', 'owner/private', 4096, 'pending')",
            (case.activation_id,),
        )
        conn.execute(
            "INSERT INTO remote_module_attempt_obligations (system_id, run_id, operation_nonce) "
            "VALUES (%s, %s, %s)",
            (case.system_id, case.run_id, "1" * 32),
        )
    authority = None
    if allocate:
        with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
            authority = _allocate(worker, case)
    observer_job = uuid4()
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state = 'failed', worker_id = NULL, lease_expires_at = NULL, "
            "heartbeat_at = NULL, error_category = 'conflict' WHERE id = %s",
            (case.job_id,),
        )
        conn.execute(
            "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
            "(%s, 'teardown', %s, 'running', 1, 3, %s, now() + interval '5 minutes', now(), "
            "%s, %s)",
            (
                observer_job,
                Jsonb({"system_id": str(case.system_id)}),
                case.worker_id,
                Jsonb({"principal": "p", "project": "proj"}),
                f"observer-{observer_job}",
            ),
        )
    return _Stranded(case, observer_job, authority)


def _mark_authority_owned(migrated_url: str, case: _AuthorityCase) -> None:
    digest = "sha256:" + "f" * 64
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO authority_system_ownership (system_id, allocation_id, resource_id, "
            "provider_kind, resource_name, authority_instance, profile_identity, root_identity) "
            "SELECT %s, id, resource_id, 'local-libvirt', 'resource', %s, %s, %s "
            "FROM allocations WHERE id = %s",
            (case.system_id, case.authority_instance, digest, digest, case.allocation_id),
        )


# Each daemon is faked, because that is the boundary: the worker's presence port and the
# authority's provider adapter. Everything behind them runs against real Postgres roles.
_CREDENTIAL = SecretStr("worker-incarnation-credential")
_PEER = AuthenticatedPeer(uuid4())
_RECOVERY_REF = "local-recovery/ref"


class _WorkerDaemon:
    def __init__(self, *, defined: bool = True) -> None:
        self.defined = defined

    def is_defined(self, domain_name: str) -> bool:
        assert domain_name == _DOMAIN
        return self.defined


class _AuthorityDaemon(_Adapter):
    def __init__(self, *, domain_absent: bool = True, store_absent: bool = True) -> None:
        super().__init__()
        self.domain_absent = domain_absent
        self.store_absent = store_absent

    async def observe_stranded_absence(
        self, activation_id: UUID, target: StrandedTarget
    ) -> StrandedAuthorityFacts:
        return StrandedAuthorityFacts(self.domain_absent, self.store_absent, _RECOVERY_REF)


class _Sender:
    def __init__(self, service: ExternalBootAuthorityService) -> None:
        self._service = service

    async def observe_stranded_activation(
        self, request: AuthorityStrandedObservationRequestV1, *, deadline: float
    ) -> AuthorityStrandedObservationResponseV1:
        return await self._service.observe_stranded_activation(_PEER, request)


def _authority_service(
    tmp_path: Path, role_dsns: _RoleDsns, instance: str, adapter: _Adapter
) -> ExternalBootAuthorityService:
    @asynccontextmanager
    async def connections() -> AsyncIterator[AsyncConnection]:
        async with await AsyncConnection.connect(
            role_dsns("kdive_provider_authority"), autocommit=True
        ) as conn:
            yield conn

    return ExternalBootAuthorityService(
        repository=_Repository(_PEER, _takeover()),
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
        stranded_observations=StrandedObservationRepository(connections, instance),
    )


def _resolver(presence: _WorkerDaemon, service: ExternalBootAuthorityService) -> Any:
    capability = SimpleNamespace(sender=_Sender(service), missing_route_reason=None)
    runtime = SimpleNamespace(domain_presence=presence, authority=capability)

    class Resolver:
        async def binding_for_system(self, conn: Any, system_id: UUID) -> Any:
            return SimpleNamespace(runtime=runtime)

    return Resolver()


def _claim(migrated_url: str, job_id: UUID, worker_id: str) -> None:
    """Stand in for `queue.dequeue`, whose fencing has its own tests."""
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state = 'running', attempt = 1, worker_id = %s, "
            "heartbeat_at = now(), lease_expires_at = now() + interval '5 minutes' WHERE id = %s",
            (worker_id, job_id),
        )
        conn.execute(
            "UPDATE worker_incarnations SET credential_hash = %s WHERE incarnation = %s",
            (hashlib.sha256(_CREDENTIAL.get_secret_value().encode()).digest(), worker_id),
        )


async def _run_job(
    migrated_url: str,
    role_dsns: _RoleDsns,
    job: Job,
    worker_id: str,
    presence: _WorkerDaemon,
    service: ExternalBootAuthorityService,
) -> str:
    _claim(migrated_url, job.id, worker_id)
    async with await AsyncConnection.connect(role_dsns("kdive_worker"), autocommit=True) as conn:
        claimed = await JOBS.get(conn, job.id)
        assert claimed is not None
        return await observe_stranded_activation_handler(
            conn, claimed, resolver=_resolver(presence, service), incarnation_credential=_CREDENTIAL
        )


def _fail_job(migrated_url: str, job_id: UUID, error: CategorizedError) -> None:
    """Commit the failure the way the worker does, so the read proves the real keys."""
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state = 'failed', worker_id = NULL, lease_expires_at = NULL, "
            "heartbeat_at = NULL, error_category = %s, failure_context = %s WHERE id = %s",
            (error.category.value, Jsonb(_failure_context(error, SecretRegistry())), job_id),
        )
