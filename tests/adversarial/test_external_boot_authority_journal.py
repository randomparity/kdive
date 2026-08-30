"""Adversarial end-to-end authority fencing proofs (ADR-0584)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityTakeoverRequestV1,
    JournalPhase,
    RecoveryObjectBindingV1,
)
from kdive.providers.external_boot_authority.service import (
    AuthorityServiceError,
    ExternalBootAuthorityService,
)
from tests.providers.external_boot_authority.service_support import _mutation, _service


def _successor(
    request: AuthorityTakeoverRequestV1, *, run_id: object | None = None
) -> AuthorityTakeoverRequestV1:
    """Create the next independently identified authority generation."""
    changes = {
        "authority_id": uuid4(),
        "generation": 2,
        "operation_identity": "takeover-successor",
    }
    if run_id is not None:
        changes["run_id"] = run_id
    return request.model_copy(update=changes)


@pytest.mark.anyio
async def test_two_generation_race_fences_stale_retry_and_preserves_idempotency(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, first = _service(tmp_path)
    repository.pause_phase = JournalPhase.WATERMARK_INSTALLED
    repository.phase_release.clear()
    stale = asyncio.create_task(service.acknowledge_takeover(peer, first))
    await repository.phase_entered.wait()
    successor = _successor(first)
    repository.allocating_request = successor
    repository.pause_phase = None
    current = asyncio.create_task(service.acknowledge_takeover(peer, successor))
    repository.phase_release.set()

    with pytest.raises(AuthorityServiceError, match="superseded"):
        await stale
    acknowledgement = await current
    assert acknowledgement.generation == 2
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.acknowledge_takeover(peer, first)
    assert [record.phase for record in repository.records] == [
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_SUPERSEDED,
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    ]
    assert adapter.calls == []


@pytest.mark.anyio
async def test_later_run_cannot_replay_earlier_completed_run(tmp_path: Path) -> None:
    service, repository, adapter, peer, first = _service(tmp_path)
    await service.acknowledge_takeover(peer, first)
    repository.current = True
    completed = _mutation(first)
    await service.execute_mutation(peer, completed)
    successor = _successor(first, run_id=uuid4())
    repository.allocating_request = successor
    await service.acknowledge_takeover(peer, successor)
    repository.current = False

    before = tuple(repository.records)
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_mutation(peer, completed)
    assert tuple(repository.records) == before
    assert adapter.calls == ["commit:activate", "observe"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("boundary", "last_phase"),
    [
        ("commit", JournalPhase.MUTATION_STARTED),
        ("observe", JournalPhase.PROVIDER_RETURNED),
    ],
)
async def test_lost_provider_response_recovers_without_repeating_commit(
    tmp_path: Path, boundary: str, last_phase: JournalPhase
) -> None:
    service, repository, adapter, peer, first = _service(tmp_path)
    await service.acknowledge_takeover(peer, first)
    repository.current = True
    setattr(adapter, f"fail_{boundary}", True)
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, _mutation(first))
    assert repository.records[-1].phase is last_phase

    successor = _successor(first)
    repository.allocating_request = successor
    setattr(adapter, f"fail_{boundary}", False)
    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await restarted.acknowledge_takeover(peer, successor)
    assert repository.records[-1].phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
    assert adapter.calls.count("commit:activate") == 1


@pytest.mark.anyio
@pytest.mark.parametrize("category", ["source", "target", "mixed", "unreadable", "conflict"])
async def test_every_observation_classification_is_terminal_and_bounded(
    tmp_path: Path, category: str
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    observed = cast(Literal["source", "target", "mixed", "unreadable", "conflict"], category)
    adapter.__dict__["_observation"] = lambda _category: type(adapter)._observation(observed)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    observation = await service.execute_mutation(peer, _mutation(request))
    assert observation.category == category
    terminal = repository.records[-1]
    assert terminal.phase is JournalPhase.TERMINAL
    expected = category if category in {"source", "target", "conflict"} else "conflict"
    assert terminal.outcome == expected


@pytest.mark.anyio
async def test_release_and_teardown_are_independently_fenced(tmp_path: Path) -> None:
    for purpose in ("release", "teardown"):
        lane = tmp_path / purpose
        lane.mkdir()
        service, repository, adapter, peer, base = _service(lane)
        request = base.model_copy(
            update={
                "purpose": purpose,
                "operation": purpose,
                "operation_identity": f"takeover-{purpose}",
            }
        )
        repository.request = request
        repository.allocating_request = request
        await service.acknowledge_takeover(peer, request)
        repository.current = True
        mutation = _mutation(request).model_copy(
            update={
                "operation": purpose,
                "operation_identity": f"{purpose}-identity",
                "attempt_id": uuid4(),
            }
        )
        stale = mutation.model_copy(
            update={"purpose": "teardown" if purpose == "release" else "release"}
        )
        with pytest.raises(AuthorityServiceError, match="superseded"):
            await service.execute_mutation(peer, stale)
        assert adapter.calls == []
        await service.execute_mutation(peer, mutation)
        assert adapter.calls == [f"commit:{purpose}", "observe"]


@pytest.mark.anyio
@pytest.mark.parametrize("drift", ["system", "activation", "reference", "digest"])
async def test_recovery_ownership_rejects_drift_without_provider_or_journal_access(
    tmp_path: Path, drift: str, caplog: pytest.LogCaptureFixture
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    request = request.model_copy(
        update={
            "run_id": uuid4(),
            "plan_identity": "sha256:" + "e" * 64,
            "operation_identity": "tenant-operation-identity-do-not-log",
        }
    )
    repository.request = request
    repository.allocating_request = request
    adapter.provider_output = "provider-output-do-not-log"
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    objects = tuple(
        sorted(
            (
                RecoveryObjectBindingV1(
                    system_id=request.system_id,
                    activation_id=request.activation_id,
                    reference=reference,
                )
                for reference in ("object-a", "tenant-reference-do-not-log")
            ),
            key=lambda item: item.model_dump_json(),
        )
    )
    mutation = _mutation(request).model_copy(update={"recovery_objects": objects})
    adapter.fail_commit = True
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, mutation)
    successor = _successor(request)
    repository.allocating_request = successor
    adapter.fail_commit = False
    adapter.fail_observe = True
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.acknowledge_takeover(peer, successor)
    labels = (request.provider_kind, request.authority_instance)
    assert service.metrics.unresolved == {labels: 1}
    assert repository.head is not None and repository.head.suspended_operation is not None
    exact = repository.head.suspended_operation
    if drift in {"system", "activation"}:
        changes = {f"{drift}_id": uuid4()}
    elif drift == "reference":
        changed_objects = (
            *objects[:-1],
            objects[-1].model_copy(update={"reference": "changed-ref"}),
        )
        canonical = json.dumps(
            [item.model_dump(mode="json") for item in changed_objects],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        changes = {"ownership_digest": "sha256:" + hashlib.sha256(canonical).hexdigest()}
    else:
        changes = {"ownership_digest": "sha256:" + "d" * 64}
    repository.head = replace(repository.head, suspended_operation=replace(exact, **changes))
    path = tmp_path / f"{request.system_id}.journal"
    before = path.read_bytes()
    calls = tuple(adapter.calls)
    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(path.parent, path.name),
        adapter=adapter,
    )
    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await restarted.acknowledge_takeover(peer, successor)
    assert path.read_bytes() == before
    assert tuple(adapter.calls) == calls
    repository.head = replace(repository.head, suspended_operation=exact)
    adapter.fail_observe = False
    resumed = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(path.parent, path.name),
        adapter=adapter,
    )
    assert (await resumed.acknowledge_takeover(peer, successor)).generation == 2
    forbidden = {
        str(request.run_id),
        request.plan_identity,
        mutation.operation,
        objects[-1].reference,
        adapter.provider_output,
    }
    metric_surface = repr(
        (
            service.metrics.recovery_failures,
            service.metrics.unresolved,
            service.metrics.rejections,
            service.metrics.checkpoint_latency,
        )
    )
    log_surface = " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert all(value not in metric_surface for value in forbidden)
    assert all(value not in log_surface for value in forbidden)


def test_reversed_recovery_tuple_is_rejected_before_side_effects(tmp_path: Path) -> None:
    service, repository, adapter, _, request = _service(tmp_path)
    objects = tuple(
        RecoveryObjectBindingV1(
            system_id=request.system_id,
            activation_id=request.activation_id,
            reference=reference,
        )
        for reference in ("object-a", "object-b")
    )
    values = _mutation(request).model_dump(mode="json", by_alias=True)
    values["recovery_objects"] = [item.model_dump(mode="json") for item in reversed(objects)]
    with pytest.raises(ValidationError, match="sorted"):
        AuthorityMutationRequestV1.model_validate(values)
    assert repository.records == []
    assert repository.head is None
    assert adapter.calls == []
    assert not (tmp_path / f"{request.system_id}.journal").exists()


@pytest.mark.anyio
async def test_readiness_requires_exact_recovered_continuity(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    assert await service.readiness(peer, request)
    assert repository.head is not None
    exact = repository.head
    repository.head = replace(exact, digest="sha256:" + "f" * 64)
    assert not await service.readiness(peer, request)
    labels = (request.provider_kind, request.authority_instance)
    assert service.metrics.recovery_failures == {labels: 1}
    repository.head = exact
    repository.current = True
    adapter.fail_commit = True
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, _mutation(request))
    assert not await service.readiness(peer, request)
    successor = _successor(request)
    repository.allocating_request = successor
    adapter.fail_commit = False
    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await restarted.acknowledge_takeover(peer, successor)
    assert await restarted.readiness(peer, successor)


@pytest.mark.anyio
async def test_checkpoint_conflict_exposes_no_provider_output_in_telemetry(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    repository.advance_status = "conflict"
    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await service.acknowledge_takeover(peer, request)
    labels = (request.provider_kind, request.authority_instance)
    assert service.metrics.rejections == {(*labels, "journal_conflict"): 1}
    assert service.metrics.checkpoint_latency == {}
    assert service.metrics.unresolved == {}
    assert adapter.calls == []
    assert all(
        request.plan_identity not in label for key in service.metrics.rejections for label in key
    )
    success_dir = tmp_path / "success"
    success_dir.mkdir()
    successful, _, _, success_peer, success_request = _service(success_dir)
    await successful.acknowledge_takeover(success_peer, success_request)
    success_labels = (success_request.provider_kind, success_request.authority_instance)
    count, elapsed = successful.metrics.checkpoint_latency[success_labels]
    assert count == 2
    assert elapsed >= 0
    bounded_labels = [*labels, *success_labels, "journal_conflict"]
    assert all(len(label.encode()) <= 255 for label in bounded_labels)
    assert all("provider output" not in label for label in bounded_labels)


def test_production_composition_does_not_advertise_authority_v1() -> None:
    root = Path(__file__).parents[2] / "src" / "kdive"
    production = [
        root / "providers" / "local_libvirt" / "composition.py",
        root / "providers" / "remote_libvirt" / "composition.py",
        root / "mcp" / "assembly" / "app.py",
    ]
    for path in production:
        text = path.read_text()
        assert "ExternalBootAuthorityService" not in text
        assert "external_boot_authority_v1" not in text
