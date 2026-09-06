"""Least-privilege database adapter for authority-owned Systems (ADR-0623)."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports.external_boot import RootSpecV1
from kdive.providers.system_authority.protocol import (
    AuthoritySystemJournalPhase,
    AuthoritySystemJournalRecordV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemProofV1,
    AuthoritySystemProvisionSnapshot,
    AuthoritySystemTakeoverRequestV1,
    authority_system_record_digest,
    canonical_system_authority_bytes,
)


class AuthoritySystemConnectionFactory(Protocol):
    """Open one already validated provider-authority database connection."""

    def __call__(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


@dataclass(frozen=True, slots=True)
class AuthoritySystemBinding:
    """Exact DB-authenticated attempt and immutable System ownership binding."""

    authority_id: UUID
    generation: int
    system_id: UUID
    allocation_id: UUID
    resource_id: UUID
    provider_kind: Literal["local-libvirt", "remote-libvirt"]
    resource_name: str
    authority_instance: str
    profile_identity: str
    root_identity: str
    bootstrap_identity: str
    operation: Literal["provision", "preactivation-teardown"]
    operation_identity: str
    operation_digest: str
    state: Literal["allocating", "current", "terminal"]


@dataclass(frozen=True, slots=True)
class AuthoritySystemJournalHead:
    """One global per-System DB journal head, including genesis."""

    system_id: UUID
    sequence: int
    digest: str
    phase: AuthoritySystemJournalPhase | None
    record: AuthoritySystemJournalRecordV1 | None


@dataclass(frozen=True, slots=True)
class ResolvedAuthoritySystemOperation:
    """A current mutation binding plus its validated DB-derived provision inputs."""

    binding: AuthoritySystemBinding
    head: AuthoritySystemJournalHead
    snapshot: AuthoritySystemProvisionSnapshot
    receipt_bytes: bytes | None


@dataclass(frozen=True, slots=True)
class AuthoritySystemAdvanceResult:
    status: Literal["advanced", "superseded", "conflict"]
    sequence: int | None
    digest: str | None


def _uuid(value: object) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _bounded(value: object, *, maximum: int = 255) -> str:
    text = str(value)
    if not text or not text.strip() or len(text.encode("utf-8")) > maximum:
        raise ValueError("authority System database text is outside its bound")
    return text


def _positive(value: object) -> int:
    number = int(str(value))
    if not 1 <= number <= 9_223_372_036_854_775_807:
        raise ValueError("authority System database integer is outside positive bigint")
    return number


def _binding(row: dict[str, Any]) -> AuthoritySystemBinding:
    bootstrap = _bounded(row["bootstrap_identity"])
    state = _bounded(row["state"])
    provider = _bounded(row["provider_kind"])
    operation = _bounded(row["operation"])
    if provider not in {"local-libvirt", "remote-libvirt"}:
        raise ValueError("authority System provider kind is invalid")
    if operation not in {"provision", "preactivation-teardown"}:
        raise ValueError("authority System operation is invalid")
    if state not in {"allocating", "current", "terminal"}:
        raise ValueError("authority System attempt state is invalid")
    return AuthoritySystemBinding(
        authority_id=_uuid(row["authority_id"]),
        generation=_positive(row["generation"]),
        system_id=_uuid(row["system_id"]),
        allocation_id=_uuid(row["allocation_id"]),
        resource_id=_uuid(row["resource_id"]),
        provider_kind=provider,  # type: ignore[arg-type]
        resource_name=_bounded(row["resource_name"]),
        authority_instance=_bounded(row["authority_instance"]),
        profile_identity=_bounded(row["profile_identity"]),
        root_identity=_bounded(row["root_identity"]),
        bootstrap_identity=bootstrap,
        operation=operation,  # type: ignore[arg-type]
        operation_identity=_bounded(row["operation_identity"]),
        operation_digest=_bounded(row["operation_digest"]),
        state=state,  # type: ignore[arg-type]
    )


def _head(row: dict[str, Any]) -> AuthoritySystemJournalHead:
    sequence = int(str(row["journal_sequence"]))
    if not 0 <= sequence <= 9_223_372_036_854_775_807:
        raise ValueError("authority System journal sequence is outside bigint")
    raw_phase = row["journal_phase"]
    raw_record = row["journal_record"]
    phase = AuthoritySystemJournalPhase(str(raw_phase)) if raw_phase is not None else None
    record = (
        AuthoritySystemJournalRecordV1.model_validate(raw_record)
        if raw_record is not None
        else None
    )
    if (sequence == 0) != (phase is None and record is None):
        raise ValueError("authority System journal genesis shape is invalid")
    system_id = _uuid(row["system_id"])
    digest = _bounded(row["journal_digest"])
    if record is not None and (
        record.system_id != system_id
        or record.sequence != sequence
        or record.phase is not phase
        or authority_system_record_digest(record) != digest
    ):
        raise ValueError("authority System journal head does not match its record")
    return AuthoritySystemJournalHead(
        system_id=system_id,
        sequence=sequence,
        digest=digest,
        phase=phase,
        record=record,
    )


def _request_matches(
    binding: AuthoritySystemBinding,
    request: AuthoritySystemTakeoverRequestV1 | AuthoritySystemMutationRequestV1,
) -> bool:
    return (
        binding.authority_id == request.authority_id
        and binding.generation == request.generation
        and binding.system_id == request.system_id
        and binding.allocation_id == request.allocation_id
        and binding.resource_id == request.resource_id
        and binding.provider_kind == request.provider_kind
        and binding.resource_name == request.resource_name
        and binding.authority_instance == request.authority_instance
        and binding.profile_identity == request.profile_identity
        and binding.root_identity == request.root_identity
        and binding.bootstrap_identity == request.bootstrap_identity
        and binding.operation == request.operation.value
        and binding.operation_identity == request.operation_identity
        and binding.operation_digest == request.operation_digest
    )


def _snapshot(
    row: dict[str, Any], binding: AuthoritySystemBinding
) -> AuthoritySystemProvisionSnapshot:
    profile = ProvisioningProfile.parse(row["provisioning_profile"])
    root = RootSpecV1.model_validate(row["root_spec"])
    if root.architecture != str(row["root_architecture"]):
        raise ValueError("root provenance architecture does not match RootSpec")
    return AuthoritySystemProvisionSnapshot(
        system_id=binding.system_id,
        allocation_id=binding.allocation_id,
        resource_id=binding.resource_id,
        project=_bounded(row["project"]),
        provider_kind=binding.provider_kind,
        resource_name=binding.resource_name,
        authority_instance=binding.authority_instance,
        profile=profile,
        profile_identity=binding.profile_identity,
        source_image_id=_uuid(row["source_image_id"]),
        root_identity=binding.root_identity,
        root_spec=root,
        bootstrap_public_key=_bounded(row["bootstrap_public_key"], maximum=8192),
        bootstrap_identity=binding.bootstrap_identity,
    )


class DatabaseAuthoritySystemRepository:
    """Resolve and advance only SQL-authenticated System authority state."""

    def __init__(self, connections: AuthoritySystemConnectionFactory) -> None:
        self._connections = connections

    async def resolve_allocating(
        self, peer_incarnation: str, request: AuthoritySystemTakeoverRequestV1
    ) -> ResolvedAuthoritySystemOperation | None:
        request = AuthoritySystemTakeoverRequestV1.model_validate(
            request.model_dump(mode="python", by_alias=True)
        )
        async with (
            self._connections() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                "SELECT * FROM resolve_allocating_authority_system_attempt(%s,%s,%s)",
                (peer_incarnation, request.authority_id, request.generation),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        binding = _binding(row)
        if binding.state != "allocating" or not _request_matches(binding, request):
            return None
        return ResolvedAuthoritySystemOperation(
            binding, _head(row), _snapshot(row, binding), row["receipt_bytes"]
        )

    async def resolve_current(
        self,
        peer_incarnation: str,
        request: AuthoritySystemMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> ResolvedAuthoritySystemOperation | None:
        request = AuthoritySystemMutationRequestV1.model_validate(
            request.model_dump(mode="python", by_alias=True)
        )
        async with (
            self._connections() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                "SELECT * FROM resolve_current_authority_system_attempt(%s,%s,%s,%s,%s)",
                (
                    peer_incarnation,
                    request.authority_id,
                    request.generation,
                    acknowledgement_sequence,
                    acknowledgement_digest,
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        binding = _binding(row)
        if not _request_matches(binding, request):
            return None
        return ResolvedAuthoritySystemOperation(
            binding, _head(row), _snapshot(row, binding), row["receipt_bytes"]
        )

    async def advance_head(
        self,
        peer_incarnation: str,
        request: AuthoritySystemTakeoverRequestV1 | AuthoritySystemMutationRequestV1,
        *,
        expected_sequence: int,
        expected_digest: str,
        record: AuthoritySystemJournalRecordV1,
        receipt: AuthoritySystemProofV1 | None = None,
    ) -> AuthoritySystemAdvanceResult:
        if isinstance(request, AuthoritySystemTakeoverRequestV1):
            request = AuthoritySystemTakeoverRequestV1.model_validate(
                request.model_dump(mode="python", by_alias=True)
            )
        else:
            request = AuthoritySystemMutationRequestV1.model_validate(
                request.model_dump(mode="python", by_alias=True)
            )
        record = AuthoritySystemJournalRecordV1.model_validate(
            record.model_dump(mode="python", by_alias=True)
        )
        if (
            record.authority_id != request.authority_id
            or record.generation != request.generation
            or record.system_id != request.system_id
            or record.operation_digest != request.operation_digest
        ):
            raise ValueError("authority System journal record does not match request")
        receipt_bytes = canonical_system_authority_bytes(receipt) if receipt is not None else None
        async with self._connections() as conn, conn.transaction():
            result = await conn.execute(
                "SELECT * FROM advance_authority_system_journal_head(%s,%s,%s,%s,%s,%s,%s)",
                (
                    peer_incarnation,
                    request.authority_id,
                    request.generation,
                    expected_sequence,
                    expected_digest,
                    Jsonb(record.model_dump(mode="json", by_alias=True)),
                    receipt_bytes,
                ),
            )
            row = await result.fetchone()
        if row is None or row[0] not in {"advanced", "superseded", "conflict"}:
            raise ValueError("authority System journal advance returned an invalid status")
        return AuthoritySystemAdvanceResult(row[0], row[1], row[2])
