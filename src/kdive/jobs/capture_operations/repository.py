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

type CaptureOperationState = Literal["launching", "gated", "running", "cancel_requested", "exited"]
type CaptureProviderKind = Literal["local-libvirt", "remote-libvirt"]


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
    created_at: datetime
    identity_recorded_at: datetime | None
    running_at: datetime | None
    cancel_requested_at: datetime | None
    exited_at: datetime | None
    updated_at: datetime


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
