"""Serialized external-boot authority service tests (ADR-0584)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

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
                activation_id=prior.activation_id,
                operation_identity=prior.operation_identity,
                attempt_id=prior.attempt_id,
                purpose=prior.purpose,
                operation=prior.operation or "",
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
    labels = (request.provider_kind, request.authority_instance)
    assert service.metrics.checkpoints[labels] == 2
    assert service.metrics.checkpoint_latency[labels][0] == 2
    assert service.metrics.checkpoint_latency[labels][1] >= 0


@pytest.mark.anyio
async def test_rejections_precede_journal_and_provider_access(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    with pytest.raises(AuthorityServiceError, match="unauthenticated"):
        await service.acknowledge_takeover(None, request)
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.acknowledge_takeover(AuthenticatedPeer(uuid4()), request)
    assert repository.records == []
    assert adapter.calls == []
    assert service.metrics.rejections == {
        (request.provider_kind, request.authority_instance, "unauthenticated"): 1,
        (request.provider_kind, request.authority_instance, "superseded"): 1,
    }


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
    assert service.metrics.rejections == {
        (request.provider_kind, request.authority_instance, "journal_conflict"): 1
    }


@pytest.mark.anyio
async def test_failed_local_append_never_advances_checkpoint_or_provider(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    failing = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: _FailingAppendJournal(tmp_path / f"{system_id}.journal"),
        adapter=adapter,
    )
    with pytest.raises(OSError, match="append/fsync"):
        await failing.acknowledge_takeover(peer, request)
    assert repository.records == []
    assert repository.head is None
    assert adapter.calls == []


def test_metric_labels_are_bounded_non_tenant_dimensions() -> None:
    metrics = AuthorityServiceMetrics.empty()
    request = _takeover()
    metrics.reject(request, "superseded")
    assert set(metrics.rejections) == {
        (request.provider_kind, request.authority_instance, "superseded")
    }


@pytest.mark.anyio
async def test_takeover_terminalizes_admitted_operation_without_provider_access(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    repository.pause_phase = JournalPhase.ADMITTED
    repository.phase_release.clear()
    mutation_task = asyncio.create_task(service.execute_mutation(peer, _mutation(request)))
    await repository.phase_entered.wait()
    successor = request.model_copy(
        update={
            "authority_id": uuid4(),
            "generation": 2,
            "operation_identity": "takeover-b",
        }
    )
    repository.allocating_request = successor
    takeover_task = asyncio.create_task(service.acknowledge_takeover(peer, successor))
    repository.phase_release.set()
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await mutation_task
    acknowledgement = await takeover_task
    assert acknowledgement.generation == 2
    assert adapter.calls == []
    assert [record.phase for record in repository.records[-4:]] == [
        JournalPhase.ADMITTED,
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TERMINAL,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    ]


@pytest.mark.anyio
async def test_simultaneous_mutations_admit_only_one_before_any_second_append(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    repository.pause_phase = JournalPhase.ADMITTED
    repository.phase_release.clear()
    first = asyncio.create_task(service.execute_mutation(peer, _mutation(request)))
    await repository.phase_entered.wait()
    second_request = _mutation(request).model_copy(
        update={"operation_identity": "mutation-b", "attempt_id": uuid4()}
    )
    second = asyncio.create_task(service.execute_mutation(peer, second_request))
    await asyncio.sleep(0)
    assert sum(record.phase is JournalPhase.ADMITTED for record in repository.records) == 0
    repository.pause_phase = None
    repository.phase_release.set()
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await second
    assert (await first).category == "target"
    assert sum(record.phase is JournalPhase.ADMITTED for record in repository.records) == 1
    journal = FileAuthorityJournal(tmp_path / f"{request.system_id}.journal")
    assert len(journal.load()) == len(repository.records)
    assert adapter.calls == ["commit:activate", "observe"]


@pytest.mark.anyio
async def test_takeover_waits_for_started_operation_positive_observation(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.release.clear()
    mutation_task = asyncio.create_task(service.execute_mutation(peer, _mutation(request)))
    await adapter.entered.wait()
    successor = request.model_copy(
        update={
            "authority_id": uuid4(),
            "generation": 2,
            "operation_identity": "takeover-b",
        }
    )
    repository.allocating_request = successor
    takeover_task = asyncio.create_task(service.acknowledge_takeover(peer, successor))
    await asyncio.sleep(0)
    assert not takeover_task.done()
    adapter.release.set()
    assert (await mutation_task).category == "target"
    assert (await takeover_task).generation == 2
    assert [record.phase for record in repository.records[-5:]] == [
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    ]


@pytest.mark.anyio
async def test_newer_takeover_supersedes_unacknowledged_watermark(tmp_path: Path) -> None:
    service, repository, adapter, peer, first = _service(tmp_path)
    repository.pause_phase = JournalPhase.WATERMARK_INSTALLED
    repository.phase_release.clear()
    first_task = asyncio.create_task(service.acknowledge_takeover(peer, first))
    await repository.phase_entered.wait()
    successor = first.model_copy(
        update={
            "authority_id": uuid4(),
            "generation": 2,
            "operation_identity": "takeover-b",
        }
    )
    repository.allocating_request = successor
    repository.pause_phase = None
    successor_task = asyncio.create_task(service.acknowledge_takeover(peer, successor))
    repository.phase_release.set()
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await first_task
    assert (await successor_task).generation == 2
    assert [record.phase for record in repository.records] == [
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_SUPERSEDED,
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    ]
    assert adapter.calls == []
    path = tmp_path / f"{first.system_id}.journal"
    lines = path.read_bytes().splitlines(keepends=True)
    assert repository.head is not None
    for index, record in enumerate(repository.records, start=1):
        path.write_bytes(b"".join(lines[:index]))
        repository.head = replace(
            repository.head,
            sequence=record.sequence,
            digest=record_digest(record),
            phase=record.phase,
            authority_id=record.authority_id,
            generation=record.generation,
            operation_identity=record.operation_identity,
        )
        restarted = ExternalBootAuthorityService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthorityJournal(path),
            adapter=adapter,
        )
        assert await restarted.readiness(peer, successor)


@pytest.mark.anyio
async def test_completed_mutation_can_transition_to_later_takeover(tmp_path: Path) -> None:
    service, repository, _, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    await service.execute_mutation(peer, _mutation(request))
    successor = request.model_copy(
        update={
            "authority_id": uuid4(),
            "generation": 2,
            "operation_identity": "takeover-b",
        }
    )
    repository.allocating_request = successor
    acknowledgement = await service.acknowledge_takeover(peer, successor)
    assert acknowledgement.generation == 2
    assert [record.phase for record in repository.records[-3:]] == [
        JournalPhase.TERMINAL,
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    ]


@pytest.mark.anyio
async def test_each_commit_rechecks_current_binding_and_fences_loss(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    repository.reject_resolution = 2
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_mutation(peer, _mutation(request))
    assert repository.current_resolutions == 2
    assert adapter.calls == []
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
async def test_sequential_commit_operations_each_receive_a_fresh_fence(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    first = _mutation(request)
    second = first.model_copy(
        update={
            "operation_identity": "mutation-b",
            "operation_digest": _DIGEST_B,
            "attempt_id": uuid4(),
        }
    )
    await service.execute_mutation(peer, first)
    await service.execute_mutation(peer, second)
    assert repository.current_resolutions == 4
    assert adapter.calls == ["commit:activate", "observe"] * 2


@pytest.mark.anyio
async def test_restart_accepts_every_exact_phase_and_rejects_head_divergence(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    await service.execute_mutation(peer, _mutation(request))
    records = tuple(repository.records)
    path = tmp_path / f"{request.system_id}.journal"
    lines = path.read_bytes().splitlines(keepends=True)
    assert {record.phase for record in records} >= {
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
        JournalPhase.ADMITTED,
        JournalPhase.MUTATION_STARTED,
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    }
    repository.current = False
    assert repository.head is not None
    for index, record in enumerate(records, start=1):
        path.write_bytes(b"".join(lines[:index]))
        repository.head = replace(
            repository.head,
            sequence=record.sequence,
            digest=record_digest(record),
            phase=record.phase,
            authority_id=record.authority_id,
            generation=record.generation,
            operation_identity=record.operation_identity,
        )
        restarted = ExternalBootAuthorityService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthorityJournal(path),
            adapter=adapter,
        )
        expected_ready = record.phase not in {
            JournalPhase.ADMITTED,
            JournalPhase.MUTATION_STARTED,
            JournalPhase.PROVIDER_RETURNED,
            JournalPhase.OBSERVED,
        }
        assert await restarted.readiness(peer, request) is expected_ready
    last = records[-1]
    repository.head = replace(repository.head, sequence=last.sequence + 1)
    assert not await service.readiness(peer, request)
    repository.head = replace(
        repository.head,
        sequence=last.sequence,
        digest=record_digest(last),
    )
    path.write_bytes(b"".join(lines[:-1]))
    assert not await service.readiness(peer, request)
    path.write_bytes(b"".join(lines) + lines[-1])
    assert not await service.readiness(peer, request)
    path.write_bytes(b"".join(lines) + b"corrupt")
    assert not await service.readiness(peer, request)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "last_phase"),
    [
        ("commit", JournalPhase.MUTATION_STARTED),
        ("observe", JournalPhase.PROVIDER_RETURNED),
    ],
)
async def test_provider_boundary_failure_remains_unresolved_across_restart(
    tmp_path: Path, failure: str, last_phase: JournalPhase
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    setattr(adapter, f"fail_{failure}", True)
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, _mutation(request))
    assert repository.records[-1].phase is last_phase
    successor = request.model_copy(
        update={
            "authority_id": uuid4(),
            "generation": 2,
            "operation_identity": "takeover-b",
        }
    )
    repository.allocating_request = successor
    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path / f"{system_id}.journal"),
        adapter=adapter,
    )
    if failure == "observe":
        with pytest.raises(AuthorityServiceError, match="provider_conflict"):
            await restarted.acknowledge_takeover(peer, successor)
        adapter.fail_observe = False
        restarted = ExternalBootAuthorityService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthorityJournal(
                tmp_path / f"{system_id}.journal"
            ),
            adapter=adapter,
        )
    acknowledgement = await restarted.acknowledge_takeover(peer, successor)
    assert acknowledgement.generation == 2
    assert repository.records[-1].phase is JournalPhase.TAKEOVER_ACKNOWLEDGED


@pytest.mark.anyio
async def test_worker_death_recovers_every_suspended_phase_before_ack(tmp_path: Path) -> None:
    source, source_repository, _, peer, request = _service(tmp_path / "source")
    (tmp_path / "source").mkdir()
    await source.acknowledge_takeover(peer, request)
    source_repository.current = True
    mutation = _mutation(request).model_copy(update={"operation": "commit-boot"})
    await source.execute_mutation(peer, mutation)
    source_records = tuple(source_repository.records)
    source_lines = (
        (tmp_path / "source" / f"{request.system_id}.journal")
        .read_bytes()
        .splitlines(keepends=True)
    )
    successor = request.model_copy(
        update={
            "authority_id": uuid4(),
            "generation": 2,
            "operation_identity": "takeover-b",
        }
    )
    for phase, expected_calls in (
        (JournalPhase.ADMITTED, []),
        (JournalPhase.MUTATION_STARTED, ["observe"]),
        (JournalPhase.PROVIDER_RETURNED, ["observe"]),
        (JournalPhase.OBSERVED, []),
    ):
        index = next(i for i, record in enumerate(source_records) if record.phase is phase)
        phase_dir = tmp_path / phase
        phase_dir.mkdir()
        path = phase_dir / f"{request.system_id}.journal"
        path.write_bytes(b"".join(source_lines[: index + 1]))
        repository = _Repository(peer, request)
        repository.allocating_request = successor
        repository.records = list(source_records[: index + 1])
        record = source_records[index]
        assert source_repository.head is not None
        repository.head = replace(
            source_repository.head,
            sequence=record.sequence,
            digest=record_digest(record),
            phase=record.phase,
            authority_id=record.authority_id,
            generation=record.generation,
            operation_identity=record.operation_identity,
        )
        adapter = _Adapter()
        restarted = ExternalBootAuthorityService(
            repository=repository,
            journal_factory=lambda system_id, path=path: FileAuthorityJournal(path),
            adapter=adapter,
        )
        acknowledgement = await restarted.acknowledge_takeover(peer, successor)
        assert acknowledgement.generation == 2
        assert adapter.calls == expected_calls
        assert adapter.operations == (["commit-boot"] if expected_calls else [])
        assert repository.records[-1].phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
        terminal = next(
            record
            for record in reversed(repository.records)
            if record.phase is JournalPhase.TERMINAL
        )
        assert terminal.outcome == ("never-began" if phase is JournalPhase.ADMITTED else "target")


@pytest.mark.anyio
@pytest.mark.parametrize("divergence", ["missing", "operation", "phase"])
async def test_restart_rejects_divergent_trusted_continuation_before_recovery_access(
    tmp_path: Path, divergence: str
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.fail_commit = True
    mutation = _mutation(request).model_copy(update={"operation": "commit-boot"})
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, mutation)
    successor = request.model_copy(
        update={"authority_id": uuid4(), "generation": 2, "operation_identity": "takeover-b"}
    )
    repository.allocating_request = successor
    adapter.fail_commit = False
    adapter.fail_observe = True
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.acknowledge_takeover(peer, successor)
    assert repository.head is not None
    suspended = repository.head.suspended_operation
    assert suspended is not None
    if divergence == "missing":
        corrupted = None
    elif divergence == "operation":
        corrupted = replace(suspended, operation="different-commit")
    else:
        corrupted = replace(suspended, phase=JournalPhase.PROVIDER_RETURNED)
    repository.head = replace(repository.head, suspended_operation=corrupted)
    path = tmp_path / f"{request.system_id}.journal"
    before = path.read_bytes()
    fresh_adapter = _Adapter()
    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(path),
        adapter=fresh_adapter,
    )
    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await restarted.acknowledge_takeover(peer, successor)
    assert fresh_adapter.calls == []
    assert path.read_bytes() == before
