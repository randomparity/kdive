"""Both producers driving a stranded activation to settlement (#3574, ADR-0816).

Real Postgres under the real roles. Only each daemon is faked, because that is the boundary: the
worker's presence port and the authority's provider adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.external_boot_activations import SettlementStatus
from kdive.db.repositories import JOBS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs.handlers.external_boot.stranded import observe_stranded_activation_handler
from kdive.jobs.worker import _failure_context
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityStrandedObservationRequestV1,
    AuthorityStrandedObservationResponseV1,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
    ExternalBootAuthorityService,
)
from kdive.providers.external_boot_authority.stranded import (
    StrandedAuthorityFacts,
    StrandedObservationRepository,
    StrandedTarget,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.external_boot.settlement import (
    read_stranded_observation,
    request_stranded_observation,
    settle_stranded_activation,
)
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.stranded_activation_support import _DOMAIN, _mark_authority_owned, _strand
from tests.providers.external_boot_authority.service_support import (
    _Adapter,
    _Repository,
    _takeover,
)
from tests.support.external_boot_plan import external_boot_materialization, external_boot_plan

_ADMIN = RequestContext(
    principal="admin",
    agent_session="session",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
)
_AUDITOR = RequestContext(
    principal="auditor",
    agent_session=None,
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}),
)
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


async def _request(role_dsns: _RoleDsns, activation_id: UUID) -> Job:
    async with await AsyncConnection.connect(role_dsns("kdive_server"), autocommit=True) as conn:
        return await request_stranded_observation(conn, _ADMIN, activation_id=activation_id)


async def _read(role_dsns: _RoleDsns, activation_id: UUID) -> Any:
    async with await AsyncConnection.connect(role_dsns("kdive_server"), autocommit=True) as conn:
        return await read_stranded_observation(conn, _ADMIN, activation_id=activation_id)


async def _settle(role_dsns: _RoleDsns, activation_id: UUID) -> SettlementStatus:
    async with await AsyncConnection.connect(role_dsns("kdive_server"), autocommit=True) as conn:
        return await settle_stranded_activation(
            conn, _ADMIN, activation_id=activation_id, actor="operator-cli", reason="stranded"
        )


async def _settlement_refusal(role_dsns: _RoleDsns, activation_id: UUID) -> object:
    with pytest.raises(CategorizedError) as raised:
        await _settle(role_dsns, activation_id)
    return raised.value.details["reason"]


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


def _settlement_row(migrated_url: str, activation_id: UUID) -> tuple[Any, ...] | None:
    with psycopg.connect(migrated_url) as conn:
        return conn.execute(
            "SELECT worker_recorded_at IS NOT NULL, authority_recorded_at IS NOT NULL "
            "FROM external_boot_activation_settlements WHERE activation_id = %s",
            (activation_id,),
        ).fetchone()


def _materialize(migrated_url: str, activation_id: UUID, system_id: UUID, run_id: UUID) -> None:
    materialization = external_boot_materialization(external_boot_plan(system_id, run_id))
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT plan_identity FROM external_boot_activations WHERE id = %s", (activation_id,)
        ).fetchone()
        assert row is not None
        stored = materialization.model_copy(update={"plan_identity": row[0]})
        conn.execute(
            "UPDATE external_boot_activations SET materialization = %s WHERE id = %s",
            (Jsonb(stored.model_dump(mode="json", by_alias=True)), activation_id),
        )


def test_both_producers_settle_the_stranded_shape(
    migrated_url: str, authority_role_dsns: _RoleDsns, tmp_path: Path
) -> None:
    case = _strand(migrated_url, authority_role_dsns).case
    service = _authority_service(
        tmp_path, authority_role_dsns, case.authority_instance, _AuthorityDaemon()
    )

    async def exercise() -> None:
        assert await _read(authority_role_dsns, case.activation_id) is None
        job = await _request(authority_role_dsns, case.activation_id)
        assert job.kind is JobKind.OBSERVE_STRANDED_ACTIVATION
        assert "external_boot_authority_v1" not in job.payload
        result = await _run_job(
            migrated_url, authority_role_dsns, job, case.worker_id, _WorkerDaemon(), service
        )
        assert '"disposition":"recorded"' in result
        status = await _settle(authority_role_dsns, case.activation_id)
        assert status is SettlementStatus.APPLIED
        with pytest.raises(CategorizedError) as past_preparing:
            await _request(authority_role_dsns, case.activation_id)
        assert past_preparing.value.details["reason"] == "not_preparing"

    asyncio.run(exercise())


def test_a_second_request_while_live_returns_the_same_job(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _strand(migrated_url, authority_role_dsns).case

    async def exercise() -> tuple[Job, Job]:
        first = await _request(authority_role_dsns, case.activation_id)
        return first, await _request(authority_role_dsns, case.activation_id)

    first, second = asyncio.run(exercise())

    assert first.id == second.id
    assert second.state is JobState.QUEUED


def test_an_authority_owned_system_is_refused_at_every_layer(
    migrated_url: str, authority_role_dsns: _RoleDsns, tmp_path: Path
) -> None:
    case = _strand(migrated_url, authority_role_dsns).case
    _mark_authority_owned(migrated_url, case)
    adapter = _AuthorityDaemon()
    service = _authority_service(tmp_path, authority_role_dsns, case.authority_instance, adapter)

    async def exercise() -> None:
        with pytest.raises(CategorizedError) as requested:
            await _request(authority_role_dsns, case.activation_id)
        assert requested.value.details["reason"] == "authority_owned"
        with pytest.raises(AuthorityServiceError) as observed:
            await service.observe_stranded_activation(
                _PEER, AuthorityStrandedObservationRequestV1(activation_id=case.activation_id)
            )
        assert observed.value.category == "superseded"
        reason = await _settlement_refusal(authority_role_dsns, case.activation_id)
        assert reason == "authority_owned"

    asyncio.run(exercise())

    assert _settlement_row(migrated_url, case.activation_id) is None


def test_a_domain_on_the_authority_leaves_only_the_worker_record(
    migrated_url: str, authority_role_dsns: _RoleDsns, tmp_path: Path
) -> None:
    case = _strand(migrated_url, authority_role_dsns).case
    service = _authority_service(
        tmp_path,
        authority_role_dsns,
        case.authority_instance,
        _AuthorityDaemon(domain_absent=False),
    )

    async def exercise() -> None:
        job = await _request(authority_role_dsns, case.activation_id)
        with pytest.raises(CategorizedError) as raised:
            await _run_job(
                migrated_url, authority_role_dsns, job, case.worker_id, _WorkerDaemon(), service
            )
        assert raised.value.details["reason"] == "authority_domain_present"
        reason = await _settlement_refusal(authority_role_dsns, case.activation_id)
        assert reason == "authority_evidence_missing"

    asyncio.run(exercise())

    assert _settlement_row(migrated_url, case.activation_id) == (True, False)


def test_a_domain_absent_from_the_worker_records_neither_owner(
    migrated_url: str, authority_role_dsns: _RoleDsns, tmp_path: Path
) -> None:
    case = _strand(migrated_url, authority_role_dsns).case
    service = _authority_service(
        tmp_path, authority_role_dsns, case.authority_instance, _AuthorityDaemon()
    )

    async def exercise() -> None:
        job = await _request(authority_role_dsns, case.activation_id)
        with pytest.raises(CategorizedError) as raised:
            await _run_job(
                migrated_url,
                authority_role_dsns,
                job,
                case.worker_id,
                _WorkerDaemon(defined=False),
                service,
            )
        assert raised.value.details["reason"] == "worker_domain_absent"
        reason = await _settlement_refusal(authority_role_dsns, case.activation_id)
        assert reason == "authority_evidence_missing"

    asyncio.run(exercise())

    assert _settlement_row(migrated_url, case.activation_id) is None


def test_held_objects_refuse_read_back_and_settle_once_gone(
    migrated_url: str, authority_role_dsns: _RoleDsns, tmp_path: Path
) -> None:
    case = _strand(migrated_url, authority_role_dsns).case
    _materialize(migrated_url, case.activation_id, case.system_id, case.run_id)
    adapter = _AuthorityDaemon(store_absent=False)
    service = _authority_service(tmp_path, authority_role_dsns, case.authority_instance, adapter)

    async def exercise() -> None:
        job = await _request(authority_role_dsns, case.activation_id)
        with pytest.raises(CategorizedError) as raised:
            await _run_job(
                migrated_url, authority_role_dsns, job, case.worker_id, _WorkerDaemon(), service
            )
        _fail_job(migrated_url, job.id, raised.value)

        outcome = await _read(authority_role_dsns, case.activation_id)
        assert outcome.job_id == job.id
        assert outcome.state is JobState.FAILED
        assert outcome.reason == "authority_objects_present"
        assert _RECOVERY_REF in outcome.message
        assert "artifact/kernel" in outcome.message
        assert "artifact/modules" in outcome.message
        reason = await _settlement_refusal(authority_role_dsns, case.activation_id)
        assert reason == "authority_evidence_missing"

        adapter.store_absent = True
        again = await _request(authority_role_dsns, case.activation_id)
        assert again.id == job.id
        assert again.state is JobState.QUEUED
        await _run_job(
            migrated_url, authority_role_dsns, again, case.worker_id, _WorkerDaemon(), service
        )
        status = await _settle(authority_role_dsns, case.activation_id)
        assert status is SettlementStatus.APPLIED

    asyncio.run(exercise())


def test_request_refuses_an_unknown_activation(authority_role_dsns: _RoleDsns) -> None:
    with pytest.raises(CategorizedError) as raised:
        asyncio.run(_request(authority_role_dsns, uuid4()))

    assert raised.value.details["reason"] == "not_found"
    assert raised.value.category is ErrorCategory.NOT_FOUND


@pytest.mark.parametrize("call", [request_stranded_observation, read_stranded_observation])
def test_both_entries_require_platform_admin(call: Any) -> None:
    with pytest.raises(AuthorizationError):
        asyncio.run(call(cast(AsyncConnection, object()), _AUDITOR, activation_id=uuid4()))
