"""Adversarial end-to-end authority fencing proofs (ADR-0584)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityTakeoverRequestV1,
    JournalPhase,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
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
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path / f"{system_id}.journal"),
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
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    for operation in ("release", "teardown"):
        mutation = _mutation(request).model_copy(
            update={
                "operation": operation,
                "operation_identity": f"{operation}-identity",
                "attempt_id": uuid4(),
            }
        )
        await service.execute_mutation(peer, mutation)
    assert adapter.calls == ["commit:release", "observe", "commit:teardown", "observe"]


@pytest.mark.anyio
async def test_recovery_ownership_rejects_unrelated_peer_without_provider_access(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.fail_commit = True
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, _mutation(request))
    successor = _successor(request)
    repository.allocating_request = successor
    before = tuple(repository.records)
    calls = tuple(adapter.calls)
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.acknowledge_takeover(AuthenticatedPeer(uuid4()), successor)
    assert tuple(repository.records) == before
    assert tuple(adapter.calls) == calls


@pytest.mark.anyio
async def test_readiness_requires_exact_recovered_continuity(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    assert await service.readiness(peer, request)
    assert repository.head is not None
    exact = repository.head
    repository.head = replace(exact, digest="sha256:" + "f" * 64)
    assert not await service.readiness(peer, request)
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
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path / f"{system_id}.journal"),
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
