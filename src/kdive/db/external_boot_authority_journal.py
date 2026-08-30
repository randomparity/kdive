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
    record_digest,
)

type AdvanceStatus = Literal["advanced", "superseded", "conflict"]


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
    phase: Literal["admitted", "mutation-started"]
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
    return None if row is None else AuthorityBinding(**row)


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
    return None if value is None else PendingTakeover(**value)


def _suspended(value: dict[str, Any] | None) -> SuspendedOperation | None:
    return None if value is None else SuspendedOperation(**value)


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
    row["phase"] = JournalPhase(row["phase"])
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
                    | {"record_digest": record_digest(record)}
                ),
            ),
        )
        row = await cursor.fetchone()
    if row is None or row[0] not in {"advanced", "superseded", "conflict"}:
        raise RuntimeError("journal-head advance returned an invalid status")
    return row[0]
