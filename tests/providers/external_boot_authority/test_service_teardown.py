"""Full teardown traverses authentication, anchoring and host-owned IO (ADR-0620)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.domain.external_boot_activation import ExternalBootReleaseEvidenceV1
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
    ProviderRecoveryRefusal,
    SystemTeardownSupersededError,
)
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteExternalBootAuthorityAdapter,
    RemoteExternalBootCoordinator,
    RemoteModuleVolumePreparationStore,
    RemoteSystemTeardownInspection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from tests.providers.external_boot_authority.service_support import _Adapter, _Repository, _takeover


class _TeardownRepository(_Repository):
    deny_snapshot = False
    released_after_provider = False

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
        reservation = AuthorityTeardownReservationV1(
            disposition="ready",
            store_identity=OpaqueProviderRef(ref="store-a"),
            owner_key=OpaqueProviderRef(ref="owner-a"),
            reserved_bytes=4096,
        )
        if not self.released_after_provider:
            return AuthorityTeardownSnapshot(
                binding=binding,
                reservation=reservation,
                release_identity=None,
                release_evidence=None,
            )
        release = ExternalBootReleaseEvidenceV1(
            activation_id=request.activation_id,
            system_id=request.system_id,
            store_identity=reservation.store_identity,
            owner_key=reservation.owner_key,
            reserved_bytes=reservation.reserved_bytes,
            objects=(),
            verified_at=datetime(2026, 9, 6, tzinfo=UTC),
        )
        return AuthorityTeardownSnapshot(
            binding=binding,
            reservation=AuthorityTeardownReservationV1(
                disposition="released",
                store_identity=reservation.store_identity,
                owner_key=reservation.owner_key,
                reserved_bytes=reservation.reserved_bytes,
            ),
            release_identity=release.identity,
            release_evidence=release,
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
        self.release_reservation_after_commit: Callable[[], None] | None = None
        self.failure: Exception | None = None

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
        if self.failure is not None:
            raise self.failure
        if self.lose_completion_reply:
            raise OSError("injected lost host completion reply")
        if self.release_reservation_after_commit is not None:
            self.release_reservation_after_commit()
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
async def test_cancelled_teardown_retries_after_restart_without_repeating_host_commit(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = await _ready(tmp_path)
    adapter.release.clear()
    caller = asyncio.create_task(service.execute_teardown(peer, request))
    await adapter.entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_teardown(peer, request)
    assert adapter.commit_count == 1

    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not closing.done()
    adapter.release.set()
    await closing

    restarted = ExternalBootAuthorityService(
        repository=repository,
        adapter=adapter,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
    )
    response = await restarted.execute_teardown(peer, request)
    assert response.proof.disposition == "complete_ready"
    assert response.proof.release_evidence.verified_at == adapter.facts.completed_at
    assert await restarted.execute_teardown(peer, request) == response
    assert adapter.commit_count == 1
    await restarted.close()


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
async def test_teardown_response_keeps_provider_intent_when_capacity_releases_after_commit(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = await _ready(tmp_path)
    adapter.release_reservation_after_commit = lambda: setattr(
        repository, "released_after_provider", True
    )

    response = await service.execute_teardown(peer, request)

    assert response.proof.disposition == "complete_ready"
    assert adapter.commit_count == 1
    await service.close()


@pytest.mark.anyio
async def test_malformed_host_facts_cannot_become_a_terminal_proof(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = await _ready(tmp_path)
    adapter.facts = adapter.facts.model_copy(update={"domain_absent": "true"})
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_teardown(peer, request)
    assert repository.records[-1].phase is JournalPhase.PROVIDER_RETURNED
    assert not any(record.phase is JournalPhase.TERMINAL for record in repository.records)
    await service.close()


def _boundary_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("authority provider boundary failed")
    ]


@pytest.mark.anyio
async def test_adapter_failure_log_names_only_the_exception_type(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, _repository, adapter, peer, request = await _ready(tmp_path)
    adapter.failure = ValueError("adapter-output-do-not-log")

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(AuthorityServiceError, match="provider_conflict"),
    ):
        await service.execute_teardown(peer, request)

    assert _boundary_messages(caplog) == ["authority provider boundary failed: ValueError"]
    assert all("adapter-output-do-not-log" not in record.getMessage() for record in caplog.records)
    await service.close()


@pytest.mark.anyio
async def test_recovery_refusal_log_names_its_fixed_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, _repository, adapter, peer, request = await _ready(tmp_path)
    adapter.failure = ProviderRecoveryRefusal("external-boot recovery phase is not resumable")

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(AuthorityServiceError, match="provider_conflict"),
    ):
        await service.execute_teardown(peer, request)

    assert _boundary_messages(caplog) == [
        "authority provider boundary failed: ProviderRecoveryRefusal: "
        "external-boot recovery phase is not resumable"
    ]
    await service.close()


@pytest.mark.anyio
async def test_post_commit_reobservation_under_a_successor_record_is_superseded(
    tmp_path: Path,
) -> None:
    """#2921: a successor that began before this generation re-observes answers `superseded`."""
    service, repository, adapter, peer, request = await _ready(tmp_path)
    in_run_facts = adapter.facts

    class _Superseded(_TeardownAdapter):
        async def observe_system_teardown(
            self, request: AuthorityTeardownMutationRequestV1, context: AuthorityCommitContextV1
        ) -> AuthoritySystemTeardownFacts:
            self.read_count += 1
            if self.read_count > 1:
                raise SystemTeardownSupersededError
            return in_run_facts

    superseding = _Superseded()
    service._adapter = superseding

    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_teardown(peer, request)

    assert superseding.commit_count == 1
    assert repository.records[-1].phase is JournalPhase.TERMINAL
    await service.close()


class _RemoteTeardownHost:
    """Remote host whose first destroy is lost; later calls complete."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.present = True

    def validate_system_teardown(self, _state: object) -> None:
        self.calls.append("validate")

    def destroy_system_domain(self, _state: object) -> None:
        self.calls.append("destroy")
        if self.calls.count("destroy") == 1:
            raise OSError("injected lost destroy reply")

    def undefine_system_domain(self, _state: object) -> None:
        self.calls.append("undefine")
        self.present = False

    def remove_system_artifacts(self, _state: object) -> None:
        self.calls.append("remove")

    def inspect_system_teardown(
        self, _state: object, *, domain_validated: bool
    ) -> RemoteSystemTeardownInspection:
        return RemoteSystemTeardownInspection(
            domain_absent=not self.present,
            overlay_absent=not self.present,
            baseline_absent=not self.present,
            recovery_absent=True,
        )


class _DiesBeforeBegin(RemoteExternalBootAuthorityAdapter):
    """Real remote adapter whose chosen generation dies after `mutation-started`, before `begin`."""

    die_at_generation: int | None = None

    async def execute_system_teardown(
        self,
        request: AuthorityTeardownMutationRequestV1,
        context: AuthorityCommitContextV1,
        reservation: AuthorityTeardownReservationV1,
    ) -> AuthoritySystemTeardownFacts:
        if request.generation == self.die_at_generation:
            raise OSError("injected death before begin")
        return await super().execute_system_teardown(request, context, reservation)


@pytest.mark.anyio
async def test_takeover_recovers_a_generation_that_never_began_over_an_older_record(
    tmp_path: Path,
) -> None:
    """#2921: recovery of the older head converges instead of `provider_conflict`."""
    peer = AuthenticatedPeer(uuid4())
    first = _takeover().model_copy(
        update={
            "purpose": "teardown",
            "operation": AuthorityOperation.TEARDOWN,
            "provider_kind": "remote-libvirt",
        }
    )
    repository = _TeardownRepository(peer, first)
    (tmp_path / "provider").mkdir(mode=0o700)
    store = RemoteModuleVolumePreparationStore(tmp_path / "provider")
    host = _RemoteTeardownHost()
    adapter = _DiesBeforeBegin(
        cast(Any, object()),
        RemoteExternalBootCoordinator(cast(Any, host), store, lambda: 300.0),
        RemoteModulePreparationExecutor(),
        teardown_clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    adapter.die_at_generation = 2
    services: list[ExternalBootAuthorityService] = []

    def restart() -> ExternalBootAuthorityService:
        services.append(
            ExternalBootAuthorityService(
                repository=repository,
                adapter=adapter,
                journal_factory=lambda system_id: FileAuthorityJournal(
                    tmp_path, f"{system_id}.journal"
                ),
            )
        )
        return services[-1]

    attempt_id = str(uuid4())

    def teardown(takeover: Any) -> AuthorityTeardownMutationRequestV1:
        values = takeover.model_dump(mode="json", by_alias=True)
        values.update(schema="external-boot-authority-teardown-request-v1", attempt_id=attempt_id)
        return AuthorityTeardownMutationRequestV1.model_validate(values)

    async def take_over(
        service: ExternalBootAuthorityService, generation: int
    ) -> AuthorityTeardownMutationRequestV1:
        takeover = first.model_copy(update={"authority_id": uuid4(), "generation": generation})
        repository.allocating_request = repository.request = takeover
        repository.current = False
        await service.acknowledge_takeover(peer, takeover)
        repository.current = True
        return teardown(takeover)

    try:
        service = restart()
        await service.acknowledge_takeover(peer, first)
        repository.current = True
        with pytest.raises(AuthorityServiceError, match="provider_conflict"):
            await service.execute_teardown(peer, teardown(first))
        service = restart()
        with pytest.raises(AuthorityServiceError, match="provider_conflict"):
            await service.execute_teardown(peer, await take_over(service, 2))
        assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED
        assert repository.records[-1].generation == 2

        service = restart()
        response = await service.execute_teardown(peer, await take_over(service, 3))
    finally:
        for service in services:
            await service.close()
        adapter.close()
        store.close()

    recovered = next(
        record
        for record in repository.records
        if record.phase is JournalPhase.TERMINAL and record.generation == 2
    )
    assert recovered.outcome == "conflict"
    assert response.proof.disposition == "complete_ready"
    terminal_proofs = [
        record
        for record in repository.records
        if record.phase is JournalPhase.TERMINAL and record.outcome == "absent"
    ]
    assert [record.generation for record in terminal_proofs] == [3]
    assert host.calls.count("undefine") == 1
