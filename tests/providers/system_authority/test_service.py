"""Completion and recovery behavior for authority-owned System operations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest

from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.providers.ports.external_boot import RootSpecV1
from kdive.providers.system_authority import journal as journal_module
from kdive.providers.system_authority import service as service_module
from kdive.providers.system_authority.journal import (
    MAX_JOURNAL_BYTES,
    MAX_RECORDS_PER_JOURNAL,
    PROVISION_RECORD_CEILING,
    RECORDS_PER_ATTEMPT,
    TEARDOWN_RESERVE_RECORDS,
    FileAuthoritySystemJournal,
)
from kdive.providers.system_authority.protocol import (
    GENESIS_DIGEST,
    AuthoritySystemAbsenceFacts,
    AuthoritySystemAcknowledgementV1,
    AuthoritySystemCommitContextV1,
    AuthoritySystemJournalPhase,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemProofV1,
    AuthoritySystemProvisionFacts,
    AuthoritySystemProvisionSnapshot,
    AuthoritySystemTakeoverRequestV1,
    authority_system_record_digest,
)
from kdive.providers.system_authority.repository import (
    AuthoritySystemAdvanceResult,
    AuthoritySystemBinding,
    AuthoritySystemJournalHead,
    ResolvedAuthoritySystemOperation,
)
from kdive.providers.system_authority.service import (
    AuthoritySystemService,
    AuthoritySystemServiceError,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_PUBLIC_KEY = "ssh-ed25519 YWFhYQ== kdive-system"


def _profile() -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "linux-test",
            "provider": {
                "local-libvirt": {
                    "domain_xml_params": {},
                    "rootfs": {"kind": "local", "path": "/configured/root.qcow2"},
                }
            },
        }
    )


def _snapshot(
    system_id: UUID, allocation_id: UUID, resource_id: UUID
) -> AuthoritySystemProvisionSnapshot:
    profile = _profile()
    root = RootSpecV1(
        architecture="x86_64",
        root="/dev/vda2",
        arguments=("root=/dev/vda2",),
        authority="stage-inspection",
        source={"kind": "staged-image", "identity": _DIGEST_B},
    )
    import hashlib

    return AuthoritySystemProvisionSnapshot(
        system_id=system_id,
        allocation_id=allocation_id,
        resource_id=resource_id,
        project="proj",
        provider_kind="local-libvirt",
        resource_name="host-a",
        authority_instance="authority-a",
        profile=profile,
        profile_identity="sha256:" + profile_digest(profile),
        source_image_id=uuid4(),
        root_identity=_DIGEST_B,
        root_spec=root,
        bootstrap_public_key=_PUBLIC_KEY,
        bootstrap_identity="sha256:" + hashlib.sha256(_PUBLIC_KEY.encode()).hexdigest(),
    )


def _request(
    snapshot: AuthoritySystemProvisionSnapshot,
    *,
    generation: int = 1,
    authority_id: UUID | None = None,
) -> AuthoritySystemTakeoverRequestV1:
    return AuthoritySystemTakeoverRequestV1(
        system_id=snapshot.system_id,
        allocation_id=snapshot.allocation_id,
        resource_id=snapshot.resource_id,
        provider_kind=snapshot.provider_kind,
        resource_name=snapshot.resource_name,
        authority_instance=snapshot.authority_instance,
        profile_identity=snapshot.profile_identity,
        root_identity=snapshot.root_identity,
        bootstrap_identity=snapshot.bootstrap_identity,
        operation=AuthoritySystemOperation.PROVISION,
        operation_identity="provision-a",
        authority_id=authority_id or uuid4(),
        generation=generation,
        attempt_id=uuid4(),
        operation_digest=_DIGEST_A,
    )


def _mutation(request: AuthoritySystemTakeoverRequestV1) -> AuthoritySystemMutationRequestV1:
    return AuthoritySystemMutationRequestV1.model_validate(
        request.model_dump(mode="python", by_alias=True)
    )


class _Repository:
    def __init__(self, snapshot: AuthoritySystemProvisionSnapshot) -> None:
        self.snapshot = snapshot
        self.sequence = 0
        self.digest = GENESIS_DIGEST
        self.phase: AuthoritySystemJournalPhase | None = None
        self.record = None
        self.receipt: bytes | None = None
        self.allocating: AuthoritySystemTakeoverRequestV1 | None = None
        self.current: AuthoritySystemMutationRequestV1 | None = None
        self.fail_phase_once: AuthoritySystemJournalPhase | None = None

    def _resolved(
        self,
        request: AuthoritySystemTakeoverRequestV1 | AuthoritySystemMutationRequestV1,
        state: Literal["allocating", "current", "terminal"],
    ) -> ResolvedAuthoritySystemOperation:
        return ResolvedAuthoritySystemOperation(
            AuthoritySystemBinding(
                authority_id=request.authority_id,
                generation=request.generation,
                system_id=request.system_id,
                allocation_id=request.allocation_id,
                resource_id=request.resource_id,
                provider_kind=request.provider_kind,
                resource_name=request.resource_name,
                authority_instance=request.authority_instance,
                profile_identity=request.profile_identity,
                root_identity=request.root_identity,
                bootstrap_identity=request.bootstrap_identity,
                operation=request.operation.value,
                operation_identity=request.operation_identity,
                operation_digest=request.operation_digest,
                state=state,
            ),
            AuthoritySystemJournalHead(
                request.system_id, self.sequence, self.digest, self.phase, self.record
            ),
            self.snapshot,
            self.receipt,
        )

    async def resolve_allocating(
        self, peer_incarnation: str, request: AuthoritySystemTakeoverRequestV1
    ) -> ResolvedAuthoritySystemOperation | None:
        if peer_incarnation != "worker-a" or request != self.allocating:
            return None
        return self._resolved(request, "allocating")

    async def resolve_current(
        self,
        peer_incarnation: str,
        request: AuthoritySystemMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> ResolvedAuthoritySystemOperation | None:
        if (
            peer_incarnation != "worker-a"
            or request != self.current
            or acknowledgement_sequence > self.sequence
        ):
            return None
        return self._resolved(request, "terminal" if self.receipt else "current")

    async def advance_head(
        self,
        peer_incarnation: str,
        request: AuthoritySystemTakeoverRequestV1 | AuthoritySystemMutationRequestV1,
        *,
        expected_sequence: int,
        expected_digest: str,
        record,
        receipt: AuthoritySystemProofV1 | None = None,
    ) -> AuthoritySystemAdvanceResult:
        assert peer_incarnation == "worker-a"
        assert expected_sequence == self.sequence
        assert expected_digest == self.digest
        if self.fail_phase_once is record.phase:
            self.fail_phase_once = None
            return AuthoritySystemAdvanceResult("conflict", self.sequence, self.digest)
        self.sequence = record.sequence
        self.digest = authority_system_record_digest(record)
        self.phase = record.phase
        self.record = record
        if receipt is not None:
            from kdive.providers.system_authority.protocol import canonical_system_authority_bytes

            self.receipt = canonical_system_authority_bytes(receipt)
        return AuthoritySystemAdvanceResult("advanced", self.sequence, self.digest)


class _Provider:
    def __init__(self, *, completed_at: datetime | None = None) -> None:
        self.execute_calls = 0
        self.observe_calls = 0
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self.fail_execute = False
        self.completed_at = completed_at or datetime(2026, 9, 6, tzinfo=UTC)

    def _facts(self) -> AuthoritySystemProvisionFacts:
        return AuthoritySystemProvisionFacts(
            intent_identity=_DIGEST_A,
            domain_owned=True,
            root_storage_owned=True,
            boot_ready=True,
            bootstrap_ready=True,
            quarantine_retained=False,
            completed_at=self.completed_at,
            ssh_forward_port=41000,
        )

    async def execute_system_provision(self, request, context, snapshot):
        self.execute_calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail_execute:
            raise RuntimeError("simulated process stop")
        return self._facts()

    async def observe_system_provision(self, request, context, snapshot):
        self.observe_calls += 1
        return self._facts()

    async def execute_preactivation_teardown(
        self, request: AuthoritySystemMutationRequestV1, context: AuthoritySystemCommitContextV1
    ) -> AuthoritySystemAbsenceFacts:
        raise AssertionError("unexpected teardown")

    async def observe_preactivation_teardown(
        self, request: AuthoritySystemMutationRequestV1, context: AuthoritySystemCommitContextV1
    ) -> AuthoritySystemAbsenceFacts:
        raise AssertionError("unexpected teardown")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    (root / "system-operations").mkdir(mode=0o700)
    return root


async def _begin(
    service: AuthoritySystemService,
    repository: _Repository,
    request: AuthoritySystemTakeoverRequestV1,
) -> AuthoritySystemAcknowledgementV1:
    repository.allocating = request
    acknowledgement = await service.begin("worker-a", request)
    repository.current = _mutation(request)
    repository.allocating = None
    return acknowledgement


def test_cancellation_does_not_abandon_provider_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        provider = _Provider()
        provider.entered, provider.release = asyncio.Event(), asyncio.Event()
        root = _root(tmp_path)
        service = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=provider,
        )
        request = _request(snapshot)
        acknowledgement = await _begin(service, repository, request)
        caller = asyncio.create_task(
            service.execute("worker-a", _mutation(request), acknowledgement)
        )
        await provider.entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        provider.release.set()
        await service.close()
        assert repository.receipt is not None
        assert repository.phase is AuthoritySystemJournalPhase.TERMINAL
        assert provider.execute_calls == 1

    asyncio.run(scenario())


def test_close_closes_provider_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        provider = _Provider()
        events: list[str] = []
        root = _root(tmp_path)
        service = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=provider,
            close_provider=lambda: events.append("closed"),
        )
        await service.close()
        await service.close()
        assert events == ["closed"]

    asyncio.run(scenario())


def test_restart_after_mutation_started_observes_without_reexecution(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        failing_provider = _Provider()
        failing_provider.fail_execute = True
        request = _request(snapshot)
        first = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=failing_provider,
        )
        acknowledgement = await _begin(first, repository, request)
        with pytest.raises(RuntimeError, match="process stop"):
            await first.execute("worker-a", _mutation(request), acknowledgement)
        await first.close()

        recovery_provider = _Provider()
        recovered = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=recovery_provider,
        )
        response = await recovered.execute("worker-a", _mutation(request), acknowledgement)
        await recovered.close()
        assert response.proof.disposition == "provision-ready"
        assert response.proof.ssh_forward_port == 41000  # the proof carries the provider's port
        assert recovery_provider.execute_calls == 0
        assert recovery_provider.observe_calls == 1

    asyncio.run(scenario())


def test_one_file_ahead_record_is_replayed_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        request = _request(snapshot)
        provider = _Provider()
        first = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=provider,
        )
        acknowledgement = await _begin(first, repository, request)
        repository.fail_phase_once = AuthoritySystemJournalPhase.PROVIDER_RETURNED
        with pytest.raises(AuthoritySystemServiceError, match="journal-conflict"):
            await first.execute("worker-a", _mutation(request), acknowledgement)
        await first.close()

        recovery_provider = _Provider()
        recovered = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=recovery_provider,
        )
        response = await recovered.execute("worker-a", _mutation(request), acknowledgement)
        await recovered.close()
        assert response.proof.disposition == "provision-ready"
        assert recovery_provider.execute_calls == 0
        assert recovery_provider.observe_calls == 1

    asyncio.run(scenario())


def test_terminal_file_ahead_replays_only_exact_stable_provider_facts(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        request = _request(snapshot)
        first = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=_Provider(),
        )
        acknowledgement = await _begin(first, repository, request)
        repository.fail_phase_once = AuthoritySystemJournalPhase.TERMINAL
        with pytest.raises(AuthoritySystemServiceError, match="journal-conflict"):
            await first.execute("worker-a", _mutation(request), acknowledgement)
        await first.close()

        stable_provider = _Provider()
        recovered = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=stable_provider,
        )
        response = await recovered.execute("worker-a", _mutation(request), acknowledgement)
        await recovered.close()
        assert response.proof.disposition == "provision-ready"
        assert stable_provider.execute_calls == 0
        assert stable_provider.observe_calls == 1
        assert repository.receipt is not None

    asyncio.run(scenario())


def test_terminal_file_ahead_rejects_changed_recovery_observation(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        request = _request(snapshot)
        first = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=_Provider(completed_at=datetime(2026, 9, 6, tzinfo=UTC)),
        )
        acknowledgement = await _begin(first, repository, request)
        repository.fail_phase_once = AuthoritySystemJournalPhase.TERMINAL
        with pytest.raises(AuthoritySystemServiceError, match="journal-conflict"):
            await first.execute("worker-a", _mutation(request), acknowledgement)
        await first.close()

        changed_provider = _Provider(completed_at=datetime(2026, 9, 7, tzinfo=UTC))
        recovered = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=changed_provider,
        )
        with pytest.raises(AuthoritySystemServiceError, match="journal-conflict"):
            await recovered.execute("worker-a", _mutation(request), acknowledgement)
        await recovered.close()
        assert changed_provider.execute_calls == 0
        assert changed_provider.observe_calls == 1
        assert repository.receipt is None

    asyncio.run(scenario())


def test_successor_takeover_observes_started_predecessor_before_ack(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        first_request = _request(snapshot)
        first_provider = _Provider()
        first_provider.fail_execute = True
        first = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=first_provider,
        )
        first_ack = await _begin(first, repository, first_request)
        with pytest.raises(RuntimeError, match="process stop"):
            await first.execute("worker-a", _mutation(first_request), first_ack)
        await first.close()

        second_request = _request(snapshot, generation=2)
        repository.allocating = second_request
        observer = _Provider()
        successor = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=observer,
        )
        acknowledgement = await successor.begin("worker-a", second_request)
        await successor.close()
        assert observer.execute_calls == 0
        assert observer.observe_calls == 1
        assert acknowledgement.generation == 2
        assert repository.phase is AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED

    asyncio.run(scenario())


class _FailingProvisionProvider(_Provider):
    """A provider whose provision never completes and whose teardown always does."""

    def _facts(self) -> AuthoritySystemProvisionFacts:
        return AuthoritySystemProvisionFacts(
            intent_identity=_DIGEST_A,
            domain_owned=False,
            root_storage_owned=False,
            boot_ready=False,
            bootstrap_ready=False,
            quarantine_retained=True,
            completed_at=None,
        )

    async def execute_preactivation_teardown(self, request, context) -> AuthoritySystemAbsenceFacts:
        return await self.observe_preactivation_teardown(request, context)

    async def observe_preactivation_teardown(self, request, context) -> AuthoritySystemAbsenceFacts:
        return AuthoritySystemAbsenceFacts(
            intent_identity=_DIGEST_A,
            domain_absent=True,
            root_storage_absent=True,
            baseline_absent=True,
            private_intent_absent=True,
            quarantine_retained=False,
            completed_at=self.completed_at,
        )


async def _run_attempt(
    service: AuthoritySystemService,
    repository: _Repository,
    request: AuthoritySystemTakeoverRequestV1,
) -> None:
    repository.receipt = None  # the fake keeps one receipt; a new attempt starts without one
    acknowledgement = await _begin(service, repository, request)
    await service.execute("worker-a", _mutation(request), acknowledgement)


def test_persistent_provision_failure_leaves_room_for_teardown(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        service = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=_FailingProvisionProvider(),
        )
        records = lambda: repository.sequence  # noqa: E731 - head sequence is the record count
        generation = 0
        refused = False
        while not refused and generation < MAX_RECORDS_PER_JOURNAL:
            generation += 1
            before = records()
            try:
                await _run_attempt(service, repository, _request(snapshot, generation=generation))
            except AuthoritySystemServiceError as error:
                assert error.category == "journal-conflict"
                refused = True
            else:
                assert records() - before <= RECORDS_PER_ATTEMPT
        assert refused
        assert records() <= PROVISION_RECORD_CEILING
        assert records() > PROVISION_RECORD_CEILING - RECORDS_PER_ATTEMPT

        teardown = _request(snapshot, generation=generation + 1).model_copy(
            update={
                "operation": AuthoritySystemOperation.PREACTIVATION_TEARDOWN,
                "operation_identity": "teardown-a",
            }
        )
        await _run_attempt(service, repository, teardown)
        assert repository.phase is AuthoritySystemJournalPhase.TERMINAL
        assert repository.receipt is not None
        assert records() <= MAX_RECORDS_PER_JOURNAL
        await service.close()

    asyncio.run(scenario())


def test_journal_bounds_are_unchanged() -> None:
    assert MAX_RECORDS_PER_JOURNAL == 1024
    assert MAX_JOURNAL_BYTES == 16 * 1024 * 1024
    assert PROVISION_RECORD_CEILING + TEARDOWN_RESERVE_RECORDS == MAX_RECORDS_PER_JOURNAL


def test_started_attempt_resumes_above_the_provision_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        service = AuthoritySystemService(
            repository=repository,
            journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
            provider=_FailingProvisionProvider(),
        )
        request = _request(snapshot)
        repository.fail_phase_once = AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED
        repository.allocating = request
        with pytest.raises(AuthoritySystemServiceError):
            await service.begin("worker-a", request)
        monkeypatch.setattr(service_module, "PROVISION_RECORD_CEILING", 0)
        acknowledgement = await service.begin("worker-a", request)
        assert acknowledgement.attempt_id == request.attempt_id
        await service.close()

    asyncio.run(scenario())


_CRASHABLE_PHASES = (
    AuthoritySystemJournalPhase.WATERMARK_INSTALLED,
    AuthoritySystemJournalPhase.MUTATION_STARTED,
    AuthoritySystemJournalPhase.PROVIDER_RETURNED,
)


def _service(
    repository: _Repository, root: Path, provider: _Provider | None = None
) -> AuthoritySystemService:
    return AuthoritySystemService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthoritySystemJournal(root, system_id),
        provider=provider or _FailingProvisionProvider(),
    )


async def _crash_provision_at(
    service: AuthoritySystemService,
    repository: _Repository,
    request: AuthoritySystemTakeoverRequestV1,
    phase: AuthoritySystemJournalPhase,
) -> AuthoritySystemAcknowledgementV1 | None:
    """Run a provision attempt until the database refuses `phase`, leaving it in the file only."""
    repository.fail_phase_once = phase
    acknowledgement: AuthoritySystemAcknowledgementV1 | None = None
    with pytest.raises(AuthoritySystemServiceError, match="journal-conflict"):
        if phase is AuthoritySystemJournalPhase.WATERMARK_INSTALLED:
            await _begin(service, repository, request)
        else:
            acknowledgement = await _begin(service, repository, request)
            await service.execute("worker-a", _mutation(request), acknowledgement)
    return acknowledgement


def _teardown(
    snapshot: AuthoritySystemProvisionSnapshot, generation: int
) -> AuthoritySystemTakeoverRequestV1:
    return _request(snapshot, generation=generation).model_copy(
        update={
            "operation": AuthoritySystemOperation.PREACTIVATION_TEARDOWN,
            "operation_identity": "teardown-a",
        }
    )


def _abandoned(root: Path) -> list[Path]:
    directory = root / "system-operations" / "abandoned"
    return sorted(directory.iterdir()) if directory.exists() else []


@pytest.mark.parametrize("phase", _CRASHABLE_PHASES)
def test_teardown_succeeds_after_unconfirmed_provision_record(
    tmp_path: Path, phase: AuthoritySystemJournalPhase
) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        first = _service(repository, root)
        await _crash_provision_at(first, repository, _request(snapshot), phase)
        await first.close()
        confirmed = repository.sequence

        recovered = _service(repository, root)
        await _run_attempt(recovered, repository, _teardown(snapshot, 2))
        await recovered.close()
        assert repository.phase is AuthoritySystemJournalPhase.TERMINAL
        assert repository.receipt is not None
        assert len(_abandoned(root)) == 1
        with FileAuthoritySystemJournal(root, snapshot.system_id) as journal:
            records = journal.read()
        assert records[-1].phase is AuthoritySystemJournalPhase.TERMINAL
        assert [r.sequence for r in records[:confirmed]] == list(range(1, confirmed + 1))

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", _CRASHABLE_PHASES)
def test_retry_adopts_unconfirmed_record(
    tmp_path: Path, phase: AuthoritySystemJournalPhase
) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        request = _request(snapshot)
        first = _service(repository, root)
        acknowledgement = await _crash_provision_at(first, repository, request, phase)
        await first.close()
        with FileAuthoritySystemJournal(root, snapshot.system_id) as journal:
            file_ahead = journal.read()[repository.sequence]

        provider = _FailingProvisionProvider()
        recovered = _service(repository, root, provider)
        if acknowledgement is None:
            acknowledgement = await _begin(recovered, repository, request)
        await recovered.execute("worker-a", _mutation(request), acknowledgement)
        await recovered.close()
        assert repository.receipt is not None
        assert _abandoned(root) == []
        with FileAuthoritySystemJournal(root, snapshot.system_id) as journal:
            assert file_ahead in journal.read()
        # Only a confirmed mutation-started means the provider may already have run.
        provider_may_have_run = phase is AuthoritySystemJournalPhase.PROVIDER_RETURNED
        assert provider.execute_calls == int(not provider_may_have_run)

    asyncio.run(scenario())


def test_divergence_inside_the_confirmed_prefix_is_still_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        first = _service(repository, root)
        await _crash_provision_at(
            first, repository, _request(snapshot), AuthoritySystemJournalPhase.MUTATION_STARTED
        )
        await first.close()
        with FileAuthoritySystemJournal(root, snapshot.system_id) as journal:
            before = journal.read()

        repository.sequence -= 1  # the file now holds two records past the database head
        teardown = _teardown(snapshot, 2)
        repository.allocating = teardown
        refused = _service(repository, root)
        with pytest.raises(AuthoritySystemServiceError, match="journal-conflict"):
            await refused.begin("worker-a", teardown)
        repository.sequence += 1
        repository.digest = "sha256:" + "0" * 64  # the head digest no longer matches the file
        with pytest.raises(AuthoritySystemServiceError, match="journal-conflict"):
            await refused.begin("worker-a", teardown)
        await refused.close()
        with FileAuthoritySystemJournal(root, snapshot.system_id) as journal:
            assert journal.read() == before
        assert _abandoned(root) == []

    asyncio.run(scenario())


def test_full_evidence_refuses_quarantine_with_journal_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        first = _service(repository, root)
        await _crash_provision_at(
            first, repository, _request(snapshot), AuthoritySystemJournalPhase.MUTATION_STARTED
        )
        await first.close()
        with FileAuthoritySystemJournal(root, snapshot.system_id) as journal:
            before = journal.read()

        monkeypatch.setattr(journal_module, "MAX_ABANDONED_PER_SYSTEM", 0)
        teardown = _teardown(snapshot, 2)
        repository.allocating = teardown
        refused = _service(repository, root)
        with pytest.raises(AuthoritySystemServiceError, match="journal-conflict"):
            await refused.begin("worker-a", teardown)
        await refused.close()
        with FileAuthoritySystemJournal(root, snapshot.system_id) as journal:
            assert journal.read() == before

    asyncio.run(scenario())


def test_unwritable_evidence_directory_refuses_quarantine_with_journal_conflict(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        snapshot = _snapshot(uuid4(), uuid4(), uuid4())
        repository = _Repository(snapshot)
        root = _root(tmp_path)
        first = _service(repository, root)
        await _crash_provision_at(
            first, repository, _request(snapshot), AuthoritySystemJournalPhase.MUTATION_STARTED
        )
        await first.close()
        (root / "system-operations" / "abandoned").mkdir(mode=0o755)
        (root / "system-operations" / "abandoned").chmod(0o755)

        teardown = _teardown(snapshot, 2)
        repository.allocating = teardown
        refused = _service(repository, root)
        with pytest.raises(AuthoritySystemServiceError, match="journal-conflict"):
            await refused.begin("worker-a", teardown)
        await refused.close()
        with FileAuthoritySystemJournal(root, snapshot.system_id) as journal:
            assert len(journal.read()) == repository.sequence + 1

    asyncio.run(scenario())
