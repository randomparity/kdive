"""Serialized external-boot authority service tests (ADR-0584)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from kdive.db.external_boot_authority_journal import AuthorityBinding, JournalHead
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
    AuthorityServiceError,
    AuthorityServiceMetrics,
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
        self.head: JournalHead | None = None
        self.records: list[JournalRecordV1] = []
        self.advance_status: Literal["advanced", "superseded", "conflict"] = "advanced"

    async def resolve_allocating(
        self, peer: AuthenticatedPeer, request: AuthorityTakeoverRequestV1
    ) -> AuthorityBinding | None:
        if peer != self.peer or request != self.request or self.current:
            return None
        return _binding(peer, request, "allocating")

    async def resolve_current(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> AuthorityBinding | None:
        if (
            peer != self.peer
            or not self.current
            or request.authority_id != self.request.authority_id
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
        if self.advance_status != "advanced":
            return self.advance_status
        self.records.append(record)
        self.head = JournalHead(
            authority_instance=binding.authority_instance,
            system_id=binding.system_id,
            sequence=record.sequence,
            digest=record_digest(record),
            phase=record.phase,
            authority_id=record.authority_id,
            generation=record.generation,
            operation_identity=record.operation_identity,
            pending_takeover=None,
            suspended_operation=None,
        )
        return "advanced"


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def commit(
        self, request: AuthorityMutationRequestV1, commit_point: str
    ) -> AuthorityObservationV1:
        self.calls.append(f"commit:{commit_point}")
        self.entered.set()
        await self.release.wait()
        return self._observation("target")

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        self.calls.append("observe")
        return self._observation("target")

    @staticmethod
    def _observation(
        category: Literal["source", "target", "mixed", "unreadable", "conflict"],
    ) -> AuthorityObservationV1:
        return AuthorityObservationV1(
            observation_id=uuid4(), category=category, composite_state=_DIGEST_A
        )


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


@pytest.mark.anyio
async def test_takeover_anchors_without_provider_access(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    acknowledgement = await service.acknowledge_takeover(peer, request)
    assert [record.phase for record in repository.records] == [
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    ]
    assert acknowledgement.journal_sequence == 2
    assert acknowledgement.journal_digest == record_digest(repository.records[-1])
    assert adapter.calls == []


@pytest.mark.anyio
async def test_rejections_precede_journal_and_provider_access(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    with pytest.raises(AuthorityServiceError, match="unauthenticated"):
        await service.acknowledge_takeover(None, request)
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.acknowledge_takeover(AuthenticatedPeer(uuid4()), request)
    assert repository.records == []
    assert adapter.calls == []


@pytest.mark.anyio
async def test_mutation_requires_promotion_and_anchors_before_provider(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    mutation = _mutation(request)
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_mutation(peer, mutation)
    assert adapter.calls == []
    repository.current = True
    observation = await service.execute_mutation(peer, mutation)
    assert observation.category == "target"
    assert [record.phase for record in repository.records] == [
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
        JournalPhase.ADMITTED,
        JournalPhase.MUTATION_STARTED,
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]
    assert adapter.calls == ["commit:activate", "observe"]


@pytest.mark.anyio
async def test_caller_cancellation_does_not_cancel_started_lane(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.release.clear()
    task = asyncio.create_task(service.execute_mutation(peer, _mutation(request)))
    await adapter.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    adapter.release.set()
    for _ in range(20):
        if repository.records[-1].phase is JournalPhase.TERMINAL:
            break
        await asyncio.sleep(0)
    assert repository.records[-1].phase is JournalPhase.TERMINAL


@pytest.mark.anyio
async def test_readiness_requires_exact_local_and_trusted_head(tmp_path: Path) -> None:
    service, repository, _, peer, request = _service(tmp_path)
    assert await service.readiness(peer, request)
    await service.acknowledge_takeover(peer, request)
    assert await service.readiness(peer, request)
    path = tmp_path / f"{request.system_id}.journal"
    path.write_bytes(path.read_bytes() + b"corrupt")
    assert not await service.readiness(peer, request)
    assert service.metrics.recovery_failures == {
        (request.provider_kind, request.authority_instance): 1
    }


@pytest.mark.anyio
async def test_failed_checkpoint_never_reaches_provider_and_fails_closed(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    repository.advance_status = "conflict"
    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await service.acknowledge_takeover(peer, request)
    assert adapter.calls == []
    assert repository.head is None
    assert not await service.readiness(peer, request)
    assert service.metrics.checkpoints == {}


def test_metric_labels_are_bounded_non_tenant_dimensions() -> None:
    metrics = AuthorityServiceMetrics.empty()
    request = _takeover()
    metrics.reject(request, "superseded")
    assert set(metrics.rejections) == {
        (request.provider_kind, request.authority_instance, "superseded")
    }
