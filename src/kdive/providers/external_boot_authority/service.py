"""Serialized provider-host external-boot authority lanes (ADR-0584)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast
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

    incarnation_id: UUID | str


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

    async def resolve_current_candidate(
        self, peer: AuthenticatedPeer, request: AuthorityMutationRequestV1
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
        *,
        telemetry_recorded: bool = False,
    ):
        self.category = category
        self.telemetry_recorded = telemetry_recorded
        super().__init__(category)


@dataclass(slots=True)
class AuthorityServiceMetrics:
    """Bounded in-process observations; composition may export these values."""

    rejections: dict[tuple[str, str, str], int]
    recovery_failures: dict[tuple[str, str], int]
    unresolved: dict[tuple[str, str], int]
    checkpoints: dict[tuple[str, str], int]
    checkpoint_latency: dict[tuple[str, str], tuple[int, float]]
    max_coordinates: int = 256
    registered_coordinates: frozenset[tuple[str, str]] = frozenset()
    _coordinates: set[tuple[str, str]] = field(default_factory=set)

    _OVERFLOW = ("overflow", "overflow")
    _UNTRUSTED = ("untrusted", "unresolved")

    @classmethod
    def empty(
        cls,
        *,
        max_coordinates: int = 256,
        registered_coordinates: frozenset[tuple[str, str]] = frozenset(),
    ) -> AuthorityServiceMetrics:
        if max_coordinates < 1:
            raise ValueError("authority metrics coordinate maximum must be positive")
        if len(registered_coordinates) > max_coordinates:
            raise ValueError("registered authority metrics coordinates exceed maximum")
        return cls(
            {},
            {},
            {},
            {},
            {},
            max_coordinates,
            registered_coordinates,
            set(registered_coordinates),
        )

    def _labels(
        self,
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1 | JournalRecordV1,
    ) -> tuple[str, str]:
        return self.labels((request.provider_kind, request.authority_instance))

    def labels(self, labels: tuple[str, str]) -> tuple[str, str]:
        if labels in {self._UNTRUSTED, self._OVERFLOW}:
            return labels
        if labels in self._coordinates:
            return labels
        if len(self._coordinates) >= self.max_coordinates:
            return self._OVERFLOW
        self._coordinates.add(labels)
        return labels

    def _key(self, key: tuple[str, ...]) -> tuple[str, ...]:
        overflow = (*self._OVERFLOW, *("overflow" for _ in key[2:]))
        if key[:2] == self._OVERFLOW:
            return overflow
        return key

    def reject_labels(self, labels: tuple[str, str], category: str) -> tuple[str, str]:
        bounded = self.labels(labels)
        key = cast(tuple[str, str, str], self._key((*bounded, category)))
        self.rejections[key] = self.rejections.get(key, 0) + 1
        return key[:2]

    def reject(
        self,
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1 | JournalRecordV1,
        category: str,
    ) -> None:
        self.reject_labels((request.provider_kind, request.authority_instance), category)

    def recovery_failed_labels(self, labels: tuple[str, str]) -> tuple[str, str]:
        key = cast(tuple[str, str], self._key(self.labels(labels)))
        self.recovery_failures[key] = self.recovery_failures.get(key, 0) + 1
        return key

    def record_checkpoint(self, request: JournalRecordV1, elapsed: float) -> None:
        key = cast(tuple[str, str], self._key(self._labels(request)))
        self.checkpoints[key] = self.checkpoints.get(key, 0) + 1
        key = cast(tuple[str, str], self._key(self._labels(request)))
        count, total = self.checkpoint_latency.get(key, (0, 0.0))
        self.checkpoint_latency[key] = count + 1, total + elapsed

    def set_unresolved(self, labels: tuple[str, str], unresolved: bool) -> None:
        key = self.labels(labels)
        if unresolved:
            self.unresolved[key] = self.unresolved.get(key, 0) + 1 if key == self._OVERFLOW else 1
        elif key in self.unresolved:
            remaining = self.unresolved[key] - 1 if key == self._OVERFLOW else 0
            if remaining:
                self.unresolved[key] = remaining
            else:
                self.unresolved.pop(key)


@dataclass(slots=True)
class _Lane:
    lock: asyncio.Lock
    users: int = 0
    failed: bool = False
    watermark_generation: int = 0
    active: _ActiveOperation | None = None
    journal: FileAuthorityJournal | None = None
    records: list[JournalRecordV1] | None = None


@dataclass(slots=True)
class _ActiveOperation:
    generation: int
    phase: JournalPhase
    done: asyncio.Event
    stop_before_start: bool = False
    completion_binding: AuthorityBinding | None = None


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
        lane = self._lanes.setdefault(system_id, _Lane(asyncio.Lock()))
        lane.users += 1
        return lane

    def _release_lane(self, system_id: UUID, lane: _Lane) -> None:
        lane.users -= 1
        if lane.users == 0 and lane.active is None and self._lanes.get(system_id) is lane:
            self._lanes.pop(system_id)
            if lane.journal is not None:
                lane.journal.close()

    def _lane_journal(
        self, system_id: UUID, lane: _Lane
    ) -> tuple[FileAuthorityJournal, list[JournalRecordV1]]:
        if lane.journal is None:
            lane.journal = self._journal_factory(system_id)
            lane.records = list(lane.journal.load())
        assert lane.records is not None
        return lane.journal, lane.records

    @staticmethod
    def _trusted_labels(binding: AuthorityBinding | None) -> tuple[str, str]:
        if binding is None:
            return "untrusted", "unresolved"
        return binding.provider_kind, binding.authority_instance

    def _reject(
        self,
        category: Literal["unauthenticated", "superseded", "journal_conflict"],
        *,
        labels: tuple[str, str] = ("untrusted", "unresolved"),
    ) -> AuthorityServiceError:
        labels = self.metrics.reject_labels(labels, category)
        self._logger.warning(
            "authority request rejected",
            extra={
                "provider_kind": labels[0],
                "authority_instance": labels[1],
                "category": category,
            },
        )
        return AuthorityServiceError(category, telemetry_recorded=True)

    def _ensure_rejection(
        self,
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1 | JournalRecordV1,
        error: AuthorityServiceError,
    ) -> None:
        if error.telemetry_recorded:
            return
        labels = self.metrics.reject_labels(
            (request.provider_kind, request.authority_instance), error.category
        )
        self._logger.warning(
            "authority request rejected",
            extra={
                "provider_kind": labels[0],
                "authority_instance": labels[1],
                "category": error.category,
            },
        )
        error.telemetry_recorded = True

    def _require_peer(
        self,
        peer: AuthenticatedPeer | None,
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1,
    ) -> AuthenticatedPeer:
        if peer is None or not isinstance(peer.incarnation_id, UUID | str):
            raise self._reject("unauthenticated")
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
            and binding.operation == request.operation
            and binding.provider_kind == request.provider_kind
            and binding.authority_instance == request.authority_instance
            and binding.operation_identity == request.operation_identity
            and binding.operation_digest == request.operation_digest
        )

    async def _recover(
        self,
        binding: AuthorityBinding,
        journal: FileAuthorityJournal,
        records: list[JournalRecordV1] | None = None,
    ) -> list[JournalRecordV1]:
        if records is None:
            records = list(journal.load())
        head = await self._repository.read_head(binding)
        if head is None:
            if records:
                raise AuthorityServiceError("journal_conflict")
            return records
        if not records:
            raise AuthorityServiceError("journal_conflict")
        last = records[-1]
        inherited_terminal = (
            last.phase is JournalPhase.TERMINAL
            and last.generation < binding.generation
            and head.pending_takeover is not None
            and head.pending_takeover.authority_id == binding.authority_id
            and head.pending_takeover.generation == binding.generation
        )
        if (
            last.sequence != head.sequence
            or record_digest(last) != head.digest
            or last.phase is not head.phase
            or (not inherited_terminal and last.authority_id != head.authority_id)
            or (not inherited_terminal and last.generation != head.generation)
            or last.operation_identity != head.operation_identity
        ):
            raise AuthorityServiceError("journal_conflict")
        return records

    async def _anchor(
        self,
        binding: AuthorityBinding,
        journal: FileAuthorityJournal,
        records: list[JournalRecordV1],
        record: JournalRecordV1,
    ) -> list[JournalRecordV1]:
        started = time.perf_counter()
        journal.append(record)
        status = await self._repository.advance(
            binding,
            records[-1].sequence if records else 0,
            record_digest(records[-1]) if records else GENESIS_DIGEST,
            record,
        )
        if status != "advanced":
            raise self._reject(
                "superseded" if status == "superseded" else "journal_conflict",
                labels=self._trusted_labels(binding),
            )
        self.metrics.record_checkpoint(record, time.perf_counter() - started)
        records.append(record)
        return records

    def _provider_error(self, request: AuthorityMutationRequestV1) -> AuthorityServiceError:
        labels = self.metrics.reject_labels(
            (request.provider_kind, request.authority_instance), "provider_conflict"
        )
        self._logger.warning(
            "authority provider boundary failed",
            extra={
                "provider_kind": labels[0],
                "authority_instance": labels[1],
                "category": "provider_conflict",
            },
        )
        return AuthorityServiceError("provider_conflict", telemetry_recorded=True)

    async def readiness(
        self, peer: AuthenticatedPeer | None, request: AuthorityTakeoverRequestV1
    ) -> bool:
        """Return true only when local bytes exactly equal the scoped trusted head."""
        journal: FileAuthorityJournal | None = None
        trusted_labels = self._trusted_labels(None)
        try:
            authenticated = self._require_peer(peer, request)
            binding = await self._repository.resolve_allocating(authenticated, request)
            if binding is None or not self._binding_matches(binding, request):
                return False
            trusted_labels = self._trusted_labels(binding)
            journal = self._journal_factory(request.system_id)
            records = await self._recover(binding, journal)
        except AuthorityServiceError, OSError, ValueError:
            self.metrics.recovery_failed_labels(trusted_labels)
            self._logger.warning(
                "authority recovery rejected",
                extra={"category": "journal_conflict"},
            )
            return False
        finally:
            if journal is not None:
                journal.close()
        phases_by_operation = {record.operation_identity: record.phase for record in records}
        return not any(
            phase
            in {
                JournalPhase.ADMITTED,
                JournalPhase.MUTATION_STARTED,
                JournalPhase.PROVIDER_RETURNED,
                JournalPhase.OBSERVED,
            }
            for phase in phases_by_operation.values()
        )

    @staticmethod
    def _record(
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1,
        records: list[JournalRecordV1],
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
        values.update(changes)
        return JournalRecordV1.model_validate(values)

    @staticmethod
    def _mutation_from_record(record: JournalRecordV1) -> AuthorityMutationRequestV1:
        return AuthorityMutationRequestV1(
            authority_id=record.authority_id,
            generation=record.generation,
            system_id=record.system_id,
            activation_id=record.activation_id,
            run_id=record.run_id,
            plan_identity=record.plan_identity,
            purpose=record.purpose,
            provider_kind=record.provider_kind,
            authority_instance=record.authority_instance,
            operation_identity=record.operation_identity,
            operation_digest=record.operation_digest,
            operation=record.operation,
            attempt_id=record.attempt_id,
            expected_source_identity=record.expected_source_identity or "",
            intended_target_identity=record.intended_target_identity or "",
            recovery_objects=record.recovery_objects,
        )

    async def _recover_suspended(
        self,
        binding: AuthorityBinding,
        journal: FileAuthorityJournal,
        records: list[JournalRecordV1],
        prior: JournalRecordV1,
        suspended: object,
    ) -> list[JournalRecordV1]:
        from kdive.db.external_boot_authority_journal import SuspendedOperation

        ownership = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    [item.model_dump(mode="json") for item in prior.recovery_objects],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        if not isinstance(suspended, SuspendedOperation) or (
            suspended.authority_id != prior.authority_id
            or suspended.generation != prior.generation
            or suspended.system_id != prior.system_id
            or suspended.activation_id != prior.activation_id
            or suspended.run_id != prior.run_id
            or suspended.plan_identity != prior.plan_identity
            or suspended.operation_identity != prior.operation_identity
            or suspended.attempt_id != prior.attempt_id
            or suspended.purpose != prior.purpose
            or suspended.operation != prior.operation
            or suspended.provider_kind != prior.provider_kind
            or suspended.authority_instance != prior.authority_instance
            or suspended.request_digest != prior.operation_digest
            or suspended.phase != prior.phase.value
            or suspended.source_identity != prior.expected_source_identity
            or suspended.target_identity != prior.intended_target_identity
            or suspended.ownership_digest != ownership
        ):
            raise AuthorityServiceError("journal_conflict")
        return await self._finish_recovery(binding, journal, records, prior)

    async def _finish_recovery(
        self,
        binding: AuthorityBinding,
        journal: FileAuthorityJournal,
        records: list[JournalRecordV1],
        prior: JournalRecordV1,
    ) -> list[JournalRecordV1]:
        request = self._mutation_from_record(prior)
        if prior.phase is JournalPhase.ADMITTED:
            terminal = self._record(request, records, JournalPhase.TERMINAL, outcome="never-began")
            return await self._anchor(binding, journal, records, terminal)
        if prior.phase is JournalPhase.MUTATION_STARTED:
            try:
                observation = await self._adapter.observe(request)
            except Exception:
                raise self._provider_error(request) from None
            records = await self._anchor(
                binding,
                journal,
                records,
                self._record(request, records, JournalPhase.PROVIDER_RETURNED),
            )
        elif prior.phase is JournalPhase.PROVIDER_RETURNED:
            try:
                observation = await self._adapter.observe(request)
            except Exception:
                raise self._provider_error(request) from None
        elif prior.phase is JournalPhase.OBSERVED:
            if prior.observation is None:
                raise AuthorityServiceError("journal_conflict")
            observation = prior.observation
        else:
            raise AuthorityServiceError("journal_conflict")
        if prior.phase is not JournalPhase.OBSERVED:
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
        return await self._anchor(
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

    async def acknowledge_takeover(
        self, peer: AuthenticatedPeer | None, request: AuthorityTakeoverRequestV1
    ) -> AuthorityAcknowledgementV1:
        authenticated = self._require_peer(peer, request)
        binding = await self._repository.resolve_allocating(authenticated, request)
        if binding is None or not self._binding_matches(binding, request):
            raise self._reject("superseded", labels=self._trusted_labels(binding))
        lane = self._lane(binding.system_id)
        try:
            return await self._acknowledge_takeover_bound(authenticated, binding, lane, request)
        finally:
            self._release_lane(binding.system_id, lane)

    async def _acknowledge_takeover_bound(
        self,
        authenticated: AuthenticatedPeer,
        binding: AuthorityBinding,
        lane: _Lane,
        request: AuthorityTakeoverRequestV1,
    ) -> AuthorityAcknowledgementV1:
        async with lane.lock:
            if lane.failed:
                raise self._reject("journal_conflict", labels=self._trusted_labels(binding))
            confirmed = await self._repository.resolve_allocating(authenticated, request)
            if confirmed is None or confirmed != binding:
                raise self._reject("superseded", labels=self._trusted_labels(binding))
            try:
                journal, records = self._lane_journal(request.system_id, lane)
                records = await self._recover(binding, journal, records)
                trusted = await self._repository.read_head(binding)
                watermark: JournalRecordV1 | None = None
                pending = trusted.pending_takeover if trusted is not None else None
                if pending is not None:
                    prior = next(
                        (
                            record
                            for record in records
                            if record.sequence == pending.watermark_sequence
                            and record_digest(record) == pending.watermark_digest
                        ),
                        None,
                    )
                    if prior is None:
                        raise AuthorityServiceError("journal_conflict")
                    if pending.generation == request.generation:
                        if (
                            pending.authority_id != request.authority_id
                            or pending.operation_identity != request.operation_identity
                            or pending.request_digest != request.operation_digest
                        ):
                            raise AuthorityServiceError("superseded")
                        watermark = prior
                    elif pending.generation >= request.generation:
                        raise AuthorityServiceError("superseded")
                    else:
                        superseded = self._record(
                            request,
                            records,
                            JournalPhase.TAKEOVER_SUPERSEDED,
                            predecessor_generation=pending.generation,
                            watermark_sequence=pending.watermark_sequence,
                            watermark_digest=pending.watermark_digest,
                        )
                        records = await self._anchor(binding, journal, records, superseded)
                if watermark is None:
                    watermark = self._record(request, records, JournalPhase.WATERMARK_INSTALLED)
                    records = await self._anchor(binding, journal, records, watermark)
                lane.watermark_generation = request.generation
                active = lane.active
                phases_by_operation = {
                    record.operation_identity: record.phase for record in records[:-1]
                }
                unresolved_restart = any(
                    phase
                    in {
                        JournalPhase.ADMITTED,
                        JournalPhase.MUTATION_STARTED,
                        JournalPhase.PROVIDER_RETURNED,
                        JournalPhase.OBSERVED,
                    }
                    for phase in phases_by_operation.values()
                )
                if unresolved_restart and active is None:
                    self.metrics.set_unresolved(
                        (request.provider_kind, request.authority_instance), True
                    )
                    unresolved = next(
                        record
                        for record in reversed(records[:-1])
                        if phases_by_operation[record.operation_identity] == record.phase
                        and record.phase
                        in {
                            JournalPhase.ADMITTED,
                            JournalPhase.MUTATION_STARTED,
                            JournalPhase.PROVIDER_RETURNED,
                            JournalPhase.OBSERVED,
                        }
                    )
                    trusted_after_watermark = await self._repository.read_head(binding)
                    if trusted_after_watermark is None:
                        raise AuthorityServiceError("journal_conflict")
                    records = await self._recover_suspended(
                        binding,
                        journal,
                        records,
                        unresolved,
                        trusted_after_watermark.suspended_operation,
                    )
                if active is not None and active.generation < request.generation:
                    active.completion_binding = binding
                    if active.phase is JournalPhase.ADMITTED:
                        active.stop_before_start = True
                    self.metrics.set_unresolved(
                        (request.provider_kind, request.authority_instance), True
                    )
            except AuthorityServiceError as error:
                self._ensure_rejection(request, error)
                lane.failed = error.category == "journal_conflict"
                raise
            except BaseException:
                lane.failed = True
                raise
        if active is not None and active.generation < request.generation:
            await active.done.wait()
        await asyncio.sleep(0)
        async with lane.lock:
            try:
                if lane.watermark_generation != request.generation:
                    raise AuthorityServiceError("superseded")
                records = await self._recover(binding, journal, records)
                phases = {record.operation_identity: record.phase for record in records}
                if any(
                    phase
                    in {
                        JournalPhase.ADMITTED,
                        JournalPhase.MUTATION_STARTED,
                        JournalPhase.PROVIDER_RETURNED,
                        JournalPhase.OBSERVED,
                    }
                    for phase in phases.values()
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
            except AuthorityServiceError as error:
                self._ensure_rejection(request, error)
                lane.failed = error.category == "journal_conflict"
                raise
            except BaseException:
                lane.failed = True
                raise
            self.metrics.set_unresolved((request.provider_kind, request.authority_instance), False)
            quiescence = json.dumps(
                {
                    "authority_instance": request.authority_instance,
                    "generation": request.generation,
                    "lower_operations": [
                        {
                            "digest": record_digest(record),
                            "outcome": record.outcome,
                            "sequence": record.sequence,
                        }
                        for record in records
                        if record.phase is JournalPhase.TERMINAL
                        and record.generation < request.generation
                    ],
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
        authenticated = self._require_peer(peer, request)
        trusted = await self._repository.resolve_current_candidate(authenticated, request)
        if trusted is None or not self._binding_matches(trusted, request):
            raise self._reject("superseded", labels=self._trusted_labels(trusted))
        lane = self._lane(trusted.system_id)

        async def run() -> AuthorityObservationV1:
            active = _ActiveOperation(request.generation, JournalPhase.ADMITTED, asyncio.Event())
            try:
                async with lane.lock:
                    if lane.failed:
                        raise AuthorityServiceError("journal_conflict")
                    if lane.active is not None:
                        raise AuthorityServiceError("superseded")
                    journal, records = self._lane_journal(request.system_id, lane)
                    acknowledgements = [
                        record
                        for record in records
                        if record.phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
                        and record.generation == request.generation
                    ]
                    if not acknowledgements:
                        raise AuthorityServiceError("superseded")
                    acknowledgement = acknowledgements[-1]
                    confirmed = await self._repository.resolve_current(
                        authenticated,
                        request,
                        acknowledgement.sequence,
                        record_digest(acknowledgement),
                    )
                    if confirmed is None or confirmed != trusted:
                        raise AuthorityServiceError("superseded")
                    binding = trusted
                    records = await self._recover(binding, journal, records)
                    phases_by_operation: dict[str, JournalRecordV1] = {}
                    for record in reversed(records):
                        phases_by_operation.setdefault(record.operation_identity, record)
                    unresolved = next(
                        (
                            record
                            for record in phases_by_operation.values()
                            if record.phase
                            in {
                                JournalPhase.ADMITTED,
                                JournalPhase.MUTATION_STARTED,
                                JournalPhase.PROVIDER_RETURNED,
                                JournalPhase.OBSERVED,
                            }
                        ),
                        None,
                    )
                    if unresolved is not None:
                        head = await self._repository.read_head(binding)
                        if head is None:
                            raise AuthorityServiceError("journal_conflict")
                        if head.suspended_operation is not None:
                            await self._recover_suspended(
                                binding,
                                journal,
                                records,
                                unresolved,
                                head.suspended_operation,
                            )
                        elif (
                            binding.state == "current"
                            and binding.authority_id == unresolved.authority_id
                            and binding.generation == unresolved.generation
                            and binding.system_id == unresolved.system_id
                            and binding.activation_id == unresolved.activation_id
                            and binding.run_id == unresolved.run_id
                            and binding.plan_identity == unresolved.plan_identity
                            and binding.purpose == unresolved.purpose
                            and binding.provider_kind == unresolved.provider_kind
                            and binding.authority_instance == unresolved.authority_instance
                        ):
                            await self._finish_recovery(binding, journal, records, unresolved)
                        else:
                            raise AuthorityServiceError("journal_conflict")
                        raise AuthorityServiceError("provider_conflict")
                    records = await self._anchor(
                        binding,
                        journal,
                        records,
                        self._record(request, records, JournalPhase.ADMITTED),
                    )
                    lane.active = active
                await asyncio.sleep(0)
                async with lane.lock:
                    if active.stop_before_start:
                        await self._anchor(
                            binding,
                            journal,
                            records,
                            self._record(
                                request,
                                records,
                                JournalPhase.TERMINAL,
                                outcome="never-began",
                            ),
                        )
                        raise AuthorityServiceError("superseded")
                    records = await self._anchor(
                        binding,
                        journal,
                        records,
                        self._record(request, records, JournalPhase.MUTATION_STARTED),
                    )
                    active.phase = JournalPhase.MUTATION_STARTED
                rechecked = await self._repository.resolve_current(
                    authenticated,
                    request,
                    acknowledgement.sequence,
                    record_digest(acknowledgement),
                )
                if rechecked is None or not self._binding_matches(rechecked, request):
                    raise AuthorityServiceError("superseded")
                try:
                    await self._adapter.commit(request, request.operation)
                except Exception:
                    raise self._provider_error(request) from None
                async with lane.lock:
                    completion_binding = active.completion_binding or binding
                    records = await self._anchor(
                        completion_binding,
                        journal,
                        records,
                        self._record(request, records, JournalPhase.PROVIDER_RETURNED),
                    )
                try:
                    observation = await self._adapter.observe(request)
                except Exception:
                    raise self._provider_error(request) from None
                async with lane.lock:
                    completion_binding = active.completion_binding or binding
                    records = await self._anchor(
                        completion_binding,
                        journal,
                        records,
                        self._record(
                            request, records, JournalPhase.OBSERVED, observation=observation
                        ),
                    )
                    outcome = (
                        observation.category
                        if observation.category in {"source", "target", "conflict"}
                        else "conflict"
                    )
                    await self._anchor(
                        completion_binding,
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
            finally:
                active.done.set()
                if lane.active is active:
                    lane.active = None
                self._release_lane(trusted.system_id, lane)

        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except AuthorityServiceError as error:
            self._ensure_rejection(request, error)
            raise
