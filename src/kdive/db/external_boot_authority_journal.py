"""Trusted external-boot authority journal-head repository (ADR-0584)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.providers.external_boot_authority.protocol import (
    JournalPhase,
    JournalRecordV1,
    canonical_record_bytes,
)

type AdvanceStatus = Literal["advanced", "superseded", "conflict"]


def _uuid(value: object) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _bounded(value: object, maximum: int = 255) -> str:
    text = str(value)
    if not text or len(text.encode("utf-8")) > maximum:
        raise ValueError("journal-head text value is outside its byte bound")
    return text


def _positive(value: object) -> int:
    integer = int(str(value))
    if not 1 <= integer <= 9_223_372_036_854_775_807:
        raise ValueError("journal-head integer is outside signed positive bigint")
    return integer


@dataclass(frozen=True, slots=True)
class AuthorityBinding:
    """Opaque immutable authority facts resolved from migration 0122."""

    peer_incarnation_id: str
    authority_id: UUID
    generation: int
    system_id: UUID
    activation_id: UUID
    run_id: UUID
    plan_identity: str
    purpose: str
    provider_kind: str
    authority_instance: str
    operation_identity: str
    operation_digest: str
    state: Literal["allocating", "current"]


@dataclass(frozen=True, slots=True)
class PendingTakeover:
    authority_id: UUID
    generation: int
    operation_identity: str
    attempt_id: UUID
    request_digest: str
    watermark_sequence: int
    watermark_digest: str


@dataclass(frozen=True, slots=True)
class SuspendedOperation:
    authority_id: UUID
    generation: int
    activation_id: UUID
    operation_identity: str
    attempt_id: UUID
    purpose: str
    request_digest: str
    phase: Literal["admitted", "mutation-started", "provider-returned", "observed"]
    source_identity: str
    target_identity: str
    ownership_digest: str


@dataclass(frozen=True, slots=True)
class JournalHead:
    authority_instance: str
    system_id: UUID
    sequence: int
    digest: str
    phase: JournalPhase
    authority_id: UUID
    generation: int
    operation_identity: str
    pending_takeover: PendingTakeover | None
    suspended_operation: SuspendedOperation | None


def _binding(row: dict[str, Any] | None) -> AuthorityBinding | None:
    if row is None:
        return None
    return AuthorityBinding(
        peer_incarnation_id=_bounded(row["peer_incarnation_id"], 512),
        authority_id=_uuid(row["authority_id"]),
        generation=_positive(row["generation"]),
        system_id=_uuid(row["system_id"]),
        activation_id=_uuid(row["activation_id"]),
        run_id=_uuid(row["run_id"]),
        plan_identity=_bounded(row["plan_identity"]),
        purpose=_bounded(row["purpose"]),
        provider_kind=_bounded(row["provider_kind"]),
        authority_instance=_bounded(row["authority_instance"]),
        operation_identity=_bounded(row["operation_identity"]),
        operation_digest=_bounded(row["operation_digest"]),
        state=row["state"],
    )


async def resolve_allocating_authority_binding(
    conn: AsyncConnection,
    *,
    peer_incarnation_id: str,
    authority_id: UUID,
    generation: int,
) -> AuthorityBinding | None:
    """Resolve only the newest allocating authority for an active peer."""
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT * FROM resolve_allocating_external_boot_authority(%s, %s, %s)",
            (peer_incarnation_id, authority_id, generation),
        )
        return _binding(await cursor.fetchone())


async def resolve_current_authority_binding(
    conn: AsyncConnection,
    *,
    peer_incarnation_id: str,
    authority_id: UUID,
    generation: int,
    acknowledgement_sequence: int,
    acknowledgement_digest: str,
) -> AuthorityBinding | None:
    """Resolve a current authority carrying the exact anchored acknowledgement."""
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT * FROM resolve_current_external_boot_authority(%s, %s, %s, %s, %s)",
            (
                peer_incarnation_id,
                authority_id,
                generation,
                acknowledgement_sequence,
                acknowledgement_digest,
            ),
        )
        return _binding(await cursor.fetchone())


def _pending(value: dict[str, Any] | None) -> PendingTakeover | None:
    if value is None:
        return None
    return PendingTakeover(
        authority_id=_uuid(value["authority_id"]),
        generation=_positive(value["generation"]),
        operation_identity=_bounded(value["operation_identity"]),
        attempt_id=_uuid(value["attempt_id"]),
        request_digest=_bounded(value["request_digest"]),
        watermark_sequence=_positive(value["watermark_sequence"]),
        watermark_digest=_bounded(value["watermark_digest"]),
    )


def _suspended(value: dict[str, Any] | None) -> SuspendedOperation | None:
    if value is None:
        return None
    return SuspendedOperation(
        authority_id=_uuid(value["authority_id"]),
        generation=_positive(value["generation"]),
        activation_id=_uuid(value["activation_id"]),
        operation_identity=_bounded(value["operation_identity"]),
        attempt_id=_uuid(value["attempt_id"]),
        purpose=_bounded(value["purpose"]),
        request_digest=_bounded(value["request_digest"]),
        phase=value["phase"],
        source_identity=_bounded(value["source_identity"], 1024),
        target_identity=_bounded(value["target_identity"], 1024),
        ownership_digest=_bounded(value["ownership_digest"]),
    )


async def read_journal_head(
    conn: AsyncConnection, *, binding: AuthorityBinding
) -> JournalHead | None:
    """Read a lane head only through its complete binding."""
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT * FROM read_external_boot_authority_journal_head(%s, %s, %s, %s)",
            (
                binding.peer_incarnation_id,
                binding.authority_id,
                binding.generation,
                binding.authority_instance,
            ),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    row["authority_instance"] = _bounded(row["authority_instance"])
    row["system_id"] = _uuid(row["system_id"])
    row["sequence"] = _positive(row["sequence"])
    row["digest"] = _bounded(row["digest"])
    row["phase"] = JournalPhase(row["phase"])
    row["authority_id"] = _uuid(row["authority_id"])
    row["generation"] = _positive(row["generation"])
    row["operation_identity"] = _bounded(row["operation_identity"])
    row["pending_takeover"] = _pending(row["pending_takeover"])
    row["suspended_operation"] = _suspended(row["suspended_operation"])
    return JournalHead(**row)


async def advance_journal_head(
    conn: AsyncConnection,
    *,
    binding: AuthorityBinding,
    expected_sequence: int,
    expected_digest: str,
    record: JournalRecordV1,
) -> AdvanceStatus:
    """Advance one exact trusted head without translating database failures."""
    async with conn.cursor() as cursor:
        await cursor.execute(
            "SELECT advance_external_boot_authority_journal_head(%s, %s, %s, %s, %s, %s)",
            (
                binding.peer_incarnation_id,
                binding.authority_id,
                binding.generation,
                expected_sequence,
                expected_digest,
                Jsonb(
                    record.model_dump(mode="json", by_alias=True)
                    | {"canonical_record": canonical_record_bytes(record).decode("utf-8")}
                ),
            ),
        )
        row = await cursor.fetchone()
    if row is None or row[0] not in {"advanced", "superseded", "conflict"}:
        raise RuntimeError("journal-head advance returned an invalid status")
    return row[0]
