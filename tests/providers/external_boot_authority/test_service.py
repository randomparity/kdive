"""Serialized external-boot authority service tests (ADR-0584)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest

from kdive.db.external_boot_authority_journal import AuthorityBinding
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_timing import LocalExternalBootTimingV1
from kdive.domain.remote_module_attempt_preparation import (
    ModuleAttemptObligationReceiptV1,
    ModuleAttemptPreparationRequestV1,
)
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.proof_barrier import (
    ProofBarrierAbort,
    ProofBarrierShutdown,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCleanupEvidenceContextV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    AuthorityPreparationMutationRequestV1,
    AuthorityRecoveryOrphanDispositionRequestV1,
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
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootPreparationObservation,
    OpaqueProviderRef,
    RecoveryObjectObservation,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModulePreparationBeginRequestV1,
    RemoteModulePreparationBeginResponseV1,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
)
from kdive.serialization import ServerFaultError
from tests.providers.external_boot_authority.service_support import (
    _DIGEST_A,
    _DIGEST_B,
    _Adapter,
    _FailingAppendJournal,
    _mutation,
    _Repository,
    _service,
    _takeover,
)
from tests.support.external_boot_plan import external_boot_materialization, external_boot_plan


def test_journal_replay_binds_the_local_timing_snapshot() -> None:
    timing = LocalExternalBootTimingV1(accel="tcg", console_window_s=9000, deadline_budget_s=12000)
    legacy = _mutation(_takeover())
    request = legacy.model_copy(update={"local_timing": timing})
    record = ExternalBootAuthorityService._record(request, [], JournalPhase.ADMITTED)
    assert record.local_timing == timing
    assert ExternalBootAuthorityService._operation_matches(record, request)
    changed = request.model_copy(
        update={
            "local_timing": LocalExternalBootTimingV1(
                accel="tcg", console_window_s=10000, deadline_budget_s=13000
            )
        }
    )
    assert not ExternalBootAuthorityService._operation_matches(record, changed)
    assert not ExternalBootAuthorityService._operation_matches(
        record, request.model_copy(update={"local_timing": None})
    )
    legacy_record = ExternalBootAuthorityService._record(legacy, [], JournalPhase.ADMITTED)
    assert ExternalBootAuthorityService._operation_matches(legacy_record, legacy)
    assert "local_timing" not in legacy_record.model_dump(mode="json", by_alias=True)


class _PreparationAdapter:
    def __init__(self, receipt: ExternalBootPreparationObservation) -> None:
        self.calls: list[str] = []
        self.receipt = receipt
        self.reopened = receipt

    async def commit(self, request: object, context: object) -> AuthorityObservationV1:
        self.calls.append("commit:materialize")
        return AuthorityObservationV1(
            observation_id=uuid4(), category="target", composite_state=self.receipt.identity
        )

    async def observe(self, request: object) -> AuthorityObservationV1:
        self.calls.append("observe")
        return AuthorityObservationV1(
            observation_id=uuid4(), category="target", composite_state=self.receipt.identity
        )

    async def preparation_receipt(
        self, request: AuthorityPreparationMutationRequestV1
    ) -> ExternalBootPreparationObservation:
        return self.reopened


class _RecordingProofCheckpoint:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.identities: list[tuple[UUID, UUID, AuthorityOperation]] = []

    async def checkpoint(
        self,
        system_id: UUID,
        run_id: UUID,
        operation: AuthorityOperation,
        checkpoint: Literal["before-provider", "after-provider", "before-finalize"],
    ) -> None:
        self.identities.append((system_id, run_id, operation))
        self.calls.append(checkpoint)


@pytest.mark.anyio
async def test_proof_checkpoints_wrap_provider_commit_before_provider_returned(
    tmp_path: Path,
) -> None:
    _unused, repository, adapter, peer, takeover = _service(tmp_path)
    checkpoint_calls = adapter.calls
    proof = _RecordingProofCheckpoint(checkpoint_calls)
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
        proof_checkpoint=proof,
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True

    request = _mutation(takeover)
    await service.execute_mutation(peer, request)

    assert checkpoint_calls == ["before-provider", "commit:activate", "after-provider", "observe"]
    assert proof.identities == [
        (request.system_id, request.run_id, request.operation),
        (request.system_id, request.run_id, request.operation),
    ]
    assert [record.phase for record in repository.records][-3:] == [
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]


class _FinalizingAdapter(_Adapter):
    """A finalizer whose private receipt survives a failed finalize (ADR-0615)."""

    receipt = cast(RecoveryObjectObservation, object())

    async def finalize(self, request: object, context: object) -> None:
        self.calls.append("finalize")

    async def cleanup_quarantine_inventory(
        self, request: object
    ) -> tuple[RecoveryObjectObservation, ...]:
        return (self.receipt,)


class _FinalizeFault(_RecordingProofCheckpoint):
    def __init__(self, calls: list[str], fault: Exception) -> None:
        super().__init__(calls)
        self.fault: Exception | None = fault

    async def checkpoint(
        self,
        system_id: UUID,
        run_id: UUID,
        operation: AuthorityOperation,
        checkpoint: Literal["before-provider", "after-provider", "before-finalize"],
    ) -> None:
        await super().checkpoint(system_id, run_id, operation, checkpoint)
        if checkpoint == "before-finalize" and self.fault is not None:
            fault, self.fault = self.fault, None
            raise fault


async def _finalize_fault_service(
    tmp_path: Path, fault: Exception
) -> tuple[ExternalBootAuthorityService, _Repository, _FinalizingAdapter, AuthenticatedPeer, Any]:
    _unused, repository, _plain, peer, takeover = _service(tmp_path)
    adapter = _FinalizingAdapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
        proof_checkpoint=_FinalizeFault(adapter.calls, fault),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    return service, repository, adapter, peer, _mutation(takeover)


@pytest.mark.anyio
async def test_before_finalize_abort_publishes_then_replay_finalizes(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = await _finalize_fault_service(
        tmp_path, ProofBarrierAbort("aborted")
    )

    with pytest.raises(AuthorityServiceError) as raised:
        await service.execute_mutation(peer, request)

    assert raised.value.category == "provider_conflict"
    assert adapter.calls[-2:] == ["observe", "before-finalize"]
    assert repository.records[-1].phase is JournalPhase.TERMINAL
    assert [args[-1] for args in repository.published_cleanup_quarantines] == [(adapter.receipt,)]

    observation = await service.execute_mutation(peer, request)

    assert observation == repository.records[-1].observation
    assert adapter.calls[-2:] == ["before-finalize", "finalize"]
    assert adapter.calls.count("finalize") == 1
    assert len(repository.published_cleanup_quarantines) == 1


@pytest.mark.anyio
async def test_shutdown_at_before_finalize_publishes_nothing(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = await _finalize_fault_service(
        tmp_path, ProofBarrierShutdown("shutdown")
    )

    with pytest.raises(ProofBarrierShutdown):
        await service.execute_mutation(peer, request)

    assert "finalize" not in adapter.calls
    assert repository.published_cleanup_quarantines == []


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
async def test_read_only_observation_requires_current_acknowledged_authority(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    records = list(repository.records)

    observation = await service.observe_authority(peer, _mutation(takeover))

    assert observation.category == "target"
    assert adapter.calls == ["observe"]
    assert repository.records == records


@pytest.mark.anyio
async def test_read_only_observation_rechecks_current_authority(tmp_path: Path) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    repository.reject_resolution = 2

    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.observe_authority(peer, _mutation(takeover))
    assert adapter.calls == ["observe"]
    assert [record.phase for record in repository.records] == [
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("evidence_present", [True, False])
@pytest.mark.parametrize(
    ("purpose", "operation"),
    [("release", AuthorityOperation.CLEANUP)],
)
async def test_authenticated_cleanup_resolves_subject_evidence_before_commit(
    tmp_path: Path, evidence_present: bool, purpose: str, operation: AuthorityOperation
) -> None:
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover().model_copy(
        update={
            "purpose": purpose,
            "operation": operation,
            "provider_kind": "remote-libvirt",
        }
    )
    repository = _Repository(peer, takeover)

    class CleanupAdapter(_Adapter):
        async def cleanup_subject(self, request: object) -> str:
            self.calls.append("cleanup-subject")
            return "0" * 32

        async def commit_cleanup(
            self, request: object, context: object, evidence: object
        ) -> object:
            self.calls.append("commit-cleanup")
            assert evidence is repository.cleanup_evidence
            return self._observation("target")

    adapter = CleanupAdapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    request = _mutation(takeover)
    if evidence_present:
        repository.cleanup_evidence = AuthorityCleanupEvidenceContextV1(
            operation_identity=request.operation_identity,
            attempt_id=request.attempt_id,
            operation_nonce="0" * 32,
            cleanup_state="open",
            recovery_reference_json="{}",
        )

    if evidence_present:
        await service.execute_mutation(peer, request)
    else:
        with pytest.raises(AuthorityServiceError, match="provider_conflict"):
            await service.execute_mutation(peer, request)

    assert repository.cleanup_nonces == ["0" * 32]
    assert adapter.calls == (
        ["cleanup-subject", "commit-cleanup", "observe"]
        if evidence_present
        # A cleanup refusal is not re-observed.
        else ["cleanup-subject"]
    )


@pytest.mark.anyio
@pytest.mark.parametrize("purpose", ["release", "recover"])
async def test_recover_commits_without_cleanup_evidence(tmp_path: Path, purpose: str) -> None:
    """A RECOVER restores the definition and deletes nothing, so it needs no cleanup evidence."""
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover().model_copy(
        update={
            "purpose": purpose,
            "operation": AuthorityOperation.RECOVER,
            "provider_kind": "remote-libvirt",
        }
    )
    repository = _Repository(peer, takeover)

    class CleanupAdapter(_Adapter):
        async def cleanup_subject(self, request: object) -> str:
            raise AssertionError("release recover reached the cleanup-evidence gate")

        async def commit_cleanup(
            self, request: object, context: object, evidence: object
        ) -> object:
            raise AssertionError("release recover reached commit_cleanup")

    adapter = CleanupAdapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True

    await service.execute_mutation(peer, _mutation(takeover))

    assert repository.cleanup_nonces == []
    assert adapter.calls == ["commit:recover", "observe"]


@pytest.mark.anyio
async def test_cancelling_observation_keeps_its_system_lane_until_provider_completion(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    adapter.observe_release.clear()
    first = asyncio.create_task(service.observe_authority(peer, _mutation(takeover)))
    await adapter.observe_entered.wait()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second_started = asyncio.Event()

    async def observe_again() -> AuthorityObservationV1:
        second_started.set()
        return await service.observe_authority(peer, _mutation(takeover))

    second = asyncio.create_task(observe_again())
    await second_started.wait()
    assert adapter.calls == ["observe"]
    adapter.observe_release.set()
    await second
    assert adapter.calls == ["observe", "observe"]


@pytest.mark.anyio
async def test_read_only_observation_rejects_a_stale_trusted_head_before_provider_access(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    repository._head_override_armed = True
    repository.corrupt_head_field = "digest"
    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )

    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await restarted.observe_authority(peer, _mutation(takeover))

    assert adapter.calls == []


@pytest.mark.anyio
async def test_read_only_observation_rejects_unresolved_restart_before_provider_access(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    journal_name = "empty-observation.journal"
    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda _system_id: FileAuthorityJournal(tmp_path, journal_name),
        adapter=adapter,
    )

    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await restarted.observe_authority(peer, _mutation(takeover))

    assert adapter.calls == []


@pytest.mark.anyio
async def test_preparation_uses_authenticated_lane_and_exact_receipt(tmp_path: Path) -> None:
    service, repository, _adapter, peer, takeover = _service(tmp_path)
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    takeover = takeover.model_copy(update={"plan_identity": plan.identity})
    repository.request = takeover
    repository.allocating_request = takeover
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    request = AuthorityPreparationMutationRequestV1(
        **takeover.model_dump(
            mode="python",
            by_alias=True,
            exclude={"operation", "operation_identity", "operation_digest", "plan_identity"},
        ),
        operation="materialize",
        operation_identity="materialize-op",
        operation_digest="sha256:" + "c" * 64,
        plan_identity=plan.identity,
        attempt_id=uuid4(),
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(),
        plan=plan,
    )
    receipt = ExternalBootPreparationObservation(
        state="materialized",
        binding=ExternalBootActivationBinding(
            system_id=str(request.system_id),
            run_id=str(request.run_id),
            activation_id=str(request.activation_id),
        ),
        plan_identity=plan.identity,
        authority=OpaqueProviderRef(
            ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
        ),
        operation_identity=request.operation_identity,
        materialization=external_boot_materialization(plan),
    )
    adapter = _PreparationAdapter(receipt)
    service._adapter = cast(Any, adapter)

    response = await service.execute_preparation(peer, request)

    assert response.receipt == receipt
    assert response.observation.composite_state == receipt.identity
    terminal = repository.records[-1]
    assert response.journal_sequence == terminal.sequence
    assert response.journal_digest == record_digest(terminal)
    assert adapter.calls == ["commit:materialize", "observe"]
    assert repository.records[-1].phase is JournalPhase.TERMINAL


def _materialize(
    takeover: AuthorityTakeoverRequestV1, operation_identity: str
) -> AuthorityPreparationMutationRequestV1:
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    return AuthorityPreparationMutationRequestV1(
        **takeover.model_dump(
            mode="python",
            by_alias=True,
            exclude={"operation", "operation_identity", "operation_digest", "plan_identity"},
        ),
        operation="materialize",
        operation_identity=operation_identity,
        operation_digest="sha256:" + "c" * 64,
        plan_identity=plan.identity,
        attempt_id=uuid4(),
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(),
        plan=plan,
    )


def _materialized(request: AuthorityPreparationMutationRequestV1) -> _PreparationAdapter:
    return _PreparationAdapter(
        ExternalBootPreparationObservation(
            state="materialized",
            binding=ExternalBootActivationBinding(
                system_id=str(request.system_id),
                run_id=str(request.run_id),
                activation_id=str(request.activation_id),
            ),
            plan_identity=request.plan_identity,
            authority=OpaqueProviderRef(
                ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
            ),
            operation_identity=request.operation_identity,
            materialization=external_boot_materialization(request.plan),
        )
    )


async def _lane_with_terminal_materialize(
    tmp_path: Path,
) -> tuple[
    ExternalBootAuthorityService, _Repository, AuthenticatedPeer, AuthorityTakeoverRequestV1
]:
    service, repository, _adapter, peer, takeover = _service(tmp_path)
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    takeover = takeover.model_copy(update={"plan_identity": plan.identity})
    repository.request = repository.allocating_request = takeover
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    first = _materialize(takeover, "materialize-a")
    service._adapter = cast(Any, _materialized(first))
    await service.execute_preparation(peer, first)
    assert repository.records[-1].phase is JournalPhase.TERMINAL
    return service, repository, peer, takeover


async def _successor(
    service: ExternalBootAuthorityService,
    repository: _Repository,
    peer: AuthenticatedPeer,
    successor: AuthorityTakeoverRequestV1,
) -> None:
    repository.current = False
    repository.request = repository.allocating_request = successor
    await service.acknowledge_takeover(peer, successor)
    repository.current = True


@pytest.mark.anyio
async def test_another_activations_terminal_preparation_is_not_a_predecessor(
    tmp_path: Path,
) -> None:
    """#2968: a reused System's lane holds the prior activation's records; they are history."""
    service, repository, peer, takeover = await _lane_with_terminal_materialize(tmp_path)
    run_id = uuid4()
    successor = takeover.model_copy(
        update={
            "authority_id": uuid4(),
            "generation": 2,
            "activation_id": uuid4(),
            "run_id": run_id,
            "plan_identity": external_boot_plan(takeover.system_id, run_id).identity,
            "operation_identity": "takeover-b",
        }
    )
    await _successor(service, repository, peer, successor)
    second = _materialize(successor, "materialize-b")
    service._adapter = cast(Any, _materialized(second))

    await service.execute_preparation(peer, second)

    assert repository.records[-1].phase is JournalPhase.TERMINAL
    assert repository.records[-1].activation_id == successor.activation_id


@pytest.mark.anyio
async def test_same_activation_predecessor_mismatch_still_refuses(tmp_path: Path) -> None:
    """The activation scope must not skip a takeover's own predecessor check (ADR-0713)."""
    service, repository, peer, takeover = await _lane_with_terminal_materialize(tmp_path)
    run_id = uuid4()
    successor = takeover.model_copy(
        update={
            "authority_id": uuid4(),
            "generation": 2,
            "run_id": run_id,
            "plan_identity": external_boot_plan(takeover.system_id, run_id).identity,
            "operation_identity": "takeover-b",
        }
    )
    await _successor(service, repository, peer, successor)
    drifted = _materialize(successor, "materialize-b")
    records = len(repository.records)

    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_preparation(peer, drifted)

    assert (refused.value.category, refused.value.reason) == (
        "journal_conflict",
        "predecessor_operation_mismatch",
    )
    assert len(repository.records) == records


@pytest.mark.anyio
async def test_remote_prepare_begin_anchors_before_opening_its_authority_receipt(
    tmp_path: Path,
) -> None:
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover().model_copy(update={"provider_kind": "remote-libvirt"})
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    takeover = takeover.model_copy(update={"plan_identity": plan.identity})

    class PreparationRepository(_Repository):
        async def resolve_allocating(
            self, peer: AuthenticatedPeer, request: AuthorityTakeoverRequestV1
        ) -> AuthorityBinding | None:
            binding = await super().resolve_allocating(peer, request)
            return replace(binding, preparation_plan=plan) if binding is not None else None

    repository = PreparationRepository(peer, takeover)
    repository.remote_attempt = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=takeover.system_id,
            run_id=takeover.run_id,
            operation_nonce="a" * 32,
        )
    )

    class Host:
        async def begin(
            self,
            authority: AuthorityPreparationMutationRequestV1,
            preparation: ModuleAttemptPreparationRequestV1,
            budget_seconds: int,
        ) -> RemoteModulePreparationBeginResponseV1:
            assert budget_seconds == 30
            obligation = preparation.module_attempt_obligation
            return RemoteModulePreparationBeginResponseV1(
                preparation=preparation,
                operation=RemoteModuleOperationV1(
                    operation="capture_install",
                    system_id=str(obligation.system_id),
                    run_id=str(obligation.run_id),
                    plan_identity=authority.plan_identity,
                    operation_nonce=obligation.operation_nonce,
                    release=authority.plan.module_obligation.release,
                    root_volume={"key": "root", "identity": "sha256:" + "d" * 64},
                    source_manifest=authority.plan.module_obligation.source_manifest,
                    appliance_image_digest="sha256:" + "e" * 64,
                ),
            )

        async def execute(self, request: object) -> object:
            raise AssertionError(f"begin must not execute the provider host: {request!r}")

    materialize = AuthorityPreparationMutationRequestV1(
        **takeover.model_dump(
            mode="python",
            by_alias=True,
            exclude={"operation", "operation_identity", "operation_digest", "plan_identity"},
        ),
        operation="materialize",
        operation_identity="materialize-op",
        operation_digest="sha256:" + "b" * 64,
        plan_identity=plan.identity,
        attempt_id=uuid4(),
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(),
        plan=plan,
    )
    materialization = external_boot_materialization(plan)
    adapter = _PreparationAdapter(
        ExternalBootPreparationObservation(
            state="materialized",
            binding=ExternalBootActivationBinding(
                system_id=str(takeover.system_id),
                run_id=str(takeover.run_id),
                activation_id=str(takeover.activation_id),
            ),
            plan_identity=plan.identity,
            authority=OpaqueProviderRef(ref="authority/materialize"),
            operation_identity=materialize.operation_identity,
            materialization=materialization,
        )
    )

    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=cast(Any, adapter),
        remote_module_host=cast(Any, Host()),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    await service.execute_preparation(peer, materialize)
    request = AuthorityPreparationMutationRequestV1(
        **takeover.model_dump(
            mode="python",
            by_alias=True,
            exclude={"operation", "operation_identity", "operation_digest", "plan_identity"},
        ),
        operation="prepare",
        operation_identity="prepare-op",
        operation_digest="sha256:" + "c" * 64,
        plan_identity=plan.identity,
        attempt_id=uuid4(),
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(),
        plan=plan,
    )

    begin = RemoteModulePreparationBeginRequestV1(authority=request, budget_seconds=30)
    response = await service.open_remote_module_attempt(peer, begin)

    assert response.preparation == repository.remote_attempt
    assert [record.phase for record in repository.records][-2:] == [
        JournalPhase.ADMITTED,
        JournalPhase.MUTATION_STARTED,
    ]
    acknowledgement = repository.records[1]
    assert repository.remote_attempt_calls == [
        (acknowledgement.sequence, record_digest(acknowledgement), request.attempt_id.hex)
    ]
    assert await service.open_remote_module_attempt(peer, begin) == response
    assert len(repository.remote_attempt_calls) == 2

    successor = takeover.model_copy(
        update={"authority_id": uuid4(), "generation": 2, "operation_identity": "takeover-b"}
    )
    repository.allocating_request = successor
    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=cast(Any, adapter),
        remote_module_host=cast(Any, Host()),
    )

    # 10s, not a tight bound: this deadline exists to catch a deadlock in acknowledge_takeover,
    # and the call writes through FileAuthorityJournal. The former 0.1s had ~10x headroom on an
    # idle host and none under the gate's xdist worksteal beside a Postgres testcontainer, where
    # it timed out on CI while passing every local run (#2339).
    acknowledgement = await asyncio.wait_for(
        restarted.acknowledge_takeover(peer, successor), timeout=10
    )

    assert acknowledgement.generation == successor.generation


@pytest.mark.anyio
async def test_remote_prepare_execute_finishes_only_the_begun_prepare_phase(tmp_path: Path) -> None:
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover().model_copy(update={"provider_kind": "remote-libvirt"})
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    takeover = takeover.model_copy(update={"plan_identity": plan.identity})
    repository = _Repository(peer, takeover)
    nonce = "a" * 32
    repository.remote_attempt = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=takeover.system_id, run_id=takeover.run_id, operation_nonce=nonce
        )
    )
    completed: list[RemoteModuleVolumePreparationRequestV1] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class Result:
        def validate_terminal_for(self, operation: object, authority: object) -> None:
            assert operation is remote.operation
            assert authority is remote.authority

    class Host:
        async def begin(
            self,
            authority: AuthorityPreparationMutationRequestV1,
            preparation: ModuleAttemptPreparationRequestV1,
            budget_seconds: int,
        ) -> RemoteModulePreparationBeginResponseV1:
            assert authority == request
            assert budget_seconds == 30
            return RemoteModulePreparationBeginResponseV1(
                preparation=preparation,
                operation=remote.operation,
            )

        async def execute(
            self, request: RemoteModuleVolumePreparationRequestV1
        ) -> RemoteModuleTerminalPreparationResponseV1:
            completed.append(request)
            entered.set()
            await release.wait()
            return cast(RemoteModuleTerminalPreparationResponseV1, Result())

        async def observe_lifecycle(
            self, request: RemoteModuleLifecycleRequestV1
        ) -> RemoteModuleLifecycleResponseV1 | None:
            raise AssertionError(f"unexpected lifecycle observation: {request!r}")

        async def execute_lifecycle(
            self, request: RemoteModuleLifecycleRequestV1
        ) -> RemoteModuleLifecycleResponseV1:
            raise AssertionError(f"unexpected lifecycle request: {request!r}")

    adapter = _Adapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
        remote_module_host=Host(),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    request = AuthorityPreparationMutationRequestV1(
        **takeover.model_dump(
            mode="python",
            by_alias=True,
            exclude={"operation", "operation_identity", "operation_digest", "plan_identity"},
        ),
        operation="prepare",
        operation_identity="prepare-op",
        operation_digest="sha256:" + "c" * 64,
        plan_identity=plan.identity,
        attempt_id=uuid4(),
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(),
        plan=plan,
    )
    remote = RemoteModuleVolumePreparationRequestV1(
        authority=request,
        operation=RemoteModuleOperationV1(
            operation="capture_install",
            system_id=str(request.system_id),
            run_id=str(request.run_id),
            plan_identity=request.plan_identity,
            operation_nonce=nonce,
            release=request.plan.module_obligation.release,
            root_volume={"key": "root", "identity": "sha256:" + "d" * 64},
            source_manifest=request.plan.module_obligation.source_manifest,
            appliance_image_digest="sha256:" + "e" * 64,
        ),
    )
    await service.open_remote_module_attempt(
        peer, RemoteModulePreparationBeginRequestV1(authority=request, budget_seconds=30)
    )

    first = asyncio.create_task(service.execute_remote_module_preparation(peer, remote))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_remote_module_preparation(peer, remote)
    assert completed == [remote]
    release.set()
    for _ in range(20):
        if repository.records[-1].phase is JournalPhase.TERMINAL:
            break
        await asyncio.sleep(0)
    assert completed == [remote]
    assert [record.phase for record in repository.records][-3:] == [
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]
    assert adapter.calls == ["commit:prepare", "observe"]


async def _failed_prepare_service(
    tmp_path: Path,
    failure: Exception,
    calls: list[object] | None = None,
    *,
    begun: bool = True,
) -> tuple[Any, Any, Any, AuthenticatedPeer, AuthorityPreparationMutationRequestV1, Any]:
    """A service whose PREPARE was begun and whose appliance run then raised ``failure``."""
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover().model_copy(update={"provider_kind": "remote-libvirt"})
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    takeover = takeover.model_copy(update={"plan_identity": plan.identity})
    repository = _Repository(peer, takeover)

    def prepare(identity: str, nonce: str) -> tuple[Any, Any]:
        request = AuthorityPreparationMutationRequestV1(
            **takeover.model_dump(
                mode="python",
                by_alias=True,
                exclude={"operation", "operation_identity", "operation_digest", "plan_identity"},
            ),
            operation="prepare",
            operation_identity=identity,
            operation_digest="sha256:" + "c" * 64,
            plan_identity=plan.identity,
            attempt_id=uuid4(),
            expected_source_identity="source-a",
            intended_target_identity="target-a",
            recovery_objects=(),
            plan=plan,
        )
        remote = RemoteModuleVolumePreparationRequestV1(
            authority=request,
            operation=RemoteModuleOperationV1(
                operation="capture_install",
                system_id=str(request.system_id),
                run_id=str(request.run_id),
                plan_identity=request.plan_identity,
                operation_nonce=nonce,
                release=request.plan.module_obligation.release,
                root_volume={"key": "root", "identity": "sha256:" + "d" * 64},
                source_manifest=request.plan.module_obligation.source_manifest,
                appliance_image_digest="sha256:" + "e" * 64,
            ),
        )
        return request, remote

    class Host:
        async def begin(
            self,
            authority: AuthorityPreparationMutationRequestV1,
            preparation: ModuleAttemptPreparationRequestV1,
            budget_seconds: int,
        ) -> RemoteModulePreparationBeginResponseV1:
            return RemoteModulePreparationBeginResponseV1(
                preparation=preparation,
                operation=prepare(authority.operation_identity, "a" * 32)[1].operation,
            )

        async def execute(self, request: object) -> object:
            if calls is not None:
                calls.append(request)
            raise failure

    repository.remote_attempt = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=takeover.system_id, run_id=takeover.run_id, operation_nonce="a" * 32
        )
    )
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=_Adapter(),
        remote_module_host=cast(Any, Host()),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    first, remote = prepare("prepare-op", "a" * 32)
    if begun:
        await service.open_remote_module_attempt(
            peer, RemoteModulePreparationBeginRequestV1(authority=first, budget_seconds=30)
        )
    return service, repository, remote, peer, first, prepare


@pytest.mark.anyio
async def test_unsettled_prepare_failure_rejects_the_next_prepare(tmp_path: Path) -> None:
    """Pins the wedge: a raw failure leaves PREPARE unresolved and the retry is refused."""
    service, repository, remote, peer, _first, prepare = await _failed_prepare_service(
        tmp_path, RuntimeError("appliance failed")
    )
    with pytest.raises(RuntimeError, match="appliance failed"):
        await service.execute_remote_module_preparation(peer, remote)
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED

    retry, _ = prepare("prepare-op-2", "b" * 32)
    with pytest.raises(AuthorityServiceError) as refused:
        await service.open_remote_module_attempt(
            peer, RemoteModulePreparationBeginRequestV1(authority=retry, budget_seconds=30)
        )
    assert refused.value.category == "provider_conflict"


@pytest.mark.anyio
async def test_abandoned_prepare_failure_is_settled_so_the_retry_proceeds(tmp_path: Path) -> None:
    failure = CategorizedError(
        "appliance failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"completion": "failed-after-mutation", "abandoned": True},
    )
    service, repository, remote, peer, _first, prepare = await _failed_prepare_service(
        tmp_path, failure
    )
    with pytest.raises(CategorizedError) as raised:
        await service.execute_remote_module_preparation(peer, remote)
    assert raised.value is failure
    assert [record.phase for record in repository.records][-3:] == [
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]
    assert repository.records[-1].outcome == "source"
    assert repository.records[-1].observation.category == "source"

    retry, _ = prepare("prepare-op-2", "b" * 32)
    await service.open_remote_module_attempt(
        peer, RemoteModulePreparationBeginRequestV1(authority=retry, budget_seconds=30)
    )
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
async def test_replay_of_an_abandoned_prepare_reports_the_failure_without_running_it(
    tmp_path: Path,
) -> None:
    calls: list[object] = []
    failure = CategorizedError(
        "appliance failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"completion": "failed-after-mutation", "abandoned": True},
    )

    service, repository, remote, peer, _first, _prepare = await _failed_prepare_service(
        tmp_path, failure, calls
    )
    with pytest.raises(CategorizedError):
        await service.execute_remote_module_preparation(peer, remote)
    assert len(calls) == 1
    records = len(repository.records)

    with pytest.raises(CategorizedError) as replay:
        await service.execute_remote_module_preparation(peer, remote)

    assert replay.value.details == {"completion": "failed-after-mutation"}
    assert len(calls) == 1
    assert len(repository.records) == records


@pytest.mark.anyio
async def test_unsettleable_abandon_reports_the_provider_failure(tmp_path: Path) -> None:
    failure = CategorizedError(
        "appliance failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"completion": "failed-after-mutation", "abandoned": True},
    )
    service, repository, remote, peer, _first, _prepare = await _failed_prepare_service(
        tmp_path, failure
    )

    async def refuse(*_args: object) -> None:
        raise OSError("journal unavailable")

    service._anchor = refuse  # type: ignore[method-assign]
    with pytest.raises(CategorizedError) as raised:
        await service.execute_remote_module_preparation(peer, remote)

    assert raised.value is failure
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
async def test_begin_refused_after_anchoring_is_settled_so_the_retry_proceeds(
    tmp_path: Path,
) -> None:
    """The receipt gate refusing (superseded) must not leave the anchored PREPARE open."""
    service, repository, _remote, peer, _first, prepare = await _failed_prepare_service(
        tmp_path, RuntimeError("unused"), begun=False
    )
    receipt = repository.remote_attempt
    repository.remote_attempt = None
    retry, _ = prepare("prepare-op-2", "b" * 32)
    with pytest.raises(AuthorityServiceError) as refused:
        await service.open_remote_module_attempt(
            peer, RemoteModulePreparationBeginRequestV1(authority=retry, budget_seconds=30)
        )
    assert refused.value.category == "superseded"
    assert [record.phase for record in repository.records][-3:] == [
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]
    assert repository.records[-1].operation_identity == retry.operation_identity

    repository.remote_attempt = receipt
    third, _ = prepare("prepare-op-3", "c" * 32)
    await service.open_remote_module_attempt(
        peer, RemoteModulePreparationBeginRequestV1(authority=third, budget_seconds=30)
    )
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
async def test_rebegin_of_an_earlier_operation_is_never_settled(tmp_path: Path) -> None:
    """An idempotent re-begin may belong to a live execute, so its refusal leaves it open."""
    service, repository, _remote, peer, first, _prepare = await _failed_prepare_service(
        tmp_path, RuntimeError("unused")
    )
    repository.remote_attempt = None

    with pytest.raises(AuthorityServiceError) as refused:
        await service.open_remote_module_attempt(
            peer, RemoteModulePreparationBeginRequestV1(authority=first, budget_seconds=30)
        )

    assert refused.value.category == "superseded"
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
async def test_transient_host_begin_failure_is_not_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _remote, peer, first, _prepare = await _failed_prepare_service(
        tmp_path, RuntimeError("unused"), begun=False
    )

    async def derive_failed(*_args: object) -> None:
        raise TimeoutError("derive")

    monkeypatch.setattr(service._remote_module_host, "begin", derive_failed)
    with pytest.raises(TimeoutError):
        await service.open_remote_module_attempt(
            peer, RemoteModulePreparationBeginRequestV1(authority=first, budget_seconds=30)
        )

    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
async def test_execute_refused_after_anchoring_is_settled(tmp_path: Path) -> None:
    service, repository, remote, peer, _first, _prepare = await _failed_prepare_service(
        tmp_path, RuntimeError("unused")
    )
    repository.remote_attempt = None
    service._lanes.clear()  # type: ignore[attr-defined]

    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_remote_module_preparation(peer, remote)

    assert refused.value.category == "superseded"
    assert repository.records[-1].phase is JournalPhase.TERMINAL


@pytest.mark.anyio
async def test_latched_prepare_failure_stays_unresolved(tmp_path: Path) -> None:
    """A failure the provider did not abandon (power restore failed) keeps the op unresolved."""
    failure = CategorizedError(
        "appliance failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"completion": "failed-after-mutation"},
    )
    service, repository, remote, peer, _first, _prepare = await _failed_prepare_service(
        tmp_path, failure
    )
    with pytest.raises(CategorizedError):
        await service.execute_remote_module_preparation(peer, remote)
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
async def test_remote_lifecycle_reopens_completion_only_for_current_authenticated_binding(
    tmp_path: Path,
) -> None:
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover().model_copy(
        update={
            "provider_kind": "remote-libvirt",
            "purpose": "recover",
            "operation": AuthorityOperation.RECOVER,
        }
    )
    repository = _Repository(peer, takeover)
    request = AuthorityMutationRequestV1.model_validate(
        _mutation(takeover).model_dump(mode="python", by_alias=True)
        | {"purpose": "recover", "operation": "recover"}
    )
    remote = RemoteModuleLifecycleRequestV1(
        authority=request,
        action="restore",
        budget_seconds=30,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    completed: RemoteModuleLifecycleResponseV1 | None = None

    class Host:
        async def begin(self, *_args: object) -> object:
            raise AssertionError("unexpected begin")

        async def execute(self, *_args: object) -> object:
            raise AssertionError("unexpected preparation")

        async def observe_lifecycle(
            self, candidate: RemoteModuleLifecycleRequestV1
        ) -> RemoteModuleLifecycleResponseV1 | None:
            assert candidate == remote
            return completed

        async def execute_lifecycle(
            self, candidate: RemoteModuleLifecycleRequestV1
        ) -> RemoteModuleLifecycleResponseV1:
            nonlocal calls, completed
            assert candidate == remote
            assert candidate.budget_seconds == 30
            calls += 1
            entered.set()
            await release.wait()
            completed = cast(RemoteModuleLifecycleResponseV1, object())
            return completed

    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=_Adapter(),
        remote_module_host=cast(Any, Host()),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    trusted = await repository.resolve_current_candidate(peer, request)
    assert trusted is not None
    assert service._binding_matches(trusted, request)
    first = asyncio.create_task(service.execute_remote_module_lifecycle(peer, remote))
    try:
        await asyncio.wait_for(entered.wait(), 1)
    except TimeoutError:
        await first
        raise
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(service.execute_remote_module_lifecycle(peer, remote))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    assert await second is completed
    assert calls == 1

    repository.current = False
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_remote_module_lifecycle(peer, remote)
    assert calls == 1


@pytest.mark.anyio
async def test_teardown_recovery_keeps_ordinary_observe_adapter_compatible(
    tmp_path: Path,
) -> None:
    service, _repository, adapter, _peer, takeover = _service(tmp_path)
    values = _mutation(takeover).model_dump(mode="json", by_alias=True)
    request = type(_mutation(takeover)).model_validate(
        values | {"purpose": "teardown", "operation": "teardown"}
    )
    record = service._record(request, [], JournalPhase.MUTATION_STARTED)

    observation = await service._recovery_observation(request, record, [record])

    assert observation.category == "target"
    assert adapter.calls == ["observe"]


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
@pytest.mark.parametrize("case", ["inactive-peer", "stale-binding", "cross-system"])
async def test_mutation_rejects_before_caller_journal_construction(
    tmp_path: Path, case: str
) -> None:
    _unused, repository, adapter, peer, takeover = _service(tmp_path)
    repository.current = case == "cross-system"
    request = _mutation(takeover)
    selected_peer: AuthenticatedPeer | None = peer
    if case == "inactive-peer":
        selected_peer = None
    elif case == "cross-system":
        request = request.model_copy(update={"system_id": uuid4()})
    journal_calls = 0

    def forbidden_journal(_system_id: object) -> FileAuthorityJournal:
        nonlocal journal_calls
        journal_calls += 1
        raise AssertionError("caller-derived journal path was accessed")

    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=forbidden_journal,
        adapter=adapter,
    )
    category = "unauthenticated" if case == "inactive-peer" else "superseded"
    with pytest.raises(AuthorityServiceError, match=category):
        await service.execute_mutation(selected_peer, request)

    assert journal_calls == 0
    assert adapter.calls == []
    assert service._lanes == {}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_stored_operation_drift_rejects_before_journal_lane_or_provider(
    tmp_path: Path,
) -> None:
    _unused, repository, adapter, peer, takeover = _service(tmp_path)
    repository.current = True
    repository.operation_override = AuthorityOperation.DEADLINE
    journal_calls = 0

    def forbidden_journal(_system_id: object) -> FileAuthorityJournal:
        nonlocal journal_calls
        journal_calls += 1
        raise AssertionError("operation drift reached the journal")

    service = ExternalBootAuthorityService(
        repository=repository, journal_factory=forbidden_journal, adapter=adapter
    )
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_mutation(peer, _mutation(takeover))
    assert (journal_calls, adapter.calls, service._lanes) == (0, [], {})


@pytest.mark.anyio
async def test_hostile_mutation_cardinality_stops_before_journal_and_lane(
    tmp_path: Path,
) -> None:
    _unused, repository, adapter, peer, takeover = _service(tmp_path)
    repository.current = True
    journal_calls = 0

    def forbidden_journal(_system_id: object) -> FileAuthorityJournal:
        nonlocal journal_calls
        journal_calls += 1
        raise AssertionError("hostile journal path was accessed")

    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=forbidden_journal,
        adapter=adapter,
    )
    template = _mutation(takeover)
    for index in range(100):
        hostile = template.model_copy(
            update={
                "system_id": uuid4(),
                "provider_kind": f"hostile-provider-{index}",
                "authority_instance": f"hostile-instance-{index}",
            }
        )
        with pytest.raises(AuthorityServiceError, match="superseded"):
            await service.execute_mutation(peer, hostile)

    assert journal_calls == 0
    assert adapter.calls == []
    assert service._lanes == {}
    assert service.metrics.rejections == {("untrusted", "unresolved", "superseded"): 100}
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
async def test_terminal_operation_retry_returns_receipt_without_provider_access(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    mutation = _mutation(takeover)

    first = await service.execute_mutation(peer, mutation)
    second = await service.execute_mutation(
        peer, mutation.model_copy(update={"attempt_id": uuid4()})
    )

    assert second == first
    assert adapter.calls == ["commit:activate", "observe"]


@pytest.mark.anyio
async def test_mutation_reuses_one_validated_journal_scan_across_checkpoints(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    template = _mutation(takeover)
    for index in range(8):
        history_request = template.model_copy(
            update={"operation_identity": f"history-{index}", "attempt_id": uuid4()}
        )
        await service.execute_mutation(peer, history_request)
    path = tmp_path / f"{takeover.system_id}.journal"
    factory_calls = 0
    load_calls = 0
    validation_calls = 0
    history_length = len(repository.records)

    class CountingJournal(FileAuthorityJournal):
        @staticmethod
        def _prepare_record(state: Any, record: JournalRecordV1) -> Any:
            nonlocal validation_calls
            validation_calls += 1
            return FileAuthorityJournal._prepare_record(state, record)

        def load(self, *, deadline: float | None = None) -> tuple[JournalRecordV1, ...]:
            nonlocal load_calls
            load_calls += 1
            return super().load(deadline=deadline)

    def journal_factory(_system_id: object) -> FileAuthorityJournal:
        nonlocal factory_calls
        factory_calls += 1
        return CountingJournal(path.parent, path.name)

    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=journal_factory,
        adapter=adapter,
    )
    request = template.model_copy(
        update={"operation_identity": "after-restart", "attempt_id": uuid4()}
    )
    await restarted.execute_mutation(peer, request)

    assert factory_calls == 1
    assert load_calls == 1
    assert validation_calls == history_length + 5
    assert adapter.calls[-2:] == ["commit:activate", "observe"]


@pytest.mark.anyio
async def test_mutation_detects_external_rewrite_before_next_phase_checkpoint(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    adapter.release.clear()
    path = tmp_path / f"{takeover.system_id}.journal"
    task = asyncio.create_task(service.execute_mutation(peer, _mutation(takeover)))
    await adapter.entered.wait()
    before = path.read_bytes()
    rewritten = before.replace(b'"host-a"', b'"host-b"', 1)
    assert len(rewritten) == len(before)
    path.write_bytes(rewritten)
    adapter.release.set()

    with pytest.raises(ValueError, match="changed since validation"):
        await task

    assert path.read_bytes() == rewritten
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED
    assert adapter.calls == ["commit:activate"]


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
async def test_shutdown_drains_started_lane_before_closing_adapter(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.release.clear()
    mutation = asyncio.create_task(service.execute_mutation(peer, _mutation(request)))
    await adapter.entered.wait()

    shutdown = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert not adapter.closed
    shutdown.cancel()
    await asyncio.sleep(0)
    shutdown.cancel()
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert not adapter.closed

    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_mutation(peer, _mutation(request))
    adapter.release.set()
    await mutation
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert shutdown.cancelling() == 2
    assert repository.records[-1].phase is JournalPhase.TERMINAL
    assert adapter.closed


@pytest.mark.anyio
async def test_shutdown_closes_adapter_when_system_service_close_fails(tmp_path: Path) -> None:
    service, _repository, adapter, _peer, _request = _service(tmp_path)

    class FailingSystemService:
        async def close(self) -> None:
            raise RuntimeError("system service close failed")

    service._system_service = cast(Any, FailingSystemService())  # noqa: SLF001

    with pytest.raises(RuntimeError, match="system service close failed"):
        await service.close()

    assert adapter.closed


@pytest.mark.anyio
async def test_readiness_requires_exact_local_and_trusted_head(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
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
    rejection = next(
        record for record in caplog.records if record.message == "authority recovery rejected"
    )
    assert "provider_kind" not in rejection.__dict__
    assert "authority_instance" not in rejection.__dict__


def _rejection_records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.message == message]


@pytest.mark.anyio
async def test_readiness_rejection_logs_category_and_bounded_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _, peer, request = _service(tmp_path)
    repository.head = None
    await service.acknowledge_takeover(peer, request)

    async def refuse(*_: Any) -> Any:
        raise AuthorityServiceError("journal_conflict", reason="orphan_digest_changed")

    monkeypatch.setattr(service, "_recover", refuse)
    assert not await service.readiness(peer, request)

    (record,) = _rejection_records(caplog, "authority recovery rejected")
    assert (record.__dict__["category"], record.__dict__["reason"]) == (
        "journal_conflict",
        "orphan_digest_changed",
    )


@pytest.mark.anyio
async def test_readiness_oserror_logs_class_name_never_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, peer, request = _service(tmp_path)

    async def fail(*_: Any) -> Any:
        raise OSError("/secret/path/payload")

    monkeypatch.setattr(service, "_recover", fail)
    assert not await service.readiness(peer, request)

    (record,) = _rejection_records(caplog, "authority recovery rejected")
    assert (record.__dict__["category"], record.__dict__["reason"]) == ("OSError", None)
    assert "/secret/path" not in repr(record.__dict__)


class _RefusingOrphans:
    def set_serializer(self, serializer: Any) -> None:
        pass

    async def resolve_recovery_orphan(self, peer: Any, request: Any) -> Any:
        raise AuthorityServiceError("journal_conflict", reason="orphan_system_mismatch")


@pytest.mark.anyio
async def test_refused_orphan_disposition_logs_category_and_reason_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, _, _, peer, _ = _service(tmp_path)
    service._recovery_orphans = _RefusingOrphans()  # type: ignore[assignment]  # noqa: SLF001
    orphan_request = AuthorityRecoveryOrphanDispositionRequestV1(
        request_id=uuid4(), job_id=uuid4(), job_attempt=1
    )
    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await service.resolve_recovery_orphan(peer, orphan_request)

    (record,) = _rejection_records(caplog, "authority recovery orphan rejected")
    assert (record.__dict__["category"], record.__dict__["reason"]) == (
        "journal_conflict",
        "orphan_system_mismatch",
    )
    assert (record.__dict__["provider_kind"], record.__dict__["authority_instance"]) == (
        "untrusted",
        "unresolved",
    )


@pytest.mark.anyio
async def test_readiness_fails_closed_on_a_corrupt_stored_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The journal's binding rebuild raises ServerFaultError on a corrupt row (#3009).
    service, repository, _, peer, request = _service(tmp_path)

    async def _corrupt(*_: object) -> None:
        raise ServerFaultError("stored ExternalBootPlan failed validation")

    monkeypatch.setattr(repository, "resolve_allocating", _corrupt)
    assert not await service.readiness(peer, request)
    assert service.metrics.recovery_failures == {("untrusted", "unresolved"): 1}


@pytest.mark.anyio
async def test_unauthenticated_readiness_cannot_allocate_metric_coordinates(
    tmp_path: Path,
) -> None:
    service, _, _, _, template = _service(tmp_path)
    for index in range(300):
        hostile = template.model_copy(
            update={
                "provider_kind": f"hostile-provider-{index}",
                "authority_instance": f"hostile-instance-{index}",
            }
        )
        assert not await service.readiness(None, hostile)

    assert service.metrics.recovery_failures == {("untrusted", "unresolved"): 300}
    assert service.metrics._coordinates == set()


@pytest.mark.anyio
async def test_failed_checkpoint_never_reaches_provider_and_fails_closed(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    repository.advance_status = "conflict"
    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await service.acknowledge_takeover(peer, request)
    assert adapter.calls == []
    assert repository.head is None
    # ADR-0584 amendment (#2793): the refused record is retracted, so the lane again equals
    # its (absent) trusted head instead of stranding an unanchored record.
    assert not (tmp_path / f"{request.system_id}.journal").exists()
    assert await service.readiness(peer, request)
    assert service.metrics.checkpoints == {}
    assert service.metrics.rejections == {
        (request.provider_kind, request.authority_instance, "journal_conflict"): 1
    }


@pytest.mark.anyio
async def test_failed_local_append_never_advances_checkpoint_or_provider(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    failing = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: _FailingAppendJournal(tmp_path, f"{system_id}.journal"),
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
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
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
        coordinate = (request.provider_kind, request.authority_instance)
        metrics.recovery_failed_labels(coordinate)
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
    metrics.reject_labels(registered, "superseded")
    metrics.reject_labels(registered, "journal_conflict")
    second = ("another-provider", "another-instance")
    metrics.reject_labels(second, "superseded")
    metrics.reject_labels(second, "provider_conflict")

    assert metrics.rejections == {
        (*registered, "superseded"): 1,
        (*registered, "journal_conflict"): 1,
        ("overflow", "overflow", "overflow"): 2,
    }
    assert {key[:2] for key in metrics.rejections} == {
        registered,
        ("overflow", "overflow"),
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
    journal = FileAuthorityJournal(tmp_path, f"{request.system_id}.journal")
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
async def test_finalize_failure_after_a_refusal_conflict_publishes_the_quarantine(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = await _finalize_fault_service(
        tmp_path, ProofBarrierAbort("aborted")
    )
    adapter.fail_commit = True
    adapter.observe_category = "conflict"

    with pytest.raises(AuthorityServiceError) as raised:
        await service.execute_mutation(peer, request)

    assert raised.value.category == "provider_conflict"
    assert repository.records[-1].phase is JournalPhase.TERMINAL
    assert [args[-1] for args in repository.published_cleanup_quarantines] == [(adapter.receipt,)]


@pytest.mark.anyio
async def test_takeover_during_a_refusal_observation_completes_under_its_binding(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.fail_commit = True
    adapter.observe_category = "conflict"
    adapter.observe_release.clear()
    mutation_task = asyncio.create_task(service.execute_mutation(peer, _mutation(request)))
    await adapter.observe_entered.wait()
    successor = request.model_copy(
        update={"authority_id": uuid4(), "generation": 2, "operation_identity": "takeover-b"}
    )
    repository.allocating_request = successor
    takeover_task = asyncio.create_task(service.acknowledge_takeover(peer, successor))
    await asyncio.sleep(0)
    assert not takeover_task.done()
    adapter.observe_release.set()
    assert (await mutation_task).category == "conflict"
    assert (await takeover_task).generation == 2
    assert [record.phase for record in repository.records[-4:]] == [
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    ]
    assert {b.generation for b in repository.advance_bindings[-4:-1]} == {2}


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
            journal_factory=lambda system_id: FileAuthorityJournal(path.parent, path.name),
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
            journal_factory=lambda system_id: FileAuthorityJournal(path.parent, path.name),
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
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    if failure == "observe":
        with pytest.raises(AuthorityServiceError, match="provider_conflict"):
            await restarted.acknowledge_takeover(peer, successor)
        adapter.fail_observe = False
        restarted = ExternalBootAuthorityService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthorityJournal(
                tmp_path, f"{system_id}.journal"
            ),
            adapter=adapter,
        )
    acknowledgement = await restarted.acknowledge_takeover(peer, successor)
    assert acknowledgement.generation == 2
    assert repository.records[-1].phase is JournalPhase.TAKEOVER_ACKNOWLEDGED


@pytest.mark.anyio
async def test_exact_same_generation_takeover_ack_replays_without_new_journal_records(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    original = await service.acknowledge_takeover(peer, request)
    before = tuple(repository.records)

    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    replay = await restarted.acknowledge_takeover(peer, request)

    assert replay == original
    assert tuple(repository.records) == before


@pytest.mark.anyio
async def test_same_generation_takeover_ack_replay_requires_anchored_head(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    assert repository.head is not None
    repository.head = replace(repository.head, digest=_DIGEST_B)

    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await restarted.acknowledge_takeover(peer, request)


@pytest.mark.anyio
@pytest.mark.parametrize("restart", [False, True])
async def test_failed_commit_must_recover_before_later_same_generation_admission(
    tmp_path: Path, restart: bool
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    first = _mutation(takeover)
    adapter.fail_commit = True
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, first)
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED
    before_recovery = len(repository.records)
    adapter.fail_commit = False
    second = first.model_copy(
        update={
            "operation_identity": "mutation-after-uncertain-commit",
            "operation_digest": _DIGEST_B,
            "attempt_id": uuid4(),
        }
    )
    if restart:
        service = ExternalBootAuthorityService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthorityJournal(
                tmp_path, f"{system_id}.journal"
            ),
            adapter=adapter,
        )

    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, second)

    # The refusal's own re-observation (ADR-0805), then the recovery observation.
    assert adapter.calls == [f"commit:{first.operation}", "observe", "observe"]
    assert [record.phase for record in repository.records[before_recovery:]] == [
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]
    assert all(
        record.operation_identity == first.operation_identity
        for record in repository.records[before_recovery:]
    )
    observation = await service.execute_mutation(peer, second)
    assert observation.category == "target"
    assert adapter.calls[-2:] == [f"commit:{second.operation}", "observe"]


@pytest.mark.anyio
async def test_worker_death_recovers_every_suspended_phase_before_ack(tmp_path: Path) -> None:
    source, source_repository, _, peer, request = _service(tmp_path / "source")
    (tmp_path / "source").mkdir()
    await source.acknowledge_takeover(peer, request)
    source_repository.current = True
    mutation = _mutation(request).model_copy(update={"operation": AuthorityOperation.DEADLINE})
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
        path.chmod(0o600)
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
            journal_factory=lambda system_id, path=path: FileAuthorityJournal(
                path.parent, path.name
            ),
            adapter=adapter,
        )
        acknowledgement = await restarted.acknowledge_takeover(peer, successor)
        assert acknowledgement.generation == 2
        assert adapter.calls == expected_calls
        assert adapter.operations == (["deadline"] if expected_calls else [])
        assert repository.records[-1].phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
        terminal = next(
            record
            for record in reversed(repository.records)
            if record.phase is JournalPhase.TERMINAL
        )
        assert terminal.outcome == ("never-began" if phase is JournalPhase.ADMITTED else "target")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "category"),
    [(AuthorityOperation.RECOVER, "source"), (AuthorityOperation.CLEANUP, "absent")],
)
async def test_takeover_recovered_local_timed_release_phase_is_adopted_by_successor(
    tmp_path: Path,
    operation: AuthorityOperation,
    category: Literal["source", "absent"],
) -> None:
    peer = AuthenticatedPeer(uuid4())
    root = _takeover().model_copy(
        update={"purpose": "release", "operation": AuthorityOperation.RELEASE}
    )
    repository = _Repository(peer, root)

    class ReleaseAdapter(_Adapter):
        async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
            await super().observe(request)
            return AuthorityObservationV1(
                observation_id=uuid4(), category=category, composite_state=_DIGEST_A
            )

    adapter = ReleaseAdapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await service.acknowledge_takeover(peer, root)
    repository.current = True
    timing = LocalExternalBootTimingV1(accel="tcg", console_window_s=900, deadline_budget_s=1200)
    first = _mutation(root).model_copy(
        update={
            "operation": operation,
            "operation_identity": f"release-{operation.value}-1",
            "local_timing": timing,
        }
    )
    adapter.fail_commit = True
    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, first)
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED

    successor = root.model_copy(
        update={"authority_id": uuid4(), "generation": 2, "operation_identity": "takeover-b"}
    )
    repository.allocating_request = successor
    repository.current = False
    await service.acknowledge_takeover(peer, successor)
    recovered = next(
        record
        for record in reversed(repository.records)
        if record.phase is JournalPhase.TERMINAL
        and record.operation_identity == first.operation_identity
    )
    assert recovered.local_timing == timing

    repository.request = successor
    repository.current = True
    adapter.fail_commit = False
    retry = first.model_copy(
        update={
            "authority_id": successor.authority_id,
            "generation": 2,
            "attempt_id": uuid4(),
            "operation_identity": f"release-{operation.value}-2",
            "operation_digest": _DIGEST_A,
        }
    )
    observation = await service.execute_mutation(peer, retry)

    assert observation == recovered.observation
    assert adapter.calls.count(f"commit:{operation.value}") == 1


@pytest.mark.anyio
@pytest.mark.parametrize("divergence", ["missing", "operation", "phase"])
async def test_restart_rejects_divergent_trusted_continuation_before_recovery_access(
    tmp_path: Path, divergence: str
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.fail_commit = True
    mutation = _mutation(request).model_copy(update={"operation": AuthorityOperation.DEADLINE})
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
        corrupted = replace(suspended, operation=AuthorityOperation.FAIL)
    else:
        corrupted = replace(suspended, phase=JournalPhase.PROVIDER_RETURNED)
    repository.head = replace(repository.head, suspended_operation=corrupted)
    path = tmp_path / f"{request.system_id}.journal"
    before = path.read_bytes()
    fresh_adapter = _Adapter()
    restarted = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(path.parent, path.name),
        adapter=fresh_adapter,
    )
    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await restarted.acknowledge_takeover(peer, successor)
    assert fresh_adapter.calls == []
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_bounded_adapter_error_keeps_its_category_across_the_provider_seam(
    tmp_path: Path,
) -> None:
    """An adapter that already reached a bounded category must not be re-classified.

    The provider seam converts an unclassified exception into ``provider_conflict``. An
    ``AuthorityServiceError`` is not unclassified: downgrading a ``superseded`` verdict to
    ``provider_conflict`` would lose the fencing outcome ADR-0584 makes load-bearing, and
    would mislabel the rejection metric.
    """
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    mutation = _mutation(request)
    adapter.commit_error = AuthorityServiceError("superseded")

    with pytest.raises(AuthorityServiceError) as caught:
        await service.execute_mutation(peer, mutation)

    assert caught.value.category == "superseded"


@pytest.mark.anyio
async def test_unclassified_provider_failure_is_still_bounded_to_provider_conflict(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.fail_commit = True

    with pytest.raises(AuthorityServiceError) as caught:
        await service.execute_mutation(peer, _mutation(request))

    assert caught.value.category == "provider_conflict"


@pytest.mark.anyio
async def test_commit_receives_the_anchored_mutation_started_sequence_and_digest(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    mutation = _mutation(request)

    await service.execute_mutation(peer, mutation)

    [context] = adapter.commit_contexts
    started = [
        record
        for record in repository.records
        if record.phase is JournalPhase.MUTATION_STARTED
        and record.operation_identity == mutation.operation_identity
    ][-1]
    assert context.journal_sequence == started.sequence
    assert context.journal_digest == record_digest(started)
    assert context.attempt_id == started.attempt_id
    assert context.operation_identity == started.operation_identity
    assert context.commit_point is mutation.operation
    assert context.phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["sequence", "digest"])
async def test_a_head_disagreeing_under_the_same_operation_identity_refuses_the_commit(
    tmp_path: Path, field: str
) -> None:
    """The head is re-read because ``advance`` reports what was accepted, not what is held.

    One field at a time: dropping either comparison leaves the other case red.
    """
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    repository.head_override_after_phase = JournalPhase.MUTATION_STARTED
    repository.corrupt_head_field = cast(Literal["sequence", "digest"], field)

    with pytest.raises(AuthorityServiceError) as caught:
        await service.execute_mutation(peer, _mutation(request))

    assert caught.value.category == "journal_conflict"
    assert caught.value.reason == "head_unanchored_commit"
    assert adapter.commit_contexts == []
    assert not any(call.startswith("commit:") for call in adapter.calls)


@pytest.mark.anyio
async def test_a_head_moved_by_a_concurrent_takeover_still_lets_the_commit_finish(
    tmp_path: Path,
) -> None:
    """The scoping is proved, not asserted.

    ``acknowledge_takeover`` anchors its records under a different operation identity while an
    admitted mutation is in flight. Widen the check to compare the bare head and this goes red
    with ``journal_conflict``.
    """
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    repository.head_override_after_phase = JournalPhase.MUTATION_STARTED
    repository.head_operation_identity_override = "takeover-next"

    await service.execute_mutation(peer, _mutation(request))

    assert len(adapter.commit_contexts) == 1


class _FailingRetractJournal(FileAuthorityJournal):
    def retract(self, record: JournalRecordV1) -> None:
        raise OSError("injected retraction failure")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "category"), [("superseded", "superseded"), ("conflict", "journal_conflict")]
)
async def test_refused_anchor_retracts_its_record(
    tmp_path: Path,
    status: Literal["superseded", "conflict"],
    category: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    lane = tmp_path / f"{takeover.system_id}.journal"
    anchored = lane.read_bytes()
    repository.advance_status = status

    with caplog.at_level("WARNING"), pytest.raises(AuthorityServiceError, match=category):
        await service.execute_mutation(peer, _mutation(takeover))

    (rejected,) = [r for r in caplog.records if r.message == "authority request rejected"]
    assert rejected.__dict__["category"] == category
    assert rejected.__dict__["reason"] == ("checkpoint_refused" if status == "conflict" else None)
    labels = (takeover.provider_kind, takeover.authority_instance, category)
    assert set(service.metrics.rejections) == {labels}
    assert lane.read_bytes() == anchored
    assert adapter.calls == []
    assert [path.name.split(".")[1] for path in (tmp_path / "retracted").iterdir()] == [
        str(len(repository.records) + 1)
    ]


@pytest.mark.anyio
async def test_lane_anchors_again_after_a_superseded_refusal(tmp_path: Path) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    repository.advance_status = "superseded"
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_mutation(peer, _mutation(takeover))
    repository.advance_status = "advanced"

    observation = await service.execute_mutation(peer, _mutation(takeover))

    assert observation.category == "target"
    assert repository.records[-1].phase is JournalPhase.TERMINAL


@pytest.mark.anyio
async def test_anchor_error_keeps_the_unanchored_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, repository, _adapter, peer, takeover = _service(tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    lane = tmp_path / f"{takeover.system_id}.journal"
    anchored = lane.read_bytes()

    async def lost_advance(*_args: object) -> str:
        raise RuntimeError("advance outcome unknown")

    monkeypatch.setattr(repository, "advance", lost_advance)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        await service.execute_mutation(peer, _mutation(takeover))

    assert lane.read_bytes().startswith(anchored)
    assert len(lane.read_bytes().splitlines()) == len(anchored.splitlines()) + 1
    assert not (tmp_path / "retracted").exists()


@pytest.mark.anyio
async def test_retraction_failure_still_raises_the_refusal(tmp_path: Path) -> None:
    _unused, repository, adapter, peer, takeover = _service(tmp_path)
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: _FailingRetractJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    lane = tmp_path / f"{takeover.system_id}.journal"
    anchored = lane.read_bytes()
    repository.advance_status = "conflict"

    with pytest.raises(AuthorityServiceError, match="journal_conflict"):
        await service.execute_mutation(peer, _mutation(takeover))

    assert len(lane.read_bytes().splitlines()) == len(anchored.splitlines()) + 1


@pytest.mark.anyio
@pytest.mark.parametrize("bounded", [False, True], ids=["unclassified", "bounded"])
async def test_refusal_observed_as_conflict_is_journaled_and_answered(
    tmp_path: Path, bounded: bool
) -> None:
    """ADR-0805: a refused activation whose provider state is a third identity is a conflict."""
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    if bounded:
        adapter.commit_error = AuthorityServiceError("provider_conflict")
    else:
        adapter.fail_commit = True
    adapter.observe_category = "conflict"

    observation = await service.execute_mutation(peer, _mutation(request))

    assert observation.category == "conflict"
    assert adapter.calls == ["commit:activate", "observe"]
    terminal = repository.records[-1]
    assert [record.phase for record in repository.records][-4:] == [
        JournalPhase.MUTATION_STARTED,
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]
    assert terminal.outcome == "conflict"
    assert terminal.observation == observation


@pytest.mark.anyio
async def test_refusal_answered_as_conflict_finalizes_the_provider(tmp_path: Path) -> None:
    """The refused mutation had started, so its finalize runs as it does after a commit."""
    _unused, repository, _plain, peer, takeover = _service(tmp_path)
    adapter = _FinalizingAdapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    adapter.fail_commit = True
    adapter.observe_category = "conflict"

    observation = await service.execute_mutation(peer, _mutation(takeover))

    assert observation.category == "conflict"
    assert adapter.calls == ["commit:activate", "observe", "finalize"]


@pytest.mark.anyio
@pytest.mark.parametrize("category", ["source", "target", "mixed", "unreadable", None])
async def test_refusal_observed_without_conflict_stays_the_refusal(
    tmp_path: Path, category: Literal["source", "target", "mixed", "unreadable"] | None
) -> None:
    """Only a third identity parks; ``mixed`` may still resume from durable phase evidence."""
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.fail_commit = True
    if category is None:
        adapter.fail_observe = True
    else:
        adapter.observe_category = category

    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, _mutation(request))

    assert adapter.calls == ["commit:activate", "observe"]
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
async def test_superseded_refusal_is_not_reobserved(tmp_path: Path) -> None:
    service, repository, adapter, peer, request = _service(tmp_path)
    await service.acknowledge_takeover(peer, request)
    repository.current = True
    adapter.commit_error = AuthorityServiceError("superseded")
    adapter.observe_category = "conflict"

    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_mutation(peer, _mutation(request))

    assert adapter.calls == ["commit:activate"]


@pytest.mark.anyio
async def test_cleanup_refusal_is_not_reobserved(tmp_path: Path) -> None:
    peer = AuthenticatedPeer(uuid4())
    root = _takeover().model_copy(
        update={"purpose": "release", "operation": AuthorityOperation.RELEASE}
    )
    repository = _Repository(peer, root)
    adapter = _Adapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await service.acknowledge_takeover(peer, root)
    repository.current = True
    adapter.fail_commit = True
    adapter.observe_category = "conflict"
    cleanup = _mutation(root).model_copy(
        update={"operation": AuthorityOperation.CLEANUP, "operation_identity": "release-cleanup"}
    )

    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, cleanup)

    assert adapter.calls == ["commit:cleanup"]
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


class _ShutdownBeforeProvider(_RecordingProofCheckpoint):
    async def checkpoint(
        self,
        system_id: UUID,
        run_id: UUID,
        operation: AuthorityOperation,
        checkpoint: Literal["before-provider", "after-provider", "before-finalize"],
    ) -> None:
        await super().checkpoint(system_id, run_id, operation, checkpoint)
        if checkpoint == "before-provider":
            raise ProofBarrierShutdown("authority shutting down")


@pytest.mark.anyio
async def test_barrier_shutdown_before_provider_is_not_reobserved(tmp_path: Path) -> None:
    _unused, repository, adapter, peer, takeover = _service(tmp_path)
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
        proof_checkpoint=_ShutdownBeforeProvider(adapter.calls),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    adapter.observe_category = "conflict"

    with pytest.raises(AuthorityServiceError, match="provider_conflict"):
        await service.execute_mutation(peer, _mutation(takeover))

    assert adapter.calls == ["before-provider"]
