"""Serialized external-boot authority service tests (ADR-0584)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    JournalPhase,
    record_digest,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
    AuthorityServiceMetrics,
    ExternalBootAuthorityService,
)
from tests.providers.external_boot_authority.service_support import (
    _DIGEST_B,
    _Adapter,
    _FailingAppendJournal,
    _mutation,
    _Repository,
    _service,
    _takeover,
)


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
        ("untrusted", "unresolved", "unauthenticated"): 1,
        ("untrusted", "unresolved", "superseded"): 1,
    }


@pytest.mark.anyio
async def test_untrusted_cardinality_cannot_allocate_lanes_or_metric_series(
    tmp_path: Path,
) -> None:
    service, repository, adapter, _peer, request = _service(tmp_path)

    for index in range(100):
        hostile = request.model_copy(
            update={
                "system_id": uuid4(),
                "provider_kind": f"provider-{index}",
                "authority_instance": f"instance-{index}",
            }
        )
        with pytest.raises(AuthorityServiceError, match="unauthenticated"):
            await service.acknowledge_takeover(None, hostile)

    assert service._lanes == {}
    assert service.metrics.rejections == {("untrusted", "unresolved", "unauthenticated"): 100}
    assert repository.records == []
    assert adapter.calls == []
    assert list(tmp_path.iterdir()) == []


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
async def test_trusted_coordinate_overflow_bounds_every_metric_and_evicts_lanes(
    tmp_path: Path,
) -> None:
    _unused, repository, adapter, peer, template = _service(tmp_path)
    metrics = AuthorityServiceMetrics.empty(max_coordinates=2)
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path / f"{system_id}.journal"),
        adapter=adapter,
        metrics=metrics,
    )
    labels: list[tuple[str, str]] = []
    for index in range(5):
        request = template.model_copy(
            update={
                "authority_id": uuid4(),
                "system_id": uuid4(),
                "activation_id": uuid4(),
                "run_id": uuid4(),
                "operation_identity": f"takeover-{index}",
                "provider_kind": f"trusted-provider-{index}",
                "authority_instance": f"trusted-instance-{index}",
            }
        )
        repository.request = request
        repository.allocating_request = request
        repository.current = False
        repository.head = None
        repository.records = []
        await service.acknowledge_takeover(peer, request)
        metrics.reject(request, "superseded")
        metrics.recovery_failed(request)
        coordinate = (request.provider_kind, request.authority_instance)
        labels.append(coordinate)
        metrics.set_unresolved(coordinate, True)

    assert service._lanes == {}
    assert all(
        len(store) <= 3
        for store in (
            metrics.rejections,
            metrics.recovery_failures,
            metrics.unresolved,
            metrics.checkpoints,
            metrics.checkpoint_latency,
        )
    )
    assert ("overflow", "overflow") in metrics.checkpoints
    assert ("overflow", "overflow", "overflow") in metrics.rejections
    for coordinate in labels:
        metrics.set_unresolved(coordinate, False)
    assert metrics.unresolved == {}
    assert AuthorityServiceMetrics.empty().max_coordinates == 256


def test_registered_metric_coordinate_keeps_exact_labels() -> None:
    registered = ("configured-provider", "configured-instance")
    metrics = AuthorityServiceMetrics.empty(
        max_coordinates=1, registered_coordinates=frozenset({registered})
    )
    assert metrics.labels(registered) == registered
    assert metrics.labels(("another-provider", "another-instance")) == (
        "overflow",
        "overflow",
    )


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
    assert service.metrics.rejections == {
        (request.provider_kind, request.authority_instance, "superseded"): 1
    }


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
