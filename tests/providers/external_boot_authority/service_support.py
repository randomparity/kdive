"""Shared controllable external-boot authority service proofs (ADR-0584)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from kdive.db.external_boot_authority_journal import (
    AuthorityBinding,
    JournalHead,
    PendingTakeover,
    SuspendedOperation,
)
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
    JournalPhase,
    JournalRecordV1,
    record_digest,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    ExternalBootAuthorityService,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _takeover() -> AuthorityTakeoverRequestV1:
    return AuthorityTakeoverRequestV1(
        authority_id=uuid4(),
        generation=1,
        system_id=uuid4(),
        activation_id=uuid4(),
        run_id=uuid4(),
        plan_identity=_DIGEST_A,
        purpose="activate",
        provider_kind="local-libvirt",
        authority_instance="host-a",
        operation_identity="takeover-a",
        operation_digest=_DIGEST_B,
    )


def _mutation(request: AuthorityTakeoverRequestV1) -> AuthorityMutationRequestV1:
    values = request.model_dump(mode="json", by_alias=True)
    values |= {
        "operation": "activate",
        "operation_identity": "mutation-a",
        "operation_digest": _DIGEST_A,
        "attempt_id": str(uuid4()),
        "expected_source_identity": "source-a",
        "intended_target_identity": "target-a",
        "recovery_objects": [],
    }
    return AuthorityMutationRequestV1.model_validate(values)


def _binding(
    peer: AuthenticatedPeer,
    request: AuthorityTakeoverRequestV1 | AuthorityMutationRequestV1,
    state: Literal["allocating", "current"],
) -> AuthorityBinding:
    return AuthorityBinding(
        peer_incarnation_id=str(peer.incarnation_id),
        authority_id=request.authority_id,
        generation=request.generation,
        system_id=request.system_id,
        activation_id=request.activation_id,
        run_id=request.run_id,
        plan_identity=request.plan_identity,
        purpose=request.purpose,
        provider_kind=request.provider_kind,
        authority_instance=request.authority_instance,
        operation_identity=request.operation_identity,
        operation_digest=request.operation_digest,
        state=state,
    )


class _Repository:
    def __init__(self, peer: AuthenticatedPeer, request: AuthorityTakeoverRequestV1) -> None:
        self.peer = peer
        self.request = request
        self.current = False
        self.allocating_request = request
        self.head: JournalHead | None = None
        self.records: list[JournalRecordV1] = []
        self.advance_status: Literal["advanced", "superseded", "conflict"] = "advanced"
        self.pause_phase: JournalPhase | None = None
        self.phase_entered = asyncio.Event()
        self.phase_release = asyncio.Event()
        self.phase_release.set()
        self.current_resolutions = 0
        self.reject_resolution: int | None = None

    async def resolve_allocating(
        self, peer: AuthenticatedPeer, request: AuthorityTakeoverRequestV1
    ) -> AuthorityBinding | None:
        if peer != self.peer or request != self.allocating_request:
            return None
        if request == self.request and self.current:
            return None
        return _binding(peer, request, "allocating")

    async def resolve_current(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> AuthorityBinding | None:
        self.current_resolutions += 1
        if self.current_resolutions == self.reject_resolution:
            return None
        if (
            peer != self.peer
            or not self.current
            or request.authority_id != self.request.authority_id
            or request.generation != self.request.generation
            or request.system_id != self.request.system_id
            or request.activation_id != self.request.activation_id
            or request.run_id != self.request.run_id
            or request.purpose != self.request.purpose
            or request.provider_kind != self.request.provider_kind
            or request.authority_instance != self.request.authority_instance
        ):
            return None
        acknowledgement = next(
            (
                record
                for record in self.records
                if record.phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
            ),
            None,
        )
        if (
            acknowledgement is None
            or acknowledgement.sequence != acknowledgement_sequence
            or record_digest(acknowledgement) != acknowledgement_digest
        ):
            return None
        return _binding(peer, request, "current")

    async def read_head(self, binding: AuthorityBinding) -> JournalHead | None:
        return self.head

    async def advance(
        self,
        binding: AuthorityBinding,
        expected_sequence: int,
        expected_digest: str,
        record: JournalRecordV1,
    ) -> Literal["advanced", "superseded", "conflict"]:
        if record.phase is self.pause_phase:
            self.phase_entered.set()
            await self.phase_release.wait()
        if self.advance_status != "advanced":
            return self.advance_status
        prior = self.records[-1] if self.records else None
        self.records.append(record)
        pending = self.head.pending_takeover if self.head is not None else None
        suspended = self.head.suspended_operation if self.head is not None else None
        if record.phase in {JournalPhase.WATERMARK_INSTALLED, JournalPhase.TAKEOVER_SUPERSEDED}:
            pending = PendingTakeover(
                authority_id=record.authority_id,
                generation=record.generation,
                operation_identity=record.operation_identity,
                attempt_id=record.attempt_id,
                request_digest=record.operation_digest,
                watermark_sequence=record.sequence,
                watermark_digest=record_digest(record),
            )
        if (
            record.phase is JournalPhase.WATERMARK_INSTALLED
            and prior is not None
            and prior.phase
            in {
                JournalPhase.ADMITTED,
                JournalPhase.MUTATION_STARTED,
                JournalPhase.PROVIDER_RETURNED,
                JournalPhase.OBSERVED,
            }
        ):
            suspended = SuspendedOperation(
                authority_id=prior.authority_id,
                generation=prior.generation,
                system_id=prior.system_id,
                activation_id=prior.activation_id,
                run_id=prior.run_id,
                plan_identity=prior.plan_identity,
                operation_identity=prior.operation_identity,
                attempt_id=prior.attempt_id,
                purpose=prior.purpose,
                operation=prior.operation or "",
                provider_kind=prior.provider_kind,
                authority_instance=prior.authority_instance,
                request_digest=prior.operation_digest,
                phase=prior.phase.value,
                source_identity=prior.expected_source_identity or "",
                target_identity=prior.intended_target_identity or "",
                ownership_digest="sha256:"
                + hashlib.sha256(
                    json.dumps(
                        [item.model_dump(mode="json") for item in prior.recovery_objects],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            )
        if record.phase is JournalPhase.TERMINAL:
            suspended = None
        if record.phase is JournalPhase.TAKEOVER_ACKNOWLEDGED:
            pending = None
        self.head = JournalHead(
            authority_instance=binding.authority_instance,
            system_id=binding.system_id,
            sequence=record.sequence,
            digest=record_digest(record),
            phase=record.phase,
            authority_id=record.authority_id,
            generation=record.generation,
            operation_identity=record.operation_identity,
            pending_takeover=pending,
            suspended_operation=suspended,
        )
        return "advanced"


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.fail_commit = False
        self.fail_observe = False
        self.operations: list[str] = []

    async def commit(
        self, request: AuthorityMutationRequestV1, commit_point: str
    ) -> AuthorityObservationV1:
        self.calls.append(f"commit:{commit_point}")
        self.entered.set()
        await self.release.wait()
        if self.fail_commit:
            raise RuntimeError("bounded commit failure")
        return self._observation("target")

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        self.calls.append("observe")
        self.operations.append(request.operation)
        if self.fail_observe:
            raise RuntimeError("bounded observation failure")
        return self._observation("target")

    @staticmethod
    def _observation(
        category: Literal["source", "target", "mixed", "unreadable", "conflict"],
    ) -> AuthorityObservationV1:
        return AuthorityObservationV1(
            observation_id=uuid4(), category=category, composite_state=_DIGEST_A
        )


class _FailingAppendJournal(FileAuthorityJournal):
    def append(self, record: JournalRecordV1) -> None:
        raise OSError("injected append/fsync failure")


def _service(
    tmp_path: Path,
) -> tuple[
    ExternalBootAuthorityService,
    _Repository,
    _Adapter,
    AuthenticatedPeer,
    AuthorityTakeoverRequestV1,
]:
    peer = AuthenticatedPeer(uuid4())
    request = _takeover()
    repository = _Repository(peer, request)
    adapter = _Adapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path / f"{system_id}.journal"),
        adapter=adapter,
    )
    return service, repository, adapter, peer, request
