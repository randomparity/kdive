"""Serialized provider-host external-boot authority lanes (ADR-0584)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from kdive.db.external_boot_authority_journal import AuthorityBinding, JournalHead
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    GENESIS_DIGEST,
    AuthorityAcknowledgementV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
    JournalPhase,
    JournalRecordV1,
    record_digest,
)


@dataclass(frozen=True, slots=True)
class AuthenticatedPeer:
    """Identity established by the hosting authentication boundary."""

    incarnation_id: UUID


class AuthorityMutationAdapter(Protocol):
    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1: ...

    async def commit(
        self, request: AuthorityMutationRequestV1, commit_point: str
    ) -> AuthorityObservationV1: ...


class AuthorityRepository(Protocol):
    async def resolve_allocating(
        self, peer: AuthenticatedPeer, request: AuthorityTakeoverRequestV1
    ) -> AuthorityBinding | None: ...

    async def resolve_current(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> AuthorityBinding | None: ...

    async def read_head(self, binding: AuthorityBinding) -> JournalHead | None: ...

    async def advance(
        self,
        binding: AuthorityBinding,
        expected_sequence: int,
        expected_digest: str,
        record: JournalRecordV1,
    ) -> Literal["advanced", "superseded", "conflict"]: ...


class AuthorityServiceError(RuntimeError):
    """Bounded failure safe to expose across the authority boundary."""

    def __init__(
        self,
        category: Literal["unauthenticated", "superseded", "journal_conflict", "provider_conflict"],
    ):
        self.category = category
        super().__init__(category)


@dataclass(slots=True)
class AuthorityServiceMetrics:
    """Bounded in-process observations; composition may export these values."""

    rejections: dict[tuple[str, str, str], int]
    recovery_failures: dict[tuple[str, str], int]
    unresolved: dict[tuple[str, str], int]
    checkpoints: dict[tuple[str, str], int]

    @classmethod
    def empty(cls) -> AuthorityServiceMetrics:
        return cls({}, {}, {}, {})

    @staticmethod
    def _labels(
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1 | JournalRecordV1,
    ) -> tuple[str, str]:
        return request.provider_kind, request.authority_instance

    def reject(self, request: AuthorityTakeoverRequestV1, category: str) -> None:
        key = (*self._labels(request), category)
        self.rejections[key] = self.rejections.get(key, 0) + 1

    def recovery_failed(self, request: AuthorityTakeoverRequestV1) -> None:
        key = self._labels(request)
        self.recovery_failures[key] = self.recovery_failures.get(key, 0) + 1

    def record_checkpoint(self, request: JournalRecordV1) -> None:
        key = self._labels(request)
        self.checkpoints[key] = self.checkpoints.get(key, 0) + 1


@dataclass(slots=True)
class _Lane:
    lock: asyncio.Lock
    failed: bool = False


class ExternalBootAuthorityService:
    """Serialize, journal, and independently anchor one mutation lane per System."""

    def __init__(
        self,
        *,
        repository: AuthorityRepository,
        journal_factory: Callable[[UUID], FileAuthorityJournal],
        adapter: AuthorityMutationAdapter,
        metrics: AuthorityServiceMetrics | None = None,
    ) -> None:
        self._repository = repository
        self._journal_factory = journal_factory
        self._adapter = adapter
        self.metrics = metrics or AuthorityServiceMetrics.empty()
        self._lanes: dict[UUID, _Lane] = {}
        self._logger = logging.getLogger(__name__)

    def _lane(self, system_id: UUID) -> _Lane:
        return self._lanes.setdefault(system_id, _Lane(asyncio.Lock()))

    @staticmethod
    def _require_peer(peer: AuthenticatedPeer | None) -> AuthenticatedPeer:
        if peer is None or not isinstance(peer.incarnation_id, UUID):
            raise AuthorityServiceError("unauthenticated")
        return peer

    @staticmethod
    def _binding_matches(
        binding: AuthorityBinding,
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1,
    ) -> bool:
        return (
            binding.authority_id == request.authority_id
            and binding.generation == request.generation
            and binding.system_id == request.system_id
            and binding.activation_id == request.activation_id
            and binding.run_id == request.run_id
            and binding.plan_identity == request.plan_identity
            and binding.purpose == request.purpose
            and binding.provider_kind == request.provider_kind
            and binding.authority_instance == request.authority_instance
            and binding.operation_identity == request.operation_identity
            and binding.operation_digest == request.operation_digest
        )

    async def _recover(
        self, binding: AuthorityBinding, journal: FileAuthorityJournal
    ) -> tuple[JournalRecordV1, ...]:
        records = journal.load()
        head = await self._repository.read_head(binding)
        if head is None:
            if records:
                raise AuthorityServiceError("journal_conflict")
            return records
        if not records:
            raise AuthorityServiceError("journal_conflict")
        last = records[-1]
        if (
            last.sequence != head.sequence
            or record_digest(last) != head.digest
            or last.phase is not head.phase
            or last.authority_id != head.authority_id
            or last.generation != head.generation
            or last.operation_identity != head.operation_identity
        ):
            raise AuthorityServiceError("journal_conflict")
        return records

    async def _anchor(
        self,
        binding: AuthorityBinding,
        journal: FileAuthorityJournal,
        records: tuple[JournalRecordV1, ...],
        record: JournalRecordV1,
    ) -> tuple[JournalRecordV1, ...]:
        journal.append(record)
        status = await self._repository.advance(
            binding,
            records[-1].sequence if records else 0,
            record_digest(records[-1]) if records else GENESIS_DIGEST,
            record,
        )
        if status != "advanced":
            raise AuthorityServiceError(
                "superseded" if status == "superseded" else "journal_conflict"
            )
        self.metrics.record_checkpoint(record)
        return (*records, record)

    async def readiness(
        self, peer: AuthenticatedPeer | None, request: AuthorityTakeoverRequestV1
    ) -> bool:
        """Return true only when local bytes exactly equal the scoped trusted head."""
        try:
            authenticated = self._require_peer(peer)
            binding = await self._repository.resolve_allocating(authenticated, request)
            if binding is None or not self._binding_matches(binding, request):
                return False
            await self._recover(binding, self._journal_factory(request.system_id))
        except AuthorityServiceError, OSError, ValueError:
            self.metrics.recovery_failed(request)
            self._logger.warning(
                "authority recovery rejected",
                extra={
                    "provider_kind": request.provider_kind,
                    "authority_instance": request.authority_instance,
                    "category": "journal_conflict",
                },
            )
            return False
        return True

    @staticmethod
    def _record(
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1,
        records: tuple[JournalRecordV1, ...],
        phase: JournalPhase,
        **changes: object,
    ) -> JournalRecordV1:
        values = request.model_dump(mode="json", by_alias=True) | {
            "sequence": len(records) + 1,
            "previous_digest": record_digest(records[-1]) if records else GENESIS_DIGEST,
            "phase": phase,
            "attempt_id": getattr(request, "attempt_id", request.authority_id),
        }
        if isinstance(request, AuthorityMutationRequestV1):
            values |= {
                "expected_source_identity": request.expected_source_identity,
                "intended_target_identity": request.intended_target_identity,
                "recovery_objects": request.recovery_objects,
            }
            values.pop("operation", None)
        values.update(changes)
        return JournalRecordV1.model_validate(values)

    async def acknowledge_takeover(
        self, peer: AuthenticatedPeer | None, request: AuthorityTakeoverRequestV1
    ) -> AuthorityAcknowledgementV1:
        authenticated = self._require_peer(peer)
        lane = self._lane(request.system_id)
        async with lane.lock:
            if lane.failed:
                raise AuthorityServiceError("journal_conflict")
            binding = await self._repository.resolve_allocating(authenticated, request)
            if binding is None or not self._binding_matches(binding, request):
                raise AuthorityServiceError("superseded")
            journal = self._journal_factory(request.system_id)
            try:
                records = await self._recover(binding, journal)
                watermark = self._record(request, records, JournalPhase.WATERMARK_INSTALLED)
                records = await self._anchor(binding, journal, records, watermark)
                if any(
                    record.phase in {JournalPhase.ADMITTED, JournalPhase.MUTATION_STARTED}
                    for record in records[:-1]
                ):
                    raise AuthorityServiceError("provider_conflict")
                acknowledgement = self._record(
                    request,
                    records,
                    JournalPhase.TAKEOVER_ACKNOWLEDGED,
                    watermark_sequence=watermark.sequence,
                    watermark_digest=record_digest(watermark),
                )
                records = await self._anchor(binding, journal, records, acknowledgement)
            except BaseException:
                lane.failed = True
                raise
            quiescence = json.dumps(
                {
                    "authority_instance": request.authority_instance,
                    "generation": request.generation,
                    "lower_operations": [],
                    "system_id": str(request.system_id),
                    "watermark_digest": record_digest(watermark),
                    "watermark_sequence": watermark.sequence,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            return AuthorityAcknowledgementV1(
                authority_id=request.authority_id,
                generation=request.generation,
                system_id=request.system_id,
                journal_sequence=acknowledgement.sequence,
                journal_digest=record_digest(acknowledgement),
                positive_quiescence_digest="sha256:" + hashlib.sha256(quiescence).hexdigest(),
            )

    async def execute_mutation(
        self, peer: AuthenticatedPeer | None, request: AuthorityMutationRequestV1
    ) -> AuthorityObservationV1:
        authenticated = self._require_peer(peer)
        lane = self._lane(request.system_id)

        async def run() -> AuthorityObservationV1:
            async with lane.lock:
                if lane.failed:
                    raise AuthorityServiceError("journal_conflict")
                journal = self._journal_factory(request.system_id)
                records = journal.load()
                acknowledgements = [
                    record
                    for record in records
                    if record.phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
                    and record.generation == request.generation
                ]
                if not acknowledgements:
                    raise AuthorityServiceError("superseded")
                acknowledgement = acknowledgements[-1]
                binding = await self._repository.resolve_current(
                    authenticated,
                    request,
                    acknowledgement.sequence,
                    record_digest(acknowledgement),
                )
                if binding is None or not self._binding_matches(binding, request):
                    raise AuthorityServiceError("superseded")
                records = await self._recover(binding, journal)
                records = await self._anchor(
                    binding, journal, records, self._record(request, records, JournalPhase.ADMITTED)
                )
                records = await self._anchor(
                    binding,
                    journal,
                    records,
                    self._record(request, records, JournalPhase.MUTATION_STARTED),
                )
                rechecked = await self._repository.resolve_current(
                    authenticated,
                    request,
                    acknowledgement.sequence,
                    record_digest(acknowledgement),
                )
                if rechecked is None or not self._binding_matches(rechecked, request):
                    raise AuthorityServiceError("superseded")
                await self._adapter.commit(request, request.operation)
                records = await self._anchor(
                    binding,
                    journal,
                    records,
                    self._record(request, records, JournalPhase.PROVIDER_RETURNED),
                )
                observation = await self._adapter.observe(request)
                records = await self._anchor(
                    binding,
                    journal,
                    records,
                    self._record(request, records, JournalPhase.OBSERVED, observation=observation),
                )
                outcome = (
                    observation.category
                    if observation.category in {"source", "target", "conflict"}
                    else "conflict"
                )
                await self._anchor(
                    binding,
                    journal,
                    records,
                    self._record(
                        request,
                        records,
                        JournalPhase.TERMINAL,
                        observation=observation,
                        outcome=outcome,
                    ),
                )
                return observation

        task = asyncio.create_task(run())
        return await asyncio.shield(task)
