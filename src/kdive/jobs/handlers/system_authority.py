"""Worker execution of activation-free authority System operations (ADR-0623)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.external_boot_authority_client import AuthoritySystemSenderFactory
from kdive.jobs.handlers.system_reclaim import (
    RetiredKeyBatchDeleter,
    reclaim_system_core_after_provider_teardown,
)
from kdive.jobs.payloads import SystemPayload, TeardownPayload, load_payload
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.system_authority.protocol import (
    AuthoritySystemAcknowledgementV1,
    AuthoritySystemMarkerV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemPreactivationAbsentV1,
    AuthoritySystemResponseV1,
    AuthoritySystemTakeoverRequestV1,
    canonical_system_authority_bytes,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.system_bootstrap_key import ensure_system_bootstrap_key


@dataclass(frozen=True, slots=True)
class AuthoritySystemWorkerPorts:
    resolver: ProviderResolver
    incarnation_credential: SecretStr
    secret_registry: SecretRegistry
    sender_factory: AuthoritySystemSenderFactory | None
    artifact_store: RetiredKeyBatchDeleter
    request_timeout: timedelta = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class _Allocated:
    authority_id: UUID
    generation: int
    operation_digest: str


def _refuse(reason: str) -> CategorizedError:
    return CategorizedError(
        f"authority System: {reason}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        terminal=True,
    )


def _marker(job: Job) -> AuthoritySystemMarkerV1:
    payload = (
        load_payload(job, SystemPayload)
        if job.kind is JobKind.PROVISION
        else load_payload(job, TeardownPayload)
    )
    marker = payload.authority_system_v1
    if marker is None:
        raise _refuse("marker-missing")
    expected = "provision" if job.kind is JobKind.PROVISION else "preactivation-teardown"
    if marker.operation.value != expected:
        raise _refuse("operation-mismatch")
    return AuthoritySystemMarkerV1.model_validate(marker.model_dump(mode="python", by_alias=True))


async def _allocate(
    conn: AsyncConnection,
    job: Job,
    request_attempt_id: UUID,
    credential: SecretStr,
) -> _Allocated:
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT * FROM public.allocate_authority_system_attempt("
            "sha256(convert_to(%s,'UTF8')),%s,%s,%s)",
            (credential.get_secret_value(), job.id, job.attempt, request_attempt_id),
        )
        row = await cursor.fetchone()
    if row is None or row["status"] not in {"allocated", "replay"}:
        raise _refuse("attempt-" + ("missing" if row is None else str(row["status"])))
    if row["authority_id"] is None or row["generation"] is None or row["operation_digest"] is None:
        raise _refuse("attempt-malformed")
    return _Allocated(row["authority_id"], row["generation"], row["operation_digest"])


async def _acknowledge(
    conn: AsyncConnection,
    job: Job,
    allocated: _Allocated,
    request_attempt_id: UUID,
    acknowledgement: AuthoritySystemAcknowledgementV1,
    credential: SecretStr,
) -> None:
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT * FROM public.acknowledge_authority_system_attempt("
            "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                credential.get_secret_value(),
                job.id,
                job.attempt,
                allocated.authority_id,
                allocated.generation,
                request_attempt_id,
                acknowledgement.journal_sequence,
                acknowledgement.journal_digest,
                acknowledgement.quiescence_digest,
            ),
        )
        row = await cursor.fetchone()
    if row is None or row["status"] not in {"acknowledged", "replay"}:
        raise _refuse("acknowledgement-" + ("missing" if row is None else str(row["status"])))


async def _finalize(
    conn: AsyncConnection,
    job: Job,
    allocated: _Allocated,
    response: AuthoritySystemResponseV1,
    credential: SecretStr,
) -> None:
    receipt = canonical_system_authority_bytes(response.proof)
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT * FROM public.finalize_authority_system_attempt("
            "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s)",
            (
                credential.get_secret_value(),
                job.id,
                job.attempt,
                allocated.authority_id,
                allocated.generation,
                response.journal_sequence,
                response.journal_digest,
                receipt,
            ),
        )
        row = await cursor.fetchone()
    if row is None or row["status"] not in {"applied", "retained"}:
        raise _refuse("finalization-" + ("missing" if row is None else str(row["status"])))


def _takeover(
    marker: AuthoritySystemMarkerV1,
    allocated: _Allocated,
    request_attempt_id: UUID,
    bootstrap_identity: str,
) -> AuthoritySystemTakeoverRequestV1:
    return AuthoritySystemTakeoverRequestV1.model_validate(
        {
            **marker.model_dump(mode="python", by_alias=True, exclude={"schema_"}),
            "authority_id": allocated.authority_id,
            "generation": allocated.generation,
            "attempt_id": request_attempt_id,
            "operation_digest": allocated.operation_digest,
            "bootstrap_identity": bootstrap_identity,
        }
    )


async def _replay_once[T](call: Callable[[], Awaitable[T]]) -> T:
    """Replay one exact idempotent host request after an infrastructure ambiguity."""
    try:
        return await call()
    except CategorizedError as exc:
        if exc.category is not ErrorCategory.INFRASTRUCTURE_FAILURE:
            raise
    return await call()


async def execute_authority_system_job(
    conn: AsyncConnection,
    job: Job,
    *,
    ports: AuthoritySystemWorkerPorts,
) -> AuthoritySystemResponseV1:
    """Execute one marked System operation without touching the worker provider port."""
    marker = _marker(job)
    if ports.sender_factory is None:
        raise _refuse("route-not-configured")
    binding = await ports.resolver.binding_for_system(conn, marker.system_id)
    sender: AuthorityRequestSender = ports.sender_factory(binding, marker)
    async with conn.transaction():
        public_key = await ensure_system_bootstrap_key(
            conn, marker.system_id, secret_registry=ports.secret_registry
        )
    bootstrap_identity = "sha256:" + hashlib.sha256(public_key.encode()).hexdigest()
    request_attempt_id = uuid5(NAMESPACE_URL, f"kdive:authority-system:{job.id}:{job.attempt}")
    allocated = await _allocate(conn, job, request_attempt_id, ports.incarnation_credential)
    takeover = _takeover(marker, allocated, request_attempt_id, bootstrap_identity)
    deadline = asyncio.get_running_loop().time() + ports.request_timeout.total_seconds()
    acknowledgement = await _replay_once(
        lambda: sender.acknowledge_system_takeover(takeover, deadline=deadline)
    )
    await _acknowledge(
        conn,
        job,
        allocated,
        request_attempt_id,
        acknowledgement,
        ports.incarnation_credential,
    )
    mutation = AuthoritySystemMutationRequestV1.model_validate(
        takeover.model_dump(mode="python", by_alias=True)
    )
    response = await _replay_once(
        lambda: sender.execute_system_operation(mutation, acknowledgement, deadline=deadline)
    )
    if isinstance(response.proof, AuthoritySystemPreactivationAbsentV1):
        await reclaim_system_core_after_provider_teardown(
            conn,
            ports.artifact_store,
            marker.system_id,
            reclaim_snapshot_ledger=True,
            discharge_mutation_obligations=True,
        )
    await _finalize(conn, job, allocated, response, ports.incarnation_credential)
    return response
