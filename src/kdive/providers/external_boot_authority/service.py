"""Serialized provider-host external-boot authority lanes (ADR-0584)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast, runtime_checkable
from uuid import UUID

from kdive.db.external_boot_authority_journal import AuthorityBinding, JournalHead
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    GENESIS_DIGEST,
    AuthorityAcknowledgementV1,
    AuthorityCommitContextV1,
    AuthorityConflictResolutionRequestV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    AuthorityPreparationMutationRequestV1,
    AuthorityPreparationResponseV1,
    AuthorityRecoveryObservationContextV1,
    AuthorityRunningObservationV1,
    AuthorityTakeoverRequestV1,
    JournalPhase,
    JournalRecordV1,
    record_digest,
)
from kdive.providers.ports.external_boot import (
    ExternalBootPreparationObservation,
    RunningKernelObservation,
)


@dataclass(frozen=True, slots=True)
class AuthenticatedPeer:
    """Identity established by the hosting authentication boundary."""

    incarnation_id: UUID | str


class AuthorityMutationAdapter(Protocol):
    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1: ...

    async def commit(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityObservationV1: ...


@runtime_checkable
class AuthorityMutationFinalizer(Protocol):
    async def finalize(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> None: ...


@runtime_checkable
class AuthorityRecoveryObserver(Protocol):
    async def observe_recovery(
        self,
        request: AuthorityMutationRequestV1,
        context: AuthorityRecoveryObservationContextV1,
    ) -> AuthorityObservationV1: ...


@runtime_checkable
class AuthorityRunningObserver(Protocol):
    async def observe_running(
        self, request: AuthorityMutationRequestV1
    ) -> RunningKernelObservation: ...


@runtime_checkable
class AuthorityPreparationAdapter(Protocol):
    async def preparation_receipt(
        self, request: AuthorityPreparationMutationRequestV1
    ) -> ExternalBootPreparationObservation: ...


@runtime_checkable
class AuthorityPreparationAdopter(Protocol):
    async def adopt_preparation(
        self,
        request: AuthorityPreparationMutationRequestV1,
        predecessor: AuthorityPreparationMutationRequestV1,
        predecessor_receipt_identity: str,
        context: AuthorityCommitContextV1,
    ) -> AuthorityObservationV1: ...


@runtime_checkable
class AuthorityAdapterCloser(Protocol):
    def close(self) -> None: ...


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

    async def acknowledge(
        self,
        peer: AuthenticatedPeer,
        binding: AuthorityBinding,
        request: AuthorityTakeoverRequestV1,
        acknowledgement: AuthorityAcknowledgementV1,
    ) -> AuthorityAcknowledgementV1 | None: ...

    async def advance(
        self,
        binding: AuthorityBinding,
        expected_sequence: int,
        expected_digest: str,
        record: JournalRecordV1,
    ) -> Literal["advanced", "superseded", "conflict"]: ...


@runtime_checkable
class AuthorityPreparationRepository(Protocol):
    async def resolve_current_preparation(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityPreparationMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> AuthorityBinding | None: ...


@runtime_checkable
class AuthorityReleasePhaseRepository(Protocol):
    async def resolve_current_release_phase(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> AuthorityBinding | None: ...


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
        self._completion_tasks: set[asyncio.Task[object]] = set()
        self._accepting = True
        self._closed = False
        self._logger = logging.getLogger(__name__)

    async def close(self) -> None:
        """Stop admission, drain completion-owned mutations, then close the adapter."""
        self._accepting = False
        cancellation: asyncio.CancelledError | None = None
        while self._completion_tasks:
            pending = asyncio.gather(*tuple(self._completion_tasks), return_exceptions=True)
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError as error:
                cancellation = error
        if not self._closed and isinstance(self._adapter, AuthorityAdapterCloser):
            self._adapter.close()
        self._closed = True
        if cancellation is not None:
            raise cancellation

    def _track_completion(self, task: asyncio.Task[object]) -> None:
        self._completion_tasks.add(task)

        def completed(done: asyncio.Task[object]) -> None:
            self._completion_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)

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

    @staticmethod
    def _root_candidate_matches_preparation(
        binding: AuthorityBinding, request: AuthorityPreparationMutationRequestV1
    ) -> bool:
        return (
            binding.authority_id == request.authority_id
            and binding.generation == request.generation
            and binding.system_id == request.system_id
            and binding.activation_id == request.activation_id
            and binding.run_id == request.run_id
            and binding.plan_identity == request.plan_identity
            and binding.purpose == request.purpose == "activate"
            and binding.operation is AuthorityOperation.ACTIVATE
            and binding.provider_kind == request.provider_kind
            and binding.authority_instance == request.authority_instance
        )

    @staticmethod
    def _root_candidate_matches_release_phase(
        binding: AuthorityBinding, request: AuthorityMutationRequestV1
    ) -> bool:
        return (
            request.purpose == "release"
            and request.operation in {AuthorityOperation.RECOVER, AuthorityOperation.CLEANUP}
            and binding.authority_id == request.authority_id
            and binding.generation == request.generation
            and binding.system_id == request.system_id
            and binding.activation_id == request.activation_id
            and binding.run_id == request.run_id
            and binding.plan_identity == request.plan_identity
            and binding.purpose == "release"
            and binding.operation is AuthorityOperation.RELEASE
            and binding.provider_kind == request.provider_kind
            and binding.authority_instance == request.authority_instance
        )

    async def _resolve_confirmed(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityMutationRequestV1,
        acknowledgement: JournalRecordV1,
    ) -> AuthorityBinding | None:
        if isinstance(request, AuthorityPreparationMutationRequestV1):
            if not isinstance(self._repository, AuthorityPreparationRepository):
                return None
            return await self._repository.resolve_current_preparation(
                peer,
                request,
                acknowledgement.sequence,
                record_digest(acknowledgement),
            )
        if request.purpose == "release" and request.operation in {
            AuthorityOperation.RECOVER,
            AuthorityOperation.CLEANUP,
        }:
            if not isinstance(self._repository, AuthorityReleasePhaseRepository):
                return None
            return await self._repository.resolve_current_release_phase(
                peer,
                request,
                acknowledgement.sequence,
                record_digest(acknowledgement),
            )
        return await self._repository.resolve_current(
            peer,
            request,
            acknowledgement.sequence,
            record_digest(acknowledgement),
        )

    @staticmethod
    def _operation_matches(record: JournalRecordV1, request: AuthorityMutationRequestV1) -> bool:
        """Match immutable operation facts while allowing a successor authority generation."""
        return (
            record.system_id == request.system_id
            and record.activation_id == request.activation_id
            and record.run_id == request.run_id
            and record.plan_identity == request.plan_identity
            and record.purpose == request.purpose
            and record.operation == request.operation
            and record.provider_kind == request.provider_kind
            and record.authority_instance == request.authority_instance
            and record.operation_identity == request.operation_identity
            and record.operation_digest == request.operation_digest
        )

    async def _finalize_adapter(
        self,
        request: AuthorityMutationRequestV1,
        records: list[JournalRecordV1],
    ) -> None:
        if not isinstance(self._adapter, AuthorityMutationFinalizer):
            return
        started = next(
            (
                record
                for record in reversed(records)
                if record.operation_identity == request.operation_identity
                and record.phase is JournalPhase.MUTATION_STARTED
            ),
            None,
        )
        if started is None:
            raise AuthorityServiceError("journal_conflict")
        try:
            await self._adapter.finalize(request, AuthorityCommitContextV1.for_record(started))
        except AuthorityServiceError:
            raise
        except Exception:
            raise self._provider_error(request) from None

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

    async def _observation_head_is_current(
        self, binding: AuthorityBinding, records: list[JournalRecordV1]
    ) -> None:
        """Refuse an observation unless its local journal ends at the trusted exact head.

        Unlike mutation recovery, this only compares durable facts.  An observation must not
        repair, append, or truncate local history merely to make a provider read admissible.
        """
        if not records:
            raise AuthorityServiceError("journal_conflict")
        head = await self._repository.read_head(binding)
        last = records[-1]
        if (
            head is None
            or head.authority_instance != binding.authority_instance
            or head.system_id != binding.system_id
            or head.sequence != last.sequence
            or head.digest != record_digest(last)
            or head.phase is not last.phase
            or head.authority_id != last.authority_id
            or head.generation != last.generation
            or head.operation_identity != last.operation_identity
        ):
            raise AuthorityServiceError("journal_conflict")

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

    def _provider_error(
        self, request: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1
    ) -> AuthorityServiceError:
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

    async def _head_still_anchors(
        self, binding: AuthorityBinding, context: AuthorityCommitContextV1
    ) -> bool:
        """Return whether the anchored record is still this operation's trusted head.

        ``advance`` reports what the repository accepted, not what it still holds, and the
        provider call happens outside the lane lock. What this catches is a trusted head that
        stopped matching the record under *this* operation identity: a second authority
        instance sharing the identity, or a head row that changed after ``advance`` returned.

        Scoped to the operation identity on purpose, and it does **not** catch the
        concurrent-takeover window. ``acknowledge_takeover`` anchors its supersession and
        watermark records under a *different* operation identity before awaiting
        ``active.done``, so the lane head legitimately moves past an in-flight mutation;
        comparing against the bare head would reject the overlap ADR-0584 designs for and
        defeat the ``completion_binding`` path that lets the in-flight commit finish.
        """
        head = await self._repository.read_head(binding)
        if head is None:
            return False
        if head.operation_identity != context.operation_identity:
            return True
        return (
            head.sequence == context.journal_sequence
            and head.digest == context.journal_digest
            and head.phase is JournalPhase.MUTATION_STARTED
        )

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
        values.pop("plan", None)
        values.pop("expected_observed_composite", None)
        if not isinstance(request, AuthorityTakeoverRequestV1):
            values |= {
                "expected_source_identity": request.expected_source_identity,
                "intended_target_identity": request.intended_target_identity,
                "recovery_objects": request.recovery_objects,
            }
        values.update(changes)
        return JournalRecordV1.model_validate(values)

    @staticmethod
    def _mutation_from_record(
        record: JournalRecordV1, binding: AuthorityBinding
    ) -> AuthorityMutationRequestV1:
        values: dict[str, object] = dict(
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
        if record.operation in {AuthorityOperation.MATERIALIZE, AuthorityOperation.PREPARE}:
            if binding.preparation_plan is None:
                raise AuthorityServiceError("journal_conflict")
            values["plan"] = binding.preparation_plan
            return cast(
                AuthorityMutationRequestV1,
                AuthorityPreparationMutationRequestV1.model_validate(values),
            )
        return AuthorityMutationRequestV1.model_validate(values)

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
        request = self._mutation_from_record(prior, binding)
        if prior.phase is JournalPhase.ADMITTED:
            terminal = self._record(request, records, JournalPhase.TERMINAL, outcome="never-began")
            return await self._anchor(binding, journal, records, terminal)
        if prior.phase is JournalPhase.MUTATION_STARTED:
            try:
                observation = await self._recovery_observation(request, prior)
            except AuthorityServiceError:
                # Already a bounded category; re-classifying it as provider_conflict would
                # lose a superseded verdict the adapter is entitled to reach.
                raise
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
                observation = await self._recovery_observation(request, prior)
            except AuthorityServiceError:
                # Already a bounded category; re-classifying it as provider_conflict would
                # lose a superseded verdict the adapter is entitled to reach.
                raise
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

    async def _recovery_observation(
        self, request: AuthorityMutationRequestV1, record: JournalRecordV1
    ) -> AuthorityObservationV1:
        if request.operation is AuthorityOperation.TEARDOWN and isinstance(
            self._adapter, AuthorityRecoveryObserver
        ):
            return await self._adapter.observe_recovery(
                request, AuthorityRecoveryObservationContextV1.for_record(record)
            )
        return await self._adapter.observe(request)

    async def acknowledge_takeover(
        self, peer: AuthenticatedPeer | None, request: AuthorityTakeoverRequestV1
    ) -> AuthorityAcknowledgementV1:
        if not self._accepting:
            raise AuthorityServiceError("superseded")
        task = asyncio.create_task(self._acknowledge_takeover(peer, request))
        self._track_completion(task)
        return await asyncio.shield(task)

    async def _acknowledge_takeover(
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

    @staticmethod
    def _acknowledgement_response(
        request: AuthorityTakeoverRequestV1,
        records: list[JournalRecordV1],
        watermark: JournalRecordV1,
        acknowledgement: JournalRecordV1,
    ) -> AuthorityAcknowledgementV1:
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
                acknowledgement = next(
                    (
                        record
                        for record in records
                        if record.phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
                        and record.authority_id == request.authority_id
                        and record.generation == request.generation
                        and record.operation_identity == request.operation_identity
                        and record.operation_digest == request.operation_digest
                    ),
                    None,
                )
                if acknowledgement is not None:
                    trusted = await self._repository.read_head(binding)
                    if trusted is None:
                        raise AuthorityServiceError("journal_conflict")
                    watermark = next(
                        (
                            record
                            for record in records
                            if record.sequence == acknowledgement.watermark_sequence
                            and record_digest(record) == acknowledgement.watermark_digest
                        ),
                        None,
                    )
                    if watermark is None:
                        raise AuthorityServiceError("journal_conflict")
                    response = self._acknowledgement_response(
                        request, records[: acknowledgement.sequence], watermark, acknowledgement
                    )
                    projected = await self._repository.acknowledge(
                        authenticated, binding, request, response
                    )
                    if projected is None or projected != response:
                        raise AuthorityServiceError("superseded")
                    return projected
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
            response = self._acknowledgement_response(request, records, watermark, acknowledgement)
            projected = await self._repository.acknowledge(
                authenticated, binding, request, response
            )
            if projected is None or projected != response:
                raise AuthorityServiceError("superseded")
            return projected

    async def execute_mutation(
        self, peer: AuthenticatedPeer | None, request: AuthorityMutationRequestV1
    ) -> AuthorityObservationV1:
        if not self._accepting:
            raise AuthorityServiceError("superseded")
        authenticated = self._require_peer(peer, request)
        trusted = await self._repository.resolve_current_candidate(authenticated, request)
        candidate_matches = trusted is not None and (
            self._root_candidate_matches_preparation(trusted, request)
            if isinstance(request, AuthorityPreparationMutationRequestV1)
            else self._root_candidate_matches_release_phase(trusted, request)
            if request.purpose == "release"
            and request.operation in {AuthorityOperation.RECOVER, AuthorityOperation.CLEANUP}
            else self._binding_matches(trusted, request)
        )
        if not candidate_matches:
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
                    confirmed = await self._resolve_confirmed(
                        authenticated, request, acknowledgement
                    )
                    if confirmed is None or not self._binding_matches(confirmed, request):
                        raise AuthorityServiceError("superseded")
                    binding = confirmed
                    records = await self._recover(binding, journal, records)
                    phases_by_operation: dict[str, JournalRecordV1] = {}
                    for record in reversed(records):
                        phases_by_operation.setdefault(record.operation_identity, record)
                    prior = phases_by_operation.get(request.operation_identity)
                    if prior is not None and prior.phase is JournalPhase.TERMINAL:
                        if not self._operation_matches(prior, request) or prior.observation is None:
                            raise AuthorityServiceError("journal_conflict")
                        await self._finalize_adapter(request, records)
                        return prior.observation
                    predecessor: AuthorityPreparationMutationRequestV1 | None = None
                    predecessor_receipt_identity: str | None = None
                    adopted_release_phase: AuthorityObservationV1 | None = None
                    if isinstance(request, AuthorityPreparationMutationRequestV1):
                        predecessor_record = next(
                            (
                                record
                                for record in reversed(records)
                                if record.phase is JournalPhase.TERMINAL
                                and record.operation == request.operation
                                and record.generation < request.generation
                            ),
                            None,
                        )
                        if predecessor_record is not None:
                            if (
                                predecessor_record.outcome != "target"
                                or predecessor_record.observation is None
                                or predecessor_record.observation.category != "target"
                            ):
                                raise AuthorityServiceError("journal_conflict")
                            predecessor_receipt_identity = (
                                predecessor_record.observation.composite_state
                            )
                            predecessor = request.model_copy(
                                update={
                                    "authority_id": predecessor_record.authority_id,
                                    "generation": predecessor_record.generation,
                                    "attempt_id": predecessor_record.attempt_id,
                                    "operation_identity": predecessor_record.operation_identity,
                                    "operation_digest": predecessor_record.operation_digest,
                                    "expected_source_identity": (
                                        predecessor_record.expected_source_identity
                                    ),
                                    "intended_target_identity": (
                                        predecessor_record.intended_target_identity
                                    ),
                                    "recovery_objects": predecessor_record.recovery_objects,
                                }
                            )
                            if not self._operation_matches(predecessor_record, predecessor):
                                raise AuthorityServiceError("journal_conflict")
                    if request.purpose == "release" and request.operation in {
                        AuthorityOperation.RECOVER,
                        AuthorityOperation.CLEANUP,
                    }:
                        prior_release_phase = next(
                            (
                                record
                                for record in reversed(records)
                                if record.phase is JournalPhase.TERMINAL
                                and record.operation == request.operation
                                and record.generation < request.generation
                            ),
                            None,
                        )
                        if prior_release_phase is not None:
                            candidate = request.model_copy(
                                update={
                                    "authority_id": prior_release_phase.authority_id,
                                    "generation": prior_release_phase.generation,
                                    "attempt_id": prior_release_phase.attempt_id,
                                    "operation_identity": prior_release_phase.operation_identity,
                                    "operation_digest": prior_release_phase.operation_digest,
                                    "expected_source_identity": (
                                        prior_release_phase.expected_source_identity
                                    ),
                                    "intended_target_identity": (
                                        prior_release_phase.intended_target_identity
                                    ),
                                    "recovery_objects": prior_release_phase.recovery_objects,
                                }
                            )
                            expected_outcome = (
                                "source"
                                if request.operation is AuthorityOperation.RECOVER
                                else "absent"
                            )
                            if (
                                prior_release_phase.outcome != expected_outcome
                                or prior_release_phase.observation is None
                                or prior_release_phase.observation.category != expected_outcome
                                or not self._operation_matches(prior_release_phase, candidate)
                            ):
                                raise AuthorityServiceError("journal_conflict")
                            adopted_release_phase = prior_release_phase.observation
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
                    context = AuthorityCommitContextV1.for_record(records[-1])
                rechecked = await self._resolve_confirmed(authenticated, request, acknowledgement)
                if rechecked is None or not self._binding_matches(rechecked, request):
                    raise AuthorityServiceError("superseded")
                # Both refusals here leave the operation unresolved at `mutation-started`,
                # which is the journal's design rather than a gap: `_NEXT_OPERATION_PHASES`
                # allows `mutation-started` to be followed only by `provider-returned`, so a
                # `terminal`/`never-began` record is not a legal successor. Once the anchor is
                # written, ADR-0584 treats the mutation as possibly-begun.
                #
                # The two refusals then differ, and only the first is self-healing. A
                # `superseded` recheck failure is settled by the observation cycle
                # `_finish_recovery` runs on the next admission. A `journal_conflict` from the
                # head check is not: it means the trusted head already disagrees with the
                # record this service anchored under the same operation identity, so
                # `_recover` and `_anchor`'s compare-and-set both keep failing against that
                # head and the lane answers `journal_conflict` until a takeover or an operator
                # reconciles it. That is the correct visible answer for a head the service
                # cannot reconcile, not a state it should paper over.
                if not await self._head_still_anchors(binding, context):
                    raise AuthorityServiceError("journal_conflict")
                if isinstance(request, AuthorityConflictResolutionRequestV1):
                    observed = await self._adapter.observe(request)
                    if (
                        observed.category == "unreadable"
                        or observed.composite_state != request.expected_observed_composite
                    ):
                        raise AuthorityServiceError("superseded")
                try:
                    if predecessor is not None:
                        if not isinstance(self._adapter, AuthorityPreparationAdopter):
                            raise AuthorityServiceError("provider_conflict")
                        await self._adapter.adopt_preparation(
                            cast(AuthorityPreparationMutationRequestV1, request),
                            predecessor,
                            cast(str, predecessor_receipt_identity),
                            context,
                        )
                    elif adopted_release_phase is None:
                        await self._adapter.commit(request, context)
                except AuthorityServiceError:
                    # Already a bounded category; re-classifying it as provider_conflict would
                    # lose a superseded verdict the adapter is entitled to reach.
                    raise
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
                    observation = (
                        adopted_release_phase
                        if adopted_release_phase is not None
                        else await self._adapter.observe(request)
                    )
                except AuthorityServiceError:
                    # Already a bounded category; re-classifying it as provider_conflict would
                    # lose a superseded verdict the adapter is entitled to reach.
                    raise
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
                        if observation.category in {"absent", "source", "target", "conflict"}
                        else "conflict"
                    )
                    records = await self._anchor(
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
                    await self._finalize_adapter(request, records)
                    return observation
            finally:
                active.done.set()
                if lane.active is active:
                    lane.active = None
                self._release_lane(trusted.system_id, lane)

        if not self._accepting:
            self._release_lane(trusted.system_id, lane)
            raise AuthorityServiceError("superseded")
        task = asyncio.create_task(run())
        self._track_completion(task)
        try:
            return await asyncio.shield(task)
        except AuthorityServiceError as error:
            self._ensure_rejection(request, error)
            raise

    async def execute_conflict_resolution(
        self, peer: AuthenticatedPeer | None, request: AuthorityConflictResolutionRequestV1
    ) -> AuthorityObservationV1:
        """Mutate only after the authority re-observes the caller-bound conflict identity."""
        return await self.execute_mutation(peer, request)

    async def observe_authority(
        self, peer: AuthenticatedPeer | None, request: AuthorityMutationRequestV1
    ) -> AuthorityObservationV1:
        """Read one current authority-bound provider state without changing its journal or provider.

        The acknowledgement and current authority are checked both before and after the provider
        read.  A concurrent takeover therefore cannot turn an observation made under a stale
        generation into an admission fact for a later mutation.
        """
        return await self._observe_current(peer, request, self._adapter.observe)

    async def observe_running(
        self, peer: AuthenticatedPeer | None, request: AuthorityMutationRequestV1
    ) -> AuthorityRunningObservationV1:
        """Read bounded kernel evidence through the same authenticated read-only lane."""

        async def read(request: AuthorityMutationRequestV1) -> AuthorityRunningObservationV1:
            if not isinstance(self._adapter, AuthorityRunningObserver):
                raise AuthorityServiceError("provider_conflict")
            observed = await self._adapter.observe_running(request)
            return AuthorityRunningObservationV1.from_observation(observed)

        return await self._observe_current(peer, request, read)

    async def _observe_current[T](
        self,
        peer: AuthenticatedPeer | None,
        request: AuthorityMutationRequestV1,
        read: Callable[[AuthorityMutationRequestV1], Awaitable[T]],
    ) -> T:
        authenticated = self._require_peer(peer, request)
        trusted = await self._repository.resolve_current_candidate(authenticated, request)
        if trusted is None or not self._binding_matches(trusted, request):
            raise self._reject("superseded", labels=self._trusted_labels(trusted))
        lane = self._lane(trusted.system_id)

        async def run() -> T:
            try:
                async with lane.lock:
                    if lane.failed:
                        raise AuthorityServiceError("journal_conflict")
                    if lane.active is not None:
                        raise AuthorityServiceError("superseded")
                    _journal, records = self._lane_journal(request.system_id, lane)
                    await self._observation_head_is_current(trusted, records)
                    acknowledgements = [
                        record
                        for record in records
                        if record.phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
                        and record.generation == request.generation
                    ]
                    if not acknowledgements:
                        raise AuthorityServiceError("superseded")
                    acknowledgement = acknowledgements[-1]
                    confirmed = await self._resolve_confirmed(
                        authenticated, request, acknowledgement
                    )
                    if confirmed is None or not self._binding_matches(confirmed, request):
                        raise AuthorityServiceError("superseded")
                    try:
                        observation = await read(request)
                    except AuthorityServiceError:
                        raise
                    except Exception:
                        raise self._provider_error(request) from None
                    rechecked = await self._resolve_confirmed(
                        authenticated, request, acknowledgement
                    )
                    if rechecked is None or not self._binding_matches(rechecked, request):
                        raise AuthorityServiceError("superseded")
                    await self._observation_head_is_current(rechecked, records)
                    return observation
            finally:
                self._release_lane(trusted.system_id, lane)

        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except AuthorityServiceError as error:
            self._ensure_rejection(request, error)
            raise

    async def execute_preparation(
        self,
        peer: AuthenticatedPeer | None,
        request: AuthorityPreparationMutationRequestV1,
    ) -> AuthorityPreparationResponseV1:
        """Execute through the authenticated lane, then reopen its durable receipt."""
        if not self._accepting:
            raise AuthorityServiceError("superseded")
        task = asyncio.create_task(self._execute_preparation(peer, request))
        self._track_completion(task)
        return await asyncio.shield(task)

    async def _execute_preparation(
        self,
        peer: AuthenticatedPeer | None,
        request: AuthorityPreparationMutationRequestV1,
    ) -> AuthorityPreparationResponseV1:
        observation = await self.execute_mutation(peer, cast(AuthorityMutationRequestV1, request))
        if not isinstance(self._adapter, AuthorityPreparationAdapter):
            raise self._provider_error(request)
        try:
            receipt = await self._adapter.preparation_receipt(request)
        except AuthorityServiceError:
            raise
        except Exception:
            raise self._provider_error(request) from None
        if receipt.identity != observation.composite_state:
            raise self._provider_error(request)
        journal = self._journal_factory(request.system_id)
        try:
            terminal = next(
                (
                    record
                    for record in reversed(list(journal.load()))
                    if record.operation_identity == request.operation_identity
                    and record.attempt_id == request.attempt_id
                    and record.phase is JournalPhase.TERMINAL
                    and record.observation == observation
                ),
                None,
            )
        finally:
            journal.close()
        if terminal is None:
            raise AuthorityServiceError("journal_conflict")
        return AuthorityPreparationResponseV1(
            observation=observation,
            receipt=receipt,
            journal_sequence=terminal.sequence,
            journal_digest=record_digest(terminal),
        )
