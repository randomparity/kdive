"""Credential-fenced persistence for supervised capture operations (ADR-0558)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.catalog.artifacts import Artifact
from kdive.security.audit import AuditEvent, args_digest

type CaptureOperationState = Literal["launching", "gated", "running", "cancel_requested", "exited"]
type CaptureProviderKind = Literal["local-libvirt", "remote-libvirt"]
type CapturePublicationState = Literal[
    "pending", "publishing", "canceling", "published", "discarded"
]


@dataclass(frozen=True, slots=True)
class CaptureOperationIdentity:
    """Exact Linux child identity registered before gate release."""

    host_instance: str
    boot_id: str
    pid: int
    start_ticks: int


@dataclass(frozen=True, slots=True)
class CaptureOperationSnapshot:
    """Authority-independent provider request identity linked to a charged attempt."""

    provider_kind: CaptureProviderKind
    resource_id: UUID
    system_id: UUID
    domain_name: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class RecoveryEvidence:
    """Observed process/provider evidence; database authority decides who may use it."""

    process_absent: bool
    provider_quiescence: Mapping[str, object]
    exit_outcome: str
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class CaptureOperation:
    """One durable provider-mutation boundary for an exact job attempt."""

    id: UUID
    job_id: UUID
    job_attempt: int
    worker_incarnation: str
    provider_kind: CaptureProviderKind
    resource_id: UUID
    system_id: UUID
    domain_name: str
    request_digest: str
    launch_token: str
    host_instance: str
    boot_id: str | None
    pid: int | None
    start_ticks: int | None
    state: CaptureOperationState
    exit_outcome: str | None
    exit_code: int | None
    process_absent: bool
    provider_quiescence: dict[str, Any]
    recovered_by: str | None
    publication_state: CapturePublicationState
    publication_object_key: str | None
    publication_etag: str | None
    publication_artifact_id: UUID | None
    cleanup_capture_version_id: str | None
    publication_tombstone_version: str | None
    publication_started_at: datetime | None
    publication_closed_at: datetime | None
    spool_disposed_at: datetime | None
    created_at: datetime
    identity_recorded_at: datetime | None
    running_at: datetime | None
    cancel_requested_at: datetime | None
    exited_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CaptureRecoveryCandidate:
    """Bounded durable identity exposed only to an authority-eligible replacement."""

    id: UUID
    job_id: UUID
    job_attempt: int
    worker_incarnation: str
    provider_kind: CaptureProviderKind
    resource_id: UUID
    system_id: UUID
    domain_name: str
    launch_token: str | None
    host_instance: str
    boot_id: str | None
    pid: int | None
    start_ticks: int | None
    state: CaptureOperationState


def _record(row: Mapping[str, Any]) -> CaptureOperation:
    return CaptureOperation(
        id=cast(UUID, row["id"]),
        job_id=cast(UUID, row["job_id"]),
        job_attempt=cast(int, row["job_attempt"]),
        worker_incarnation=cast(str, row["worker_incarnation"]),
        provider_kind=cast(CaptureProviderKind, row["provider_kind"]),
        resource_id=cast(UUID, row["resource_id"]),
        system_id=cast(UUID, row["system_id"]),
        domain_name=cast(str, row["domain_name"]),
        request_digest=cast(str, row["request_digest"]),
        launch_token=cast(str, row["launch_token"]),
        host_instance=cast(str, row["host_instance"]),
        boot_id=cast(str | None, row["boot_id"]),
        pid=cast(int | None, row["pid"]),
        start_ticks=cast(int | None, row["start_ticks"]),
        state=cast(CaptureOperationState, row["state"]),
        exit_outcome=cast(str | None, row["exit_outcome"]),
        exit_code=cast(int | None, row["exit_code"]),
        process_absent=cast(bool, row["process_absent"]),
        provider_quiescence=cast(dict[str, Any], row["provider_quiescence"]),
        recovered_by=cast(str | None, row["recovered_by"]),
        publication_state=cast(CapturePublicationState, row["publication_state"]),
        publication_object_key=cast(str | None, row["publication_object_key"]),
        publication_etag=cast(str | None, row["publication_etag"]),
        publication_artifact_id=cast(UUID | None, row["publication_artifact_id"]),
        cleanup_capture_version_id=cast(str | None, row["cleanup_capture_version_id"]),
        publication_tombstone_version=cast(str | None, row["publication_tombstone_version"]),
        publication_started_at=cast(datetime | None, row["publication_started_at"]),
        publication_closed_at=cast(datetime | None, row["publication_closed_at"]),
        spool_disposed_at=cast(datetime | None, row["spool_disposed_at"]),
        created_at=cast(datetime, row["created_at"]),
        identity_recorded_at=cast(datetime | None, row["identity_recorded_at"]),
        running_at=cast(datetime | None, row["running_at"]),
        cancel_requested_at=cast(datetime | None, row["cancel_requested_at"]),
        exited_at=cast(datetime | None, row["exited_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


async def _operation(
    conn: AsyncConnection,
    query: str,
    parameters: tuple[object, ...],
    *,
    refused: str,
    error: type[ValueError] | type[PermissionError],
) -> CaptureOperation:
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(query, parameters)  # ty: ignore[invalid-argument-type]
        row = await cursor.fetchone()
    if row is None:
        raise error(refused)
    return _record(row)


async def create_launching(
    conn: AsyncConnection,
    credential: SecretStr,
    job_id: UUID,
    attempt: int,
    snapshot: CaptureOperationSnapshot,
) -> CaptureOperation:
    """Create and link the sole launching operation for an exact owned attempt."""
    return await _operation(
        conn,
        "SELECT * FROM public.create_capture_operation("
        "sha256(convert_to(%s, 'UTF8')), %s, %s, %s, %s, %s, %s, %s)",
        (
            credential.get_secret_value(),
            job_id,
            attempt,
            snapshot.provider_kind,
            snapshot.resource_id,
            snapshot.system_id,
            snapshot.domain_name,
            snapshot.request_digest,
        ),
        refused="capture operation launch was refused",
        error=PermissionError,
    )


async def record_identity(
    conn: AsyncConnection,
    credential: SecretStr,
    operation_id: UUID,
    identity: CaptureOperationIdentity,
) -> CaptureOperation:
    """Advance launching to gated after exact child identity is durable."""
    return await _operation(
        conn,
        "SELECT * FROM public.record_capture_operation_identity("
        "sha256(convert_to(%s, 'UTF8')), %s, %s, %s, %s, %s)",
        (
            credential.get_secret_value(),
            operation_id,
            identity.host_instance,
            identity.boot_id,
            identity.pid,
            identity.start_ticks,
        ),
        refused="capture operation transition was refused",
        error=ValueError,
    )


async def mark_running(
    conn: AsyncConnection, credential: SecretStr, operation_id: UUID
) -> CaptureOperation:
    """Record that a gated operation was released to provider code."""
    return await _operation(
        conn,
        "SELECT * FROM public.mark_capture_operation_running(sha256(convert_to(%s, 'UTF8')), %s)",
        (credential.get_secret_value(), operation_id),
        refused="capture operation transition was refused",
        error=ValueError,
    )


async def request_cancel(
    conn: AsyncConnection, credential: SecretStr, operation_id: UUID
) -> CaptureOperation:
    """Move an owned gated or running operation into cancellation."""
    return await _operation(
        conn,
        "SELECT * FROM public.request_capture_operation_cancel(sha256(convert_to(%s, 'UTF8')), %s)",
        (credential.get_secret_value(), operation_id),
        refused="capture operation transition was refused",
        error=PermissionError,
    )


def _evidence_parameters(evidence: RecoveryEvidence) -> tuple[object, ...]:
    return (
        evidence.process_absent,
        Jsonb(dict(evidence.provider_quiescence)),
        evidence.exit_outcome,
        evidence.exit_code,
    )


async def acknowledge_exit(
    conn: AsyncConnection,
    credential: SecretStr,
    operation_id: UUID,
    evidence: RecoveryEvidence,
) -> CaptureOperation:
    """Acknowledge an owned exit only with complete process/provider evidence."""
    return await _operation(
        conn,
        "SELECT * FROM public.acknowledge_capture_operation_exit("
        "sha256(convert_to(%s, 'UTF8')), %s, %s, %s, %s, %s)",
        (credential.get_secret_value(), operation_id, *_evidence_parameters(evidence)),
        refused="capture operation transition was refused",
        error=ValueError,
    )


async def recover_operation(
    conn: AsyncConnection,
    replacement_credential: SecretStr,
    operation_id: UUID,
    evidence: RecoveryEvidence,
) -> CaptureOperation:
    """Recover an operation when durable authority permits this replacement."""
    return await _operation(
        conn,
        "SELECT * FROM public.recover_capture_operation("
        "sha256(convert_to(%s, 'UTF8')), %s, %s, %s, %s, %s)",
        (
            replacement_credential.get_secret_value(),
            operation_id,
            *_evidence_parameters(evidence),
        ),
        refused="capture operation recovery was refused",
        error=PermissionError,
    )


async def list_recovery_candidates(
    conn: AsyncConnection, replacement_credential: SecretStr
) -> tuple[CaptureRecoveryCandidate, ...]:
    """List only nonterminal operations this credential may potentially recover.

    Candidate discovery does not authorize recovery. The final recovery write revalidates the
    durable authority and evidence while holding the same incarnation and operation fences.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT * FROM public.list_capture_recovery_candidates(sha256(convert_to(%s, 'UTF8')))",
            (replacement_credential.get_secret_value(),),
        )
        rows = await cursor.fetchall()
    return tuple(
        CaptureRecoveryCandidate(
            id=cast(UUID, row["id"]),
            job_id=cast(UUID, row["job_id"]),
            job_attempt=cast(int, row["job_attempt"]),
            worker_incarnation=cast(str, row["worker_incarnation"]),
            provider_kind=cast(CaptureProviderKind, row["provider_kind"]),
            resource_id=cast(UUID, row["resource_id"]),
            system_id=cast(UUID, row["system_id"]),
            domain_name=cast(str, row["domain_name"]),
            launch_token=cast(str | None, row["launch_token"]),
            host_instance=cast(str, row["host_instance"]),
            boot_id=cast(str | None, row["boot_id"]),
            pid=cast(int | None, row["pid"]),
            start_ticks=cast(int | None, row["start_ticks"]),
            state=cast(CaptureOperationState, row["state"]),
        )
        for row in rows
    )


async def claim_publication_recovery(
    conn: AsyncConnection,
    replacement_credential: SecretStr,
    operation_id: UUID,
) -> CaptureOperation:
    """Revalidate replacement authority for an exited but publication-incomplete attempt."""
    return await _operation(
        conn,
        "SELECT * FROM public.claim_capture_publication_recovery("
        "sha256(convert_to(%s, 'UTF8')), %s)",
        (replacement_credential.get_secret_value(), operation_id),
        refused="capture publication recovery was refused",
        error=PermissionError,
    )


async def begin_publication(
    conn: AsyncConnection,
    credential: SecretStr,
    operation_id: UUID,
    key: str,
) -> CaptureOperation:
    """Journal the immutable object key before the exact owner starts a capture PUT."""
    return await _operation(
        conn,
        "SELECT * FROM public.begin_capture_publication(sha256(convert_to(%s, 'UTF8')), %s, %s)",
        (credential.get_secret_value(), operation_id, key),
        refused="capture publication transition was refused",
        error=ValueError,
    )


async def begin_cancel_publication(
    conn: AsyncConnection,
    credential: SecretStr,
    operation_id: UUID,
    key: str,
) -> CaptureOperation:
    """Move pending or publishing work monotonically into cancellation."""
    return await _operation(
        conn,
        "SELECT * FROM public.begin_cancel_capture_publication("
        "sha256(convert_to(%s, 'UTF8')), %s, %s)",
        (credential.get_secret_value(), operation_id, key),
        refused="capture publication transition was refused",
        error=ValueError,
    )


async def record_capture_version(
    conn: AsyncConnection,
    credential: SecretStr,
    operation_id: UUID,
    version_id: str,
    etag: str,
) -> CaptureOperation:
    """Journal the exact capture version and etag returned by conditional creation."""
    return await _operation(
        conn,
        "SELECT * FROM public.record_capture_publication_version("
        "sha256(convert_to(%s, 'UTF8')), %s, %s, %s)",
        (credential.get_secret_value(), operation_id, version_id, etag),
        refused="capture publication transition was refused",
        error=ValueError,
    )


def _artifact_facts(artifact: Artifact) -> dict[str, object]:
    return {
        "id": str(artifact.id),
        "owner_kind": artifact.owner_kind,
        "owner_id": str(artifact.owner_id),
        "object_key": artifact.object_key,
        "etag": artifact.etag,
        "sensitivity": artifact.sensitivity.value,
        "retention_class": artifact.retention_class,
        "run_id": None if artifact.run_id is None else str(artifact.run_id),
        "encoding": artifact.encoding,
        "uncompressed_size": artifact.uncompressed_size,
    }


def _audit_facts(event: AuditEvent) -> dict[str, object]:
    return {
        "tool": event.tool,
        "object_kind": event.object_kind,
        "object_id": str(event.object_id),
        "transition": event.transition,
        "args_digest": args_digest(event.args),
        "project": event.project,
    }


async def commit_published(
    conn: AsyncConnection,
    credential: SecretStr,
    operation_id: UUID,
    artifact: Artifact,
    audit_event: AuditEvent,
) -> CaptureOperation:
    """Atomically claim one truthful artifact row, audit it, and close publication."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, artifact.owner_id):
        return await _operation(
            conn,
            "SELECT * FROM public.commit_capture_published("
            "sha256(convert_to(%s, 'UTF8')), %s, %s, %s)",
            (
                credential.get_secret_value(),
                operation_id,
                Jsonb(_artifact_facts(artifact)),
                Jsonb(_audit_facts(audit_event)),
            ),
            refused="capture publication transition was refused",
            error=ValueError,
        )


async def record_cleanup_capture_version(
    conn: AsyncConnection,
    credential: SecretStr,
    operation_id: UUID,
    version_id: str,
) -> CaptureOperation:
    """Journal the exact verified capture version before cancellation deletes it."""
    return await _operation(
        conn,
        "SELECT * FROM public.record_capture_cleanup_version("
        "sha256(convert_to(%s, 'UTF8')), %s, %s)",
        (credential.get_secret_value(), operation_id, version_id),
        refused="capture publication transition was refused",
        error=ValueError,
    )


async def commit_discarded(
    conn: AsyncConnection,
    credential: SecretStr,
    operation_id: UUID,
    tombstone_version: str,
) -> CaptureOperation:
    """Close cancellation with the immutable retained tombstone identity."""
    return await _operation(
        conn,
        "SELECT * FROM public.commit_capture_discarded(sha256(convert_to(%s, 'UTF8')), %s, %s)",
        (credential.get_secret_value(), operation_id, tombstone_version),
        refused="capture publication transition was refused",
        error=ValueError,
    )


async def record_spool_disposed(
    conn: AsyncConnection,
    credential: SecretStr,
    operation_id: UUID,
) -> CaptureOperation:
    """Record verified absence of the exact operation's private packet spool."""
    return await _operation(
        conn,
        "SELECT * FROM public.record_capture_spool_disposed(sha256(convert_to(%s, 'UTF8')), %s)",
        (credential.get_secret_value(), operation_id),
        refused="capture publication transition was refused",
        error=ValueError,
    )
