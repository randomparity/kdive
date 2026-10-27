"""ADR-0583: PREPARE refuses a held System before its first stop (#3353)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.db.external_boot_authority_journal import PrestopHolders
from kdive.domain.remote_module_attempt_preparation import (
    ModuleAttemptObligationReceiptV1,
    ModuleAttemptPreparationRequestV1,
)
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityPreparationMutationRequestV1,
    AuthorityTakeoverRequestV1,
    JournalPhase,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
    ExternalBootAuthorityService,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModulePreparationBeginRequestV1,
    RemoteModulePreparationBeginResponseV1,
    RemoteModuleVolumePreparationRequestV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
)
from tests.providers.external_boot_authority.service_support import (
    _Adapter,
    _Repository,
    _takeover,
)
from tests.support.external_boot_plan import external_boot_plan

_HELD: list[PrestopHolders] = ["session_active", "job_active"]


def _prepare(
    takeover: AuthorityTakeoverRequestV1, operation: str = "prepare"
) -> AuthorityPreparationMutationRequestV1:
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    return AuthorityPreparationMutationRequestV1(
        **takeover.model_dump(
            mode="python",
            by_alias=True,
            exclude={"operation", "operation_identity", "operation_digest", "plan_identity"},
        ),
        operation=operation,
        operation_identity=f"{operation}-op",
        operation_digest="sha256:" + "c" * 64,
        plan_identity=plan.identity,
        attempt_id=uuid4(),
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(),
        plan=plan,
    )


async def _local_lane(
    tmp_path: Path,
) -> tuple[ExternalBootAuthorityService, _Repository, _Adapter, AuthenticatedPeer, Any]:
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover()
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    takeover = takeover.model_copy(update={"plan_identity": plan.identity})
    repository = _Repository(peer, takeover)
    adapter = _Adapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    return service, repository, adapter, peer, takeover


@pytest.mark.anyio
@pytest.mark.parametrize("holders", _HELD)
async def test_local_prepare_refuses_a_held_system_before_any_provider_call(
    tmp_path: Path, holders: PrestopHolders
) -> None:
    service, repository, adapter, peer, takeover = await _local_lane(tmp_path)
    request = cast(Any, _prepare(takeover))
    repository.prestop_holders = holders
    records = len(repository.records)

    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_mutation(peer, request)

    assert refused.value.category == "system_held"
    assert adapter.calls == []
    assert len(repository.records) == records

    repository.prestop_holders = "clear"
    await service.execute_mutation(peer, request)
    assert adapter.calls == ["commit:prepare", "observe"]


@pytest.mark.anyio
async def test_local_prepare_proceeds_when_the_system_is_clear(tmp_path: Path) -> None:
    service, repository, adapter, peer, takeover = await _local_lane(tmp_path)

    await service.execute_mutation(peer, cast(Any, _prepare(takeover)))

    assert adapter.calls == ["commit:prepare", "observe"]
    assert len(repository.prestop_calls) == 1


@pytest.mark.anyio
async def test_local_prepare_refuses_an_unreadable_answer_without_mutating(
    tmp_path: Path,
) -> None:
    service, repository, adapter, peer, takeover = await _local_lane(tmp_path)
    repository.prestop_error = RuntimeError("connection reset")

    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_mutation(peer, cast(Any, _prepare(takeover)))

    assert refused.value.category == "provider_conflict"
    assert adapter.calls == []
    assert JournalPhase.ADMITTED not in {record.phase for record in repository.records}


@pytest.mark.anyio
async def test_local_prepare_refuses_an_authority_that_no_longer_binds(tmp_path: Path) -> None:
    service, repository, adapter, peer, takeover = await _local_lane(tmp_path)
    repository.prestop_holders = None

    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_mutation(peer, cast(Any, _prepare(takeover)))

    assert refused.value.category == "superseded"
    assert adapter.calls == []


@pytest.mark.anyio
async def test_materialize_never_consults_the_pre_stop_recheck(tmp_path: Path) -> None:
    service, repository, adapter, peer, takeover = await _local_lane(tmp_path)
    repository.prestop_holders = "session_active"

    await service.execute_mutation(peer, cast(Any, _prepare(takeover, "materialize")))

    assert repository.prestop_calls == []
    assert adapter.calls == ["commit:materialize", "observe"]


class _Reached(Exception):
    pass


class _RemoteHost:
    def __init__(self) -> None:
        self.begun = 0
        self.executed = 0

    async def begin(
        self,
        authority: AuthorityPreparationMutationRequestV1,
        preparation: ModuleAttemptPreparationRequestV1,
        budget_seconds: int,
    ) -> RemoteModulePreparationBeginResponseV1:
        self.begun += 1
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

    started = False

    def execution_started(self, request: object) -> bool:
        return self.started

    async def execute(self, request: object) -> object:
        self.executed += 1
        if self.started:
            raise _Reached
        raise AssertionError("a refused PREPARE must not reach the provider host")


async def _remote_lane(
    tmp_path: Path,
) -> tuple[
    ExternalBootAuthorityService,
    _Repository,
    _RemoteHost,
    AuthenticatedPeer,
    AuthorityPreparationMutationRequestV1,
]:
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover().model_copy(update={"provider_kind": "remote-libvirt"})
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    takeover = takeover.model_copy(update={"plan_identity": plan.identity})
    repository = _Repository(peer, takeover)
    repository.remote_attempt = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=takeover.system_id, run_id=takeover.run_id, operation_nonce="a" * 32
        )
    )
    host = _RemoteHost()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=_Adapter(),
        remote_module_host=cast(Any, host),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    return service, repository, host, peer, _prepare(takeover)


@pytest.mark.anyio
@pytest.mark.parametrize("holders", _HELD)
async def test_remote_prepare_begin_refuses_a_held_system_before_journaling(
    tmp_path: Path, holders: PrestopHolders
) -> None:
    service, repository, host, peer, request = await _remote_lane(tmp_path)
    repository.prestop_holders = holders
    records = len(repository.records)

    with pytest.raises(AuthorityServiceError) as refused:
        await service.open_remote_module_attempt(
            peer, RemoteModulePreparationBeginRequestV1(authority=request, budget_seconds=30)
        )

    assert refused.value.category == "system_held"
    assert (host.begun, repository.remote_attempt_calls) == (0, [])
    assert len(repository.records) == records


@pytest.mark.anyio
async def test_remote_prepare_begin_refuses_an_unreadable_answer_before_journaling(
    tmp_path: Path,
) -> None:
    service, repository, host, peer, request = await _remote_lane(tmp_path)
    repository.prestop_error = RuntimeError("connection reset")
    records = len(repository.records)

    with pytest.raises(AuthorityServiceError) as refused:
        await service.open_remote_module_attempt(
            peer, RemoteModulePreparationBeginRequestV1(authority=request, budget_seconds=30)
        )

    assert refused.value.category == "provider_conflict"
    assert (host.begun, len(repository.records)) == (0, records)


@pytest.mark.anyio
async def test_remote_prepare_execute_rechecks_immediately_before_the_host_stops(
    tmp_path: Path,
) -> None:
    service, repository, host, peer, request = await _remote_lane(tmp_path)
    await service.open_remote_module_attempt(
        peer, RemoteModulePreparationBeginRequestV1(authority=request, budget_seconds=30)
    )
    assert len(repository.prestop_calls) == 1
    repository.prestop_holders = "session_active"
    begun = await service.open_remote_module_attempt(
        peer, RemoteModulePreparationBeginRequestV1(authority=request, budget_seconds=30)
    )
    remote = RemoteModuleVolumePreparationRequestV1(authority=request, operation=begun.operation)

    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_remote_module_preparation(peer, remote)

    assert refused.value.category == "system_held"
    assert host.executed == 0


@pytest.mark.anyio
async def test_remote_prepare_refused_at_the_pre_stop_recheck_leaves_nothing_unsettled(
    tmp_path: Path,
) -> None:
    service, repository, host, peer, request = await _remote_lane(tmp_path)
    begun = await service.open_remote_module_attempt(
        peer, RemoteModulePreparationBeginRequestV1(authority=request, budget_seconds=30)
    )
    remote = RemoteModuleVolumePreparationRequestV1(authority=request, operation=begun.operation)
    repository.prestop_holders = "session_active"

    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_remote_module_preparation(peer, remote)

    assert refused.value.category == "system_held"
    assert [record.phase for record in repository.records][-3:] == [
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]
    assert repository.records[-1].outcome == "source"

    repository.prestop_holders = "clear"
    retry = request.model_copy(update={"operation_identity": "prepare-op-2"})
    await service.open_remote_module_attempt(
        peer, RemoteModulePreparationBeginRequestV1(authority=retry, budget_seconds=30)
    )
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


async def _refused_remote_prepare(
    tmp_path: Path, holders: PrestopHolders | None = "session_active"
) -> tuple[
    ExternalBootAuthorityService,
    _Repository,
    _RemoteHost,
    AuthenticatedPeer,
    RemoteModuleVolumePreparationRequestV1,
]:
    service, repository, host, peer, request = await _remote_lane(tmp_path)
    begun = await service.open_remote_module_attempt(
        peer, RemoteModulePreparationBeginRequestV1(authority=request, budget_seconds=30)
    )
    remote = RemoteModuleVolumePreparationRequestV1(authority=request, operation=begun.operation)
    repository.prestop_holders = holders
    return service, repository, host, peer, remote


@pytest.mark.anyio
async def test_replay_of_a_refused_prepare_is_refused_again_without_the_host(
    tmp_path: Path,
) -> None:
    service, repository, host, peer, remote = await _refused_remote_prepare(tmp_path)
    with pytest.raises(AuthorityServiceError):
        await service.execute_remote_module_preparation(peer, remote)
    records = len(repository.records)

    with pytest.raises(AuthorityServiceError) as held:
        await service.execute_remote_module_preparation(peer, remote)
    assert held.value.category == "system_held"

    repository.prestop_holders = "clear"
    with pytest.raises(AuthorityServiceError) as clear:
        await service.execute_remote_module_preparation(peer, remote)
    assert clear.value.category == "provider_conflict"
    assert (host.executed, len(repository.records)) == (0, records)


@pytest.mark.anyio
async def test_unreadable_pre_stop_answer_is_settled_and_still_refused(tmp_path: Path) -> None:
    service, repository, host, peer, remote = await _refused_remote_prepare(tmp_path)
    repository.prestop_error = RuntimeError("connection reset")

    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_remote_module_preparation(peer, remote)

    assert refused.value.category == "provider_conflict"
    assert repository.records[-1].phase is JournalPhase.TERMINAL
    assert host.executed == 0


@pytest.mark.anyio
async def test_superseded_pre_stop_answer_is_not_settled(tmp_path: Path) -> None:
    service, repository, _host, peer, remote = await _refused_remote_prepare(tmp_path, None)

    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_remote_module_preparation(peer, remote)

    assert refused.value.category == "superseded"
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


@pytest.mark.anyio
async def test_settle_failure_still_raises_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _host, peer, remote = await _refused_remote_prepare(tmp_path)

    async def refuse(*_args: object) -> None:
        raise OSError("journal unavailable")

    monkeypatch.setattr(service, "_anchor", refuse)
    with pytest.raises(AuthorityServiceError) as refused:
        await service.execute_remote_module_preparation(peer, remote)

    assert refused.value.category == "system_held"
    assert repository.records[-1].phase is JournalPhase.MUTATION_STARTED


class _AdoptingAdapter(_Adapter):
    async def adopt_preparation(self, *arguments: object) -> None:
        self.calls.append("adopt")


@pytest.mark.anyio
async def test_prepare_that_adopts_a_predecessor_is_not_rechecked(tmp_path: Path) -> None:
    peer = AuthenticatedPeer(uuid4())
    takeover = _takeover()
    plan = external_boot_plan(takeover.system_id, takeover.run_id)
    takeover = takeover.model_copy(update={"plan_identity": plan.identity})
    repository = _Repository(peer, takeover)
    adapter = _AdoptingAdapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    await service.execute_mutation(peer, cast(Any, _prepare(takeover)))

    successor = takeover.model_copy(
        update={"authority_id": uuid4(), "generation": 2, "operation_identity": "takeover-b"}
    )
    repository.current = False
    repository.request = repository.allocating_request = successor
    await service.acknowledge_takeover(peer, successor)
    repository.current = True
    repository.prestop_holders = "session_active"
    calls = len(repository.prestop_calls)
    adopted = _prepare(successor).model_copy(update={"operation_identity": "prepare-op-2"})

    await service.execute_mutation(peer, cast(Any, adopted))

    assert "adopt" in adapter.calls
    assert len(repository.prestop_calls) == calls


@pytest.mark.anyio
async def test_remote_prepare_execute_does_not_refuse_a_resume_the_host_already_started(
    tmp_path: Path,
) -> None:
    service, repository, host, peer, request = await _remote_lane(tmp_path)
    begun = await service.open_remote_module_attempt(
        peer, RemoteModulePreparationBeginRequestV1(authority=request, budget_seconds=30)
    )
    remote = RemoteModuleVolumePreparationRequestV1(authority=request, operation=begun.operation)
    host.started = True
    repository.prestop_holders = "job_active"
    checks = len(repository.prestop_calls)

    with pytest.raises(_Reached):
        await service.execute_remote_module_preparation(peer, remote)

    assert host.executed == 1
    assert len(repository.prestop_calls) == checks
