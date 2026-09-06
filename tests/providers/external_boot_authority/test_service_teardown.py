"""Full teardown traverses authentication, anchoring and host-owned IO (ADR-0620)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCommitContextV1,
    AuthorityMutationRequestV1,
    AuthorityOperation,
    AuthorityTeardownMutationRequestV1,
    JournalPhase,
    record_digest,
    teardown_proof_digest,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
    ExternalBootAuthorityService,
)
from kdive.providers.external_boot_authority.teardown import (
    AuthoritySystemTeardownFacts,
    AuthorityTeardownReservationV1,
    AuthorityTeardownSnapshot,
)
from kdive.providers.ports.external_boot import OpaqueProviderRef
from tests.providers.external_boot_authority.service_support import _Adapter, _Repository, _takeover


class _TeardownRepository(_Repository):
    deny_snapshot = False

    async def resolve_current_teardown(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityTeardownMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> AuthorityTeardownSnapshot | None:
        binding = await self.resolve_current(
            peer,
            cast(AuthorityMutationRequestV1, request),
            acknowledgement_sequence,
            acknowledgement_digest,
        )
        if binding is None or self.deny_snapshot:
            return None
        return AuthorityTeardownSnapshot(
            binding=binding,
            reservation=AuthorityTeardownReservationV1(
                disposition="ready",
                store_identity=OpaqueProviderRef(ref="store-a"),
                owner_key=OpaqueProviderRef(ref="owner-a"),
                reserved_bytes=4096,
            ),
            release_identity=None,
            release_evidence=None,
        )


class _TeardownAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.facts = AuthoritySystemTeardownFacts(
            intent_identity="sha256:" + "a" * 64,
            domain_absent=True,
            overlay_absent=True,
            baseline_absent=True,
            recovery_absent=True,
            quarantine_retained=False,
            completed_at=datetime(2026, 9, 6, tzinfo=UTC),
            reservation=AuthorityTeardownReservationV1(
                disposition="ready",
                store_identity=OpaqueProviderRef(ref="store-a"),
                owner_key=OpaqueProviderRef(ref="owner-a"),
                reserved_bytes=4096,
            ),
        )
        self.context: AuthorityCommitContextV1 | None = None
        self.commit_count = 0
        self.read_count = 0
        self.lose_completion_reply = False

    async def execute_system_teardown(
        self,
        request: AuthorityTeardownMutationRequestV1,
        context: AuthorityCommitContextV1,
        reservation: AuthorityTeardownReservationV1,
    ) -> AuthoritySystemTeardownFacts:
        self.context = context
        assert reservation == self.facts.reservation
        self.commit_count += 1
        self.entered.set()
        await self.release.wait()
        if self.lose_completion_reply:
            raise OSError("injected lost host completion reply")
        return self.facts

    async def observe_system_teardown(
        self, request: AuthorityTeardownMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthoritySystemTeardownFacts:
        assert context == self.context
        self.read_count += 1
        return self.facts


async def _ready(tmp_path: Path):
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover().model_copy(
        update={"purpose": "teardown", "operation": AuthorityOperation.TEARDOWN}
    )
    repository = _TeardownRepository(peer, takeover)
    adapter = _TeardownAdapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        adapter=adapter,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    values = takeover.model_dump(mode="json", by_alias=True)
    values.update(schema="external-boot-authority-teardown-request-v1", attempt_id=str(uuid4()))
    request = AuthorityTeardownMutationRequestV1.model_validate(values)
    return service, repository, adapter, peer, request


@pytest.mark.anyio
async def test_teardown_proof_is_anchored_and_exact_retry_does_not_delete_twice(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = await _ready(tmp_path)
    response = await service.execute_teardown(peer, request)
    assert response.proof.disposition == "complete_ready"
    assert response.observation.composite_state == teardown_proof_digest(response.proof)
    assert response.journal_digest == record_digest(repository.records[-1])
    assert repository.records[-1].outcome == "absent"
    assert [record.phase for record in repository.records[2:]] == [
        JournalPhase.ADMITTED,
        JournalPhase.MUTATION_STARTED,
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]
    assert adapter.context == AuthorityCommitContextV1.for_record(repository.records[3])
    assert adapter.calls == []  # Full System teardown never uses legacy commit/observe/finalize.
    assert await service.execute_teardown(peer, request) == response
    assert adapter.commit_count == 1
    await service.close()


@pytest.mark.anyio
async def test_teardown_snapshot_denial_prevents_every_provider_access(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = await _ready(tmp_path)
    repository.deny_snapshot = True
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_teardown(peer, request)
    assert adapter.commit_count == adapter.read_count == 0
    assert len(repository.records) == 2
    await service.close()


@pytest.mark.anyio
async def test_teardown_quarantine_is_anchored_without_absence_or_capacity_proof(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = await _ready(tmp_path)
    adapter.facts = adapter.facts.model_copy(
        update={"recovery_absent": False, "quarantine_retained": True}
    )
    response = await service.execute_teardown(peer, request)
    assert response.proof.disposition == "retained_quarantine"
    assert response.observation.category == repository.records[-1].outcome == "conflict"
    await service.close()


@pytest.mark.anyio
async def test_cancelled_teardown_caller_does_not_release_underlying_operation(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = await _ready(tmp_path)
    adapter.release.clear()
    caller = asyncio.create_task(service.execute_teardown(peer, request))
    await adapter.entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not closing.done()
    adapter.release.set()
    await closing
    assert adapter.commit_count == 1
    assert repository.records[-1].phase is JournalPhase.TERMINAL


@pytest.mark.anyio
async def test_restart_recovers_completed_teardown_without_repeating_mutation(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = await _ready(tmp_path)
    adapter.lose_completion_reply = True
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_teardown(peer, request)
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED
    await service.close()
    restarted = ExternalBootAuthorityService(
        repository=repository,
        adapter=adapter,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
    )
    # The first retry settles the interrupted journal from the provider's durable receipt.
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await restarted.execute_teardown(peer, request)
    assert repository.records[-1].outcome == "absent"
    response = await restarted.execute_teardown(peer, request)
    assert response.proof.disposition == "complete_ready"
    assert response.proof.release_evidence.verified_at == adapter.facts.completed_at
    assert adapter.commit_count == 1
    await restarted.close()


@pytest.mark.anyio
async def test_malformed_host_facts_cannot_become_a_terminal_proof(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = await _ready(tmp_path)
    adapter.facts = adapter.facts.model_copy(update={"domain_absent": "true"})
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_teardown(peer, request)
    assert repository.records[-1].phase is JournalPhase.PROVIDER_RETURNED
    assert not any(record.phase is JournalPhase.TERMINAL for record in repository.records)
    await service.close()
