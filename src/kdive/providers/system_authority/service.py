"""Completion-owned authority service for activation-free System operations (ADR-0623)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from kdive.providers.system_authority.journal import FileAuthoritySystemJournal
from kdive.providers.system_authority.ports import AuthoritySystemProvider
from kdive.providers.system_authority.protocol import (
    GENESIS_DIGEST,
    AuthoritySystemAbsenceFacts,
    AuthoritySystemAcknowledgementV1,
    AuthoritySystemCommitContextV1,
    AuthoritySystemJournalPhase,
    AuthoritySystemJournalRecordV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemObservationV1,
    AuthoritySystemOperation,
    AuthoritySystemPreactivationAbsentV1,
    AuthoritySystemProofV1,
    AuthoritySystemProvisionFacts,
    AuthoritySystemProvisionReadyV1,
    AuthoritySystemResponseV1,
    AuthoritySystemRetainedQuarantineV1,
    AuthoritySystemTakeoverRequestV1,
    authority_system_record_digest,
    make_authority_system_record,
    parse_authority_system_proof,
    system_authority_digest,
)
from kdive.providers.system_authority.repository import (
    AuthoritySystemAdvanceResult,
    ResolvedAuthoritySystemOperation,
)


class AuthoritySystemServiceError(RuntimeError):
    """A request failed closed before returning an authority receipt."""

    def __init__(self, category: Literal["unauthenticated", "superseded", "journal-conflict"]):
        super().__init__(category)
        self.category = category


class AuthoritySystemRepository(Protocol):
    async def resolve_allocating(
        self, peer_incarnation: str, request: AuthoritySystemTakeoverRequestV1
    ) -> ResolvedAuthoritySystemOperation | None: ...

    async def resolve_current(
        self,
        peer_incarnation: str,
        request: AuthoritySystemMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> ResolvedAuthoritySystemOperation | None: ...

    async def advance_head(
        self,
        peer_incarnation: str,
        request: AuthoritySystemTakeoverRequestV1 | AuthoritySystemMutationRequestV1,
        *,
        expected_sequence: int,
        expected_digest: str,
        record: AuthoritySystemJournalRecordV1,
        receipt: AuthoritySystemProofV1 | None = None,
    ) -> AuthoritySystemAdvanceResult: ...


@dataclass(slots=True)
class _JournalState:
    records: list[AuthoritySystemJournalRecordV1]
    db_sequence: int
    db_digest: str


@dataclass(slots=True)
class _Lane:
    lock: asyncio.Lock
    journal: FileAuthoritySystemJournal | None = None
    users: int = 0


class AuthoritySystemService:
    """Serialize one completion-owned mutation at a time for each System."""

    def __init__(
        self,
        *,
        repository: AuthoritySystemRepository,
        journal_factory: Callable[[UUID], FileAuthoritySystemJournal],
        provider: AuthoritySystemProvider,
    ) -> None:
        self._repository = repository
        self._journal_factory = journal_factory
        self._provider = provider
        self._lanes: dict[UUID, _Lane] = {}
        self._tasks: set[asyncio.Task[object]] = set()
        self._accepting = True

    def _lane(self, system_id: UUID) -> _Lane:
        lane = self._lanes.setdefault(system_id, _Lane(asyncio.Lock()))
        lane.users += 1
        return lane

    def _release_lane(self, system_id: UUID, lane: _Lane) -> None:
        lane.users -= 1
        if lane.users == 0 and self._lanes.get(system_id) is lane:
            self._lanes.pop(system_id)
            if lane.journal is not None:
                lane.journal.close()

    def _journal(self, system_id: UUID, lane: _Lane) -> FileAuthoritySystemJournal:
        if lane.journal is None:
            lane.journal = self._journal_factory(system_id)
        return lane.journal

    def _track(self, task: asyncio.Task[object]) -> None:
        self._tasks.add(task)

        def done(completed: asyncio.Task[object]) -> None:
            self._tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)

    async def close(self) -> None:
        self._accepting = False
        cancellation: asyncio.CancelledError | None = None
        while self._tasks:
            pending = asyncio.gather(*tuple(self._tasks), return_exceptions=True)
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError as error:
                cancellation = error
        for lane in self._lanes.values():
            if lane.journal is not None:
                lane.journal.close()
        self._lanes.clear()
        if cancellation is not None:
            raise cancellation

    async def begin(
        self, peer_incarnation: str, request: AuthoritySystemTakeoverRequestV1
    ) -> AuthoritySystemAcknowledgementV1:
        if not self._accepting:
            raise AuthoritySystemServiceError("superseded")
        request = AuthoritySystemTakeoverRequestV1.model_validate(
            request.model_dump(mode="python", by_alias=True)
        )
        task = asyncio.create_task(self._begin(peer_incarnation, request))
        self._track(task)
        return await asyncio.shield(task)

    async def _begin(
        self, peer_incarnation: str, request: AuthoritySystemTakeoverRequestV1
    ) -> AuthoritySystemAcknowledgementV1:
        if not peer_incarnation:
            raise AuthoritySystemServiceError("unauthenticated")
        lane = self._lane(request.system_id)
        try:
            async with lane.lock:
                resolved = await self._repository.resolve_allocating(peer_incarnation, request)
                if resolved is None:
                    raise AuthoritySystemServiceError("superseded")
                journal = self._journal(request.system_id, lane)
                state = self._journal_state(resolved, journal)
                quiescence = await self._quiescence(request, resolved, state.records)
                last = state.records[-1] if state.records else None
                if not (
                    last is not None
                    and last.authority_id == request.authority_id
                    and last.generation == request.generation
                    and last.phase is AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED
                ):
                    if not (
                        last is not None
                        and last.authority_id == request.authority_id
                        and last.generation == request.generation
                        and last.phase
                        in {
                            AuthoritySystemJournalPhase.WATERMARK_INSTALLED,
                            AuthoritySystemJournalPhase.TAKEOVER_SUPERSEDED,
                        }
                    ):
                        await self._anchor(
                            peer_incarnation,
                            request,
                            resolved,
                            journal,
                            state,
                            AuthoritySystemJournalPhase.WATERMARK_INSTALLED,
                        )
                    predecessor = state.records[-2] if len(state.records) > 1 else None
                    if predecessor is not None and predecessor.generation < request.generation:
                        await self._anchor(
                            peer_incarnation,
                            request,
                            resolved,
                            journal,
                            state,
                            AuthoritySystemJournalPhase.TAKEOVER_SUPERSEDED,
                        )
                    await self._anchor(
                        peer_incarnation,
                        request,
                        resolved,
                        journal,
                        state,
                        AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED,
                    )
                acknowledgement = state.records[-1]
                return AuthoritySystemAcknowledgementV1(
                    authority_id=request.authority_id,
                    generation=request.generation,
                    attempt_id=request.attempt_id,
                    journal_sequence=acknowledgement.sequence,
                    journal_digest=authority_system_record_digest(acknowledgement),
                    quiescence_digest=quiescence,
                )
        finally:
            self._release_lane(request.system_id, lane)

    async def execute(
        self,
        peer_incarnation: str,
        request: AuthoritySystemMutationRequestV1,
        acknowledgement: AuthoritySystemAcknowledgementV1,
    ) -> AuthoritySystemResponseV1:
        if not self._accepting:
            raise AuthoritySystemServiceError("superseded")
        request = AuthoritySystemMutationRequestV1.model_validate(
            request.model_dump(mode="python", by_alias=True)
        )
        acknowledgement = AuthoritySystemAcknowledgementV1.model_validate(
            acknowledgement.model_dump(mode="python", by_alias=True)
        )
        if (
            acknowledgement.authority_id != request.authority_id
            or acknowledgement.generation != request.generation
            or acknowledgement.attempt_id != request.attempt_id
        ):
            raise AuthoritySystemServiceError("superseded")
        task = asyncio.create_task(
            self._execute(peer_incarnation, request, acknowledgement),
            name=f"authority-system-{request.system_id}",
        )
        self._track(task)
        return await asyncio.shield(task)

    async def _execute(
        self,
        peer_incarnation: str,
        request: AuthoritySystemMutationRequestV1,
        acknowledgement: AuthoritySystemAcknowledgementV1,
    ) -> AuthoritySystemResponseV1:
        if not peer_incarnation:
            raise AuthoritySystemServiceError("unauthenticated")
        lane = self._lane(request.system_id)
        try:
            async with lane.lock:
                resolved = await self._repository.resolve_current(
                    peer_incarnation,
                    request,
                    acknowledgement.journal_sequence,
                    acknowledgement.journal_digest,
                )
                if resolved is None:
                    raise AuthoritySystemServiceError("superseded")
                journal = self._journal(request.system_id, lane)
                state = self._journal_state(resolved, journal)
                if resolved.receipt_bytes is not None:
                    proof = parse_authority_system_proof(resolved.receipt_bytes)
                    return AuthoritySystemResponseV1(
                        proof=proof,
                        journal_sequence=resolved.head.sequence,
                        journal_digest=resolved.head.digest,
                    )
                newly_started = await self._ensure_mutation_started(
                    peer_incarnation, request, resolved, journal, state
                )
                started = next(
                    record
                    for record in reversed(state.records)
                    if record.phase is AuthoritySystemJournalPhase.MUTATION_STARTED
                )
                context = AuthoritySystemCommitContextV1.for_record(started)
                facts = await self._provider_facts(
                    request, context, resolved, execute=newly_started
                )
                if self._db_phase(state) is AuthoritySystemJournalPhase.MUTATION_STARTED:
                    await self._anchor(
                        peer_incarnation,
                        request,
                        resolved,
                        journal,
                        state,
                        AuthoritySystemJournalPhase.PROVIDER_RETURNED,
                    )
                observation = AuthoritySystemObservationV1(
                    category="owned" if facts.complete else "partial",
                    composite_state=system_authority_digest(facts),
                )
                if self._db_phase(state) is AuthoritySystemJournalPhase.PROVIDER_RETURNED:
                    await self._anchor(
                        peer_incarnation,
                        request,
                        resolved,
                        journal,
                        state,
                        AuthoritySystemJournalPhase.OBSERVED,
                        observation=observation,
                    )
                proof = self._proof(request, facts)
                terminal_observation = AuthoritySystemObservationV1(
                    category="owned" if facts.complete else "partial",
                    composite_state=system_authority_digest(proof),
                )
                await self._anchor(
                    peer_incarnation,
                    request,
                    resolved,
                    journal,
                    state,
                    AuthoritySystemJournalPhase.TERMINAL,
                    observation=terminal_observation,
                    outcome=proof.disposition,
                    receipt=proof,
                )
                terminal = state.records[-1]
                return AuthoritySystemResponseV1(
                    proof=proof,
                    journal_sequence=terminal.sequence,
                    journal_digest=authority_system_record_digest(terminal),
                )
        finally:
            self._release_lane(request.system_id, lane)

    @staticmethod
    def _db_phase(state: _JournalState) -> AuthoritySystemJournalPhase | None:
        if state.db_sequence == 0:
            return None
        return state.records[state.db_sequence - 1].phase

    @staticmethod
    def _journal_state(
        resolved: ResolvedAuthoritySystemOperation,
        journal: FileAuthoritySystemJournal,
    ) -> _JournalState:
        records = list(journal.read())
        if not records:
            if resolved.head.sequence != 0 or resolved.head.digest != GENESIS_DIGEST:
                raise AuthoritySystemServiceError("journal-conflict")
            return _JournalState(records, 0, GENESIS_DIGEST)
        if resolved.head.sequence == 0 and resolved.head.digest != GENESIS_DIGEST:
            raise AuthoritySystemServiceError("journal-conflict")
        if resolved.head.system_id != records[-1].system_id:
            raise AuthoritySystemServiceError("journal-conflict")
        if resolved.head.sequence > len(records) or len(records) > resolved.head.sequence + 1:
            raise AuthoritySystemServiceError("journal-conflict")
        if resolved.head.sequence:
            db_record = records[resolved.head.sequence - 1]
            if (
                resolved.head.digest != authority_system_record_digest(db_record)
                or resolved.head.phase is not db_record.phase
            ):
                raise AuthoritySystemServiceError("journal-conflict")
        return _JournalState(records, resolved.head.sequence, resolved.head.digest)

    async def _quiescence(
        self,
        request: AuthoritySystemTakeoverRequestV1,
        resolved: ResolvedAuthoritySystemOperation,
        records: list[AuthoritySystemJournalRecordV1],
    ) -> str:
        predecessor_records = records
        for index, record in enumerate(records):
            if (
                record.authority_id == request.authority_id
                and record.generation == request.generation
            ):
                predecessor_records = records[:index]
                break
        if (
            not predecessor_records
            or predecessor_records[-1].phase is AuthoritySystemJournalPhase.TERMINAL
        ):
            return system_authority_digest(request)
        last_terminal = next(
            (
                index
                for index in range(len(predecessor_records) - 1, -1, -1)
                if predecessor_records[index].phase is AuthoritySystemJournalPhase.TERMINAL
            ),
            -1,
        )
        started = next(
            (
                record
                for record in reversed(predecessor_records[last_terminal + 1 :])
                if record.phase is AuthoritySystemJournalPhase.MUTATION_STARTED
            ),
            None,
        )
        if started is None:
            return authority_system_record_digest(predecessor_records[-1])
        predecessor = AuthoritySystemMutationRequestV1.model_validate(
            started.model_dump(
                mode="python",
                by_alias=True,
                exclude={
                    "schema_",
                    "sequence",
                    "previous_digest",
                    "phase",
                    "observation",
                    "outcome",
                    "canonical_record",
                },
            )
        )
        context = AuthoritySystemCommitContextV1.for_record(started)
        if predecessor.operation is AuthoritySystemOperation.PROVISION:
            facts = await self._provider.observe_system_provision(
                predecessor, context, resolved.snapshot
            )
            facts = AuthoritySystemProvisionFacts.model_validate(
                facts.model_dump(mode="python", by_alias=True)
            )
        else:
            facts = await self._provider.observe_preactivation_teardown(predecessor, context)
            facts = AuthoritySystemAbsenceFacts.model_validate(
                facts.model_dump(mode="python", by_alias=True)
            )
        return system_authority_digest(facts)

    async def _ensure_mutation_started(
        self,
        peer_incarnation: str,
        request: AuthoritySystemMutationRequestV1,
        resolved: ResolvedAuthoritySystemOperation,
        journal: FileAuthoritySystemJournal,
        state: _JournalState,
    ) -> bool:
        own = [record for record in state.records if record.authority_id == request.authority_id]
        if own and own[-1].phase in {
            AuthoritySystemJournalPhase.MUTATION_STARTED,
            AuthoritySystemJournalPhase.PROVIDER_RETURNED,
            AuthoritySystemJournalPhase.OBSERVED,
            AuthoritySystemJournalPhase.TERMINAL,
        }:
            return False
        await self._anchor(
            peer_incarnation,
            request,
            resolved,
            journal,
            state,
            AuthoritySystemJournalPhase.ADMITTED,
        )
        await self._anchor(
            peer_incarnation,
            request,
            resolved,
            journal,
            state,
            AuthoritySystemJournalPhase.MUTATION_STARTED,
        )
        return True

    async def _provider_facts(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        resolved: ResolvedAuthoritySystemOperation,
        *,
        execute: bool,
    ) -> AuthoritySystemProvisionFacts | AuthoritySystemAbsenceFacts:
        if request.operation is AuthoritySystemOperation.PROVISION:
            method = (
                self._provider.execute_system_provision
                if execute
                else self._provider.observe_system_provision
            )
            facts = await method(request, context, resolved.snapshot)
            return AuthoritySystemProvisionFacts.model_validate(
                facts.model_dump(mode="python", by_alias=True)
            )
        method = (
            self._provider.execute_preactivation_teardown
            if execute
            else self._provider.observe_preactivation_teardown
        )
        facts = await method(request, context)
        return AuthoritySystemAbsenceFacts.model_validate(
            facts.model_dump(mode="python", by_alias=True)
        )

    @staticmethod
    def _proof(
        request: AuthoritySystemMutationRequestV1,
        facts: AuthoritySystemProvisionFacts | AuthoritySystemAbsenceFacts,
    ) -> AuthoritySystemProofV1:
        binding = request.model_dump(mode="python", by_alias=True, exclude={"schema_"})
        if isinstance(facts, AuthoritySystemProvisionFacts) and facts.complete:
            return AuthoritySystemProvisionReadyV1.model_validate(
                {**binding, **facts.model_dump(mode="python"), "disposition": "provision-ready"}
            )
        if isinstance(facts, AuthoritySystemAbsenceFacts) and facts.complete:
            return AuthoritySystemPreactivationAbsentV1.model_validate(
                {
                    **binding,
                    **facts.model_dump(mode="python"),
                    "disposition": "preactivation-absent",
                }
            )
        return AuthoritySystemRetainedQuarantineV1.model_validate(
            {
                **binding,
                "disposition": "retained-quarantine",
                "observation_digest": system_authority_digest(facts),
            }
        )

    async def _anchor(
        self,
        peer_incarnation: str,
        request: AuthoritySystemTakeoverRequestV1 | AuthoritySystemMutationRequestV1,
        resolved: ResolvedAuthoritySystemOperation,
        journal: FileAuthoritySystemJournal,
        state: _JournalState,
        phase: AuthoritySystemJournalPhase,
        *,
        observation: AuthoritySystemObservationV1 | None = None,
        outcome: str | None = None,
        receipt: AuthoritySystemProofV1 | None = None,
    ) -> None:
        previous_digest = state.db_digest
        record = make_authority_system_record(
            request,
            sequence=state.db_sequence + 1,
            previous_digest=previous_digest,
            phase=phase,
            observation=observation,
            outcome=outcome,
        )
        if len(state.records) == state.db_sequence:
            journal.append(record)
            state.records.append(record)
        elif state.records[state.db_sequence] != record:
            raise AuthoritySystemServiceError("journal-conflict")
        else:
            record = state.records[state.db_sequence]
        result = await self._repository.advance_head(
            peer_incarnation,
            request,
            expected_sequence=state.db_sequence,
            expected_digest=state.db_digest,
            record=record,
            receipt=receipt,
        )
        if result.status != "advanced" or result.sequence != record.sequence:
            raise AuthoritySystemServiceError(
                "superseded" if result.status == "superseded" else "journal-conflict"
            )
        assert result.sequence is not None
        assert result.digest is not None
        state.db_sequence = result.sequence
        state.db_digest = result.digest
