"""Serialized provider-host external-boot authority lanes (ADR-0584)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
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

    @classmethod
    def empty(cls) -> AuthorityServiceMetrics:
        return cls({}, {}, {}, {}, {})

    @staticmethod
    def _labels(
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1 | JournalRecordV1,
    ) -> tuple[str, str]:
        return request.provider_kind, request.authority_instance

    def reject(
        self,
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1 | JournalRecordV1,
        category: str,
    ) -> None:
        key = (*self._labels(request), category)
        self.rejections[key] = self.rejections.get(key, 0) + 1

    def recovery_failed(self, request: AuthorityTakeoverRequestV1) -> None:
        key = self._labels(request)
        self.recovery_failures[key] = self.recovery_failures.get(key, 0) + 1

    def record_checkpoint(self, request: JournalRecordV1, elapsed: float) -> None:
        key = self._labels(request)
        self.checkpoints[key] = self.checkpoints.get(key, 0) + 1
        count, total = self.checkpoint_latency.get(key, (0, 0.0))
        self.checkpoint_latency[key] = count + 1, total + elapsed


@dataclass(slots=True)
class _Lane:
    lock: asyncio.Lock
    users: int = 0
    failed: bool = False
    watermark_generation: int = 0
    active: _ActiveOperation | None = None


@dataclass(slots=True)
class _ActiveOperation:
    generation: int
    phase: JournalPhase
    done: asyncio.Event
    stop_before_start: bool = False


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

    @staticmethod
    def _trusted_labels(binding: AuthorityBinding | None) -> tuple[str, str]:
        if binding is None:
            return "untrusted", "unresolved"
        return binding.provider_kind, binding.authority_instance

    def _reject(
        self,
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1 | JournalRecordV1,
        category: Literal["unauthenticated", "superseded", "journal_conflict"],
        *,
        labels: tuple[str, str] = ("untrusted", "unresolved"),
    ) -> AuthorityServiceError:
        key = (*labels, category)
        self.metrics.rejections[key] = self.metrics.rejections.get(key, 0) + 1
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
        self.metrics.reject(request, error.category)
        self._logger.warning(
            "authority request rejected",
            extra={
                "provider_kind": request.provider_kind,
                "authority_instance": request.authority_instance,
                "category": error.category,
            },
        )
        error.telemetry_recorded = True

    def _require_peer(
        self,
        peer: AuthenticatedPeer | None,
        request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1,
    ) -> AuthenticatedPeer:
        if peer is None or not isinstance(peer.incarnation_id, UUID):
            raise self._reject(request, "unauthenticated")
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
                record,
                "superseded" if status == "superseded" else "journal_conflict",
                labels=self._trusted_labels(binding),
            )
        self.metrics.record_checkpoint(record, time.perf_counter() - started)
        return (*records, record)

    def _provider_error(self, request: AuthorityMutationRequestV1) -> AuthorityServiceError:
        self.metrics.reject(request, "provider_conflict")
        self._logger.warning(
            "authority provider boundary failed",
            extra={
                "provider_kind": request.provider_kind,
                "authority_instance": request.authority_instance,
                "category": "provider_conflict",
            },
        )
        return AuthorityServiceError("provider_conflict", telemetry_recorded=True)

    async def readiness(
        self, peer: AuthenticatedPeer | None, request: AuthorityTakeoverRequestV1
    ) -> bool:
        """Return true only when local bytes exactly equal the scoped trusted head."""
        try:
            authenticated = self._require_peer(peer, request)
            binding = await self._repository.resolve_allocating(authenticated, request)
            if binding is None or not self._binding_matches(binding, request):
                return False
            records = await self._recover(binding, self._journal_factory(request.system_id))
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
            operation=record.operation or "",
            attempt_id=record.attempt_id,
            expected_source_identity=record.expected_source_identity or "",
            intended_target_identity=record.intended_target_identity or "",
            recovery_objects=record.recovery_objects,
        )

    async def _recover_suspended(
        self,
        binding: AuthorityBinding,
        journal: FileAuthorityJournal,
        records: tuple[JournalRecordV1, ...],
        prior: JournalRecordV1,
        suspended: object,
    ) -> tuple[JournalRecordV1, ...]:
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
            raise self._reject(request, "superseded", labels=self._trusted_labels(binding))
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
                raise self._reject(
                    request, "journal_conflict", labels=self._trusted_labels(binding)
                )
            confirmed = await self._repository.resolve_allocating(authenticated, request)
            if confirmed is None or confirmed != binding:
                raise self._reject(request, "superseded", labels=self._trusted_labels(binding))
            journal = self._journal_factory(request.system_id)
            try:
                records = await self._recover(binding, journal)
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
                    self.metrics.unresolved[(request.provider_kind, request.authority_instance)] = 1
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
                    if active.phase is JournalPhase.ADMITTED:
                        active.stop_before_start = True
                    self.metrics.unresolved[(request.provider_kind, request.authority_instance)] = 1
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
                records = await self._recover(binding, journal)
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
            self.metrics.unresolved.pop((request.provider_kind, request.authority_instance), None)
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
        journal = self._journal_factory(request.system_id)
        records = journal.load()
        acknowledgements = [
            record
            for record in records
            if record.phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
            and record.generation == request.generation
        ]
        if not acknowledgements:
            raise self._reject(request, "superseded")
        acknowledgement = acknowledgements[-1]
        trusted = await self._repository.resolve_current(
            authenticated, request, acknowledgement.sequence, record_digest(acknowledgement)
        )
        if trusted is None or not self._binding_matches(trusted, request):
            raise self._reject(request, "superseded", labels=self._trusted_labels(trusted))
        lane = self._lane(trusted.system_id)

        async def run() -> AuthorityObservationV1:
            active = _ActiveOperation(request.generation, JournalPhase.ADMITTED, asyncio.Event())
            try:
                async with lane.lock:
                    if lane.failed:
                        raise AuthorityServiceError("journal_conflict")
                    if lane.active is not None:
                        raise AuthorityServiceError("superseded")
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
                    binding = trusted
                    records = await self._recover(binding, journal)
                    records = await self._anchor(
                        binding,
                        journal,
                        records,
                        self._record(request, records, JournalPhase.ADMITTED),
                    )
                    lane.active = active
                await asyncio.sleep(0)
                async with lane.lock:
                    records = journal.load()
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
                committed = rechecked is not None and self._binding_matches(rechecked, request)
                if not committed:
                    raise AuthorityServiceError("superseded")
                try:
                    await self._adapter.commit(request, request.operation)
                except Exception:
                    raise self._provider_error(request) from None
                async with lane.lock:
                    records = journal.load()
                    records = await self._anchor(
                        binding,
                        journal,
                        records,
                        self._record(request, records, JournalPhase.PROVIDER_RETURNED),
                    )
                try:
                    observation = await self._adapter.observe(request)
                except Exception:
                    raise self._provider_error(request) from None
                async with lane.lock:
                    records = journal.load()
                    records = await self._anchor(
                        binding,
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
