"""Successful capture publication ordering and crash-boundary persistence."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.artifacts.storage import (
    ConditionalArtifactWriteRequest,
    ConditionalCreateResult,
    HeadResult,
    StoredArtifact,
)
from kdive.domain.capacity.state import JobState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.capture_operations.storage.publication import (
    CapturePublicationCoordinator,
    recover_publication,
)
from kdive.jobs.capture_operations.storage.repository import (
    CaptureOperation,
    CaptureOperationIdentity,
    CaptureOperationSnapshot,
    RecoveryEvidence,
    acknowledge_exit,
    create_launching,
    mark_running,
    record_identity,
)
from kdive.jobs.capture_operations.supervisor import CaptureSnapshot
from kdive.services.runs.worker_incarnations import CURRENT_WORKER_FENCE_PROTOCOL
from kdive.store.objectstore import ObjectStore
from tests.db_waits import wait_until_backend_waiting
from tests.reconciler.conftest import connect

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _Store:
    def __init__(
        self,
        *,
        block_create: bool = False,
        create_error: Exception | None = None,
    ) -> None:
        self.requests: list[ConditionalArtifactWriteRequest] = []
        self.objects: dict[str, StoredArtifact] = {}
        self.create_error = create_error
        self.create_entered = threading.Event()
        self.create_release = threading.Event()
        if not block_create:
            self.create_release.set()

    def create_if_absent(self, request: ConditionalArtifactWriteRequest) -> ConditionalCreateResult:
        self.requests.append(request)
        self.create_entered.set()
        assert self.create_release.wait(timeout=5)
        if self.create_error is not None:
            raise self.create_error
        stored = StoredArtifact(
            request.key,
            hashlib.sha256(request.data).hexdigest(),
            request.sensitivity,
            request.retention_class,
            "capture-version",
        )
        self.objects[request.key] = stored
        return stored


@dataclass(frozen=True, slots=True)
class _Subject:
    admin: AsyncConnection
    worker: AsyncConnection
    credential: SecretStr
    job: Job
    operation: CaptureOperation
    snapshot: CaptureSnapshot


async def _as_worker(url: str) -> AsyncConnection:
    conn = await connect(url)
    role = sql.Identifier("kdive_worker")
    await conn.execute(sql.SQL("SET SESSION AUTHORIZATION {}").format(role))
    return conn


async def _subject(url: str) -> _Subject:
    admin = await connect(url)
    worker = await _as_worker(url)
    credential = SecretStr(f"publication-{uuid4()}")
    worker_id = f"local:publication:{uuid4()}"
    resource_id, allocation_id, system_id, investigation_id, run_id, job_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    await admin.execute(
        "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
        "fence_protocol, credential_hash) VALUES (%s, 'local', %s, %s, %s)",
        (
            worker_id,
            Jsonb({"host": "host-a"}),
            CURRENT_WORKER_FENCE_PROTOCOL,
            hashlib.sha256(credential.get_secret_value().encode()).digest(),
        ),
    )
    await admin.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'pool', 'local', 'available', 'qemu:///system')",
        (resource_id,),
    )
    await admin.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'principal', 'project')",
        (allocation_id, resource_id),
    )
    await admin.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, domain_name, "
        "principal, project) VALUES (%s, %s, 'ready', '{}'::jsonb, 'guest', "
        "'principal', 'project')",
        (system_id, allocation_id),
    )
    await admin.execute(
        "INSERT INTO investigations (id, title, state, principal, project) "
        "VALUES (%s, 'capture', 'active', 'principal', 'project')",
        (investigation_id,),
    )
    await admin.execute(
        "INSERT INTO runs (id, investigation_id, system_id, state, build_profile, target_kind, "
        "principal, project) VALUES (%s, %s, %s, 'running', '{}'::jsonb, 'local-libvirt', "
        "'principal', 'project')",
        (run_id, investigation_id, system_id),
    )
    authorizing = {"principal": "principal", "agent_session": None, "project": "project"}
    payload = {
        "run_id": str(run_id),
        "duration_s": 1,
        "max_bytes": 1_048_576,
        "snaplen": 128,
    }
    await admin.execute(
        "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
        "(%s, 'capture_traffic', %s, 'running', 1, 3, %s, now() + interval '5 min', now(), "
        "%s, %s)",
        (
            job_id,
            Jsonb(payload),
            worker_id,
            Jsonb(authorizing),
            f"capture-{job_id}",
        ),
    )
    repository_snapshot = CaptureOperationSnapshot(
        provider_kind="local-libvirt",
        resource_id=resource_id,
        system_id=system_id,
        domain_name="guest",
        request_digest="a" * 64,
    )
    operation = await create_launching(worker, credential, job_id, 1, repository_snapshot)
    operation = await record_identity(
        worker,
        credential,
        operation.id,
        CaptureOperationIdentity("host-a", "boot-a", 41, 59),
    )
    operation = await mark_running(worker, credential, operation.id)
    operation = await acknowledge_exit(
        worker,
        credential,
        operation.id,
        RecoveryEvidence(
            process_absent=True,
            provider_quiescence={"result": "absent"},
            exit_outcome="completed",
            exit_code=0,
        ),
    )
    return _Subject(
        admin=admin,
        worker=worker,
        credential=credential,
        job=Job(
            id=job_id,
            created_at=_NOW,
            updated_at=_NOW,
            kind=JobKind.CAPTURE_TRAFFIC,
            payload=payload,
            state=JobState.RUNNING,
            attempt=1,
            max_attempts=3,
            worker_id=worker_id,
            authorizing=authorizing,
            dedup_key=f"capture-{job_id}",
        ),
        operation=operation,
        snapshot=CaptureSnapshot(
            provider_kind="local-libvirt",
            resource_id=resource_id,
            system_id=system_id,
            domain_name="guest",
            project="project",
            write_remediation="unused",
            configuration=lambda: b"unused",
            quiescence=cast(Any, lambda _configuration: SimpleNamespace()),
        ),
    )


async def _operation_row(subject: _Subject) -> tuple[Any, ...]:
    row = await (
        await subject.admin.execute(
            "SELECT publication_state, publication_etag, cleanup_capture_version_id, "
            "publication_artifact_id FROM capture_operations WHERE id = %s",
            (subject.operation.id,),
        )
    ).fetchone()
    assert row is not None
    return row


async def _close(subject: _Subject) -> None:
    await subject.worker.close()
    await subject.admin.close()


def test_success_uses_operation_identity_metadata_and_adopts_sequential_replay(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        subject = await _subject(migrated_url)
        store = _Store()
        coordinator = CapturePublicationCoordinator(cast(ObjectStore, store), subject.credential)
        try:
            artifact_id = await coordinator.publish(
                subject.worker, subject.job, subject.operation, subject.snapshot, b"pcap"
            )
            replayed_id = await coordinator.publish(
                subject.worker, subject.job, subject.operation, subject.snapshot, b"pcap"
            )
            assert replayed_id == artifact_id
            assert len(store.requests) == 1
            request = store.requests[0]
            assert request.key.endswith(f"/pcap-{subject.operation.id}")
            assert request.metadata == {
                "operation-id": str(subject.operation.id),
                "publication-kind": "capture",
            }
            assert request.sensitivity is Sensitivity.SENSITIVE
            assert request.retention_class == "pcap"
            assert await _operation_row(subject) == (
                "published",
                hashlib.sha256(b"pcap").hexdigest(),
                "capture-version",
                artifact_id,
            )
            assert await (
                await subject.admin.execute(
                    "SELECT count(*) FROM artifacts WHERE id = %s", (artifact_id,)
                )
            ).fetchone() == (1,)
            assert await (
                await subject.admin.execute(
                    "SELECT count(*) FROM audit_log WHERE object_id = %s",
                    (subject.job.payload["run_id"],),
                )
            ).fetchone() == (1,)
        finally:
            await _close(subject)

    asyncio.run(_run())


def test_coordinator_refreshes_durable_state_before_recovery(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kdive.jobs.capture_operations.storage import publication as publication_module

    async def _run() -> None:
        subject = await _subject(migrated_url)
        stale = subject.operation
        published = replace(stale, publication_state="published")
        observed: list[object] = []

        async def refresh(*args: object) -> CaptureOperation:
            observed.append(args[-1])
            return published

        monkeypatch.setattr(publication_module, "refresh_publication_operation", refresh)
        coordinator = CapturePublicationCoordinator(cast(ObjectStore, _Store()), subject.credential)
        try:
            assert await coordinator.recover(subject.worker, stale) == published
            assert observed == [stale.id]
        finally:
            await _close(subject)

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("stage", "expected_state", "journaled", "object_created"),
    [
        ("before_create", "publishing", False, False),
        ("during_create", "publishing", False, True),
        ("after_create_before_journal", "publishing", False, True),
        ("after_journal_before_claim", "publishing", True, True),
        ("after_transaction_before_return", "published", True, True),
    ],
)
def test_cancellation_at_synchronized_publication_stages_leaves_durable_boundary(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected_state: str,
    journaled: bool,
    object_created: bool,
) -> None:
    async def _run() -> None:
        from kdive.jobs.capture_operations.storage import publication

        subject = await _subject(migrated_url)
        store = _Store(block_create=stage == "during_create")
        coordinator = CapturePublicationCoordinator(cast(ObjectStore, store), subject.credential)
        reached = asyncio.Event()
        release = asyncio.Event()
        original_begin = publication.begin_publication
        original_record = publication.record_capture_version
        original_commit = publication.commit_published

        async def begin(*args: Any, **kwargs: Any) -> CaptureOperation:
            result = await original_begin(*args, **kwargs)
            if stage == "before_create":
                reached.set()
                await release.wait()
            return result

        async def record(*args: Any, **kwargs: Any) -> CaptureOperation:
            if stage == "after_create_before_journal":
                reached.set()
                await release.wait()
            result = await original_record(*args, **kwargs)
            if stage == "after_journal_before_claim":
                reached.set()
                await release.wait()
            return result

        async def commit(*args: Any, **kwargs: Any) -> CaptureOperation:
            result = await original_commit(*args, **kwargs)
            if stage == "after_transaction_before_return":
                reached.set()
                await release.wait()
            return result

        monkeypatch.setattr(publication, "begin_publication", begin)
        monkeypatch.setattr(publication, "record_capture_version", record)
        monkeypatch.setattr(publication, "commit_published", commit)
        try:
            task = asyncio.create_task(
                coordinator.publish(
                    subject.worker, subject.job, subject.operation, subject.snapshot, b"pcap"
                )
            )
            if stage == "during_create":
                assert await asyncio.to_thread(store.create_entered.wait, 5)
            else:
                await asyncio.wait_for(reached.wait(), timeout=5)
            task.cancel()
            store.create_release.set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            state, etag, version_id, artifact_id = await _operation_row(subject)
            assert state == expected_state
            assert (etag, version_id) == (
                (hashlib.sha256(b"pcap").hexdigest(), "capture-version")
                if journaled
                else (None, None)
            )
            assert (artifact_id is not None) is (expected_state == "published")
            assert bool(store.objects) is object_created
        finally:
            await _close(subject)

    asyncio.run(_run())


def test_cancellation_during_failed_create_preserves_authority_loss(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        subject = await _subject(migrated_url)
        store = _Store(block_create=True, create_error=RuntimeError("store unavailable"))
        coordinator = CapturePublicationCoordinator(cast(ObjectStore, store), subject.credential)
        try:
            task = asyncio.create_task(
                coordinator.publish(
                    subject.worker, subject.job, subject.operation, subject.snapshot, b"pcap"
                )
            )
            assert await asyncio.to_thread(store.create_entered.wait, 5)
            task.cancel()
            store.create_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await _operation_row(subject) == ("publishing", None, None, None)
        finally:
            await _close(subject)

    asyncio.run(_run())


def test_cancellation_while_audit_claim_is_blocked_rolls_back_the_transaction(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        from kdive.jobs.capture_operations.storage import publication

        subject = await _subject(migrated_url)
        blocker = await psycopg.AsyncConnection.connect(migrated_url)
        store = _Store()
        coordinator = CapturePublicationCoordinator(cast(ObjectStore, store), subject.credential)
        journaled = asyncio.Event()
        original_record = publication.record_capture_version

        async def record(*args: Any, **kwargs: Any) -> CaptureOperation:
            result = await original_record(*args, **kwargs)
            journaled.set()
            return result

        monkeypatch.setattr(publication, "record_capture_version", record)
        try:
            await blocker.execute("LOCK TABLE audit_log IN ACCESS EXCLUSIVE MODE")
            task = asyncio.create_task(
                coordinator.publish(
                    subject.worker, subject.job, subject.operation, subject.snapshot, b"pcap"
                )
            )
            await asyncio.wait_for(journaled.wait(), timeout=5)
            await wait_until_backend_waiting(
                subject.admin, subject.worker.info.backend_pid, locktype="relation"
            )
            task.cancel()
            await blocker.rollback()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await _operation_row(subject) == (
                "publishing",
                hashlib.sha256(b"pcap").hexdigest(),
                "capture-version",
                None,
            )
            assert await (
                await subject.admin.execute("SELECT count(*) FROM artifacts")
            ).fetchone() == (0,)
            assert await (
                await subject.admin.execute("SELECT count(*) FROM audit_log")
            ).fetchone() == (0,)
        finally:
            await blocker.close()
            await _close(subject)

    asyncio.run(_run())


def test_pending_publication_recovery_derives_key_and_commits_tombstone(
    migrated_url: str,
) -> None:
    class _RecoveryStore:
        def __init__(self) -> None:
            self.current: HeadResult | None = None
            self.requests: list[ConditionalArtifactWriteRequest] = []

        def head(self, key: str, *, version_id: str | None = None) -> HeadResult | None:
            if self.current is None:
                return None
            if version_id is not None and version_id != self.current.version_id:
                return None
            return self.current

        def create_if_absent(
            self, request: ConditionalArtifactWriteRequest
        ) -> ConditionalCreateResult:
            self.requests.append(request)
            stored = StoredArtifact(
                request.key,
                hashlib.sha256(request.data).hexdigest(),
                request.sensitivity,
                request.retention_class,
                "tombstone-version",
            )
            self.current = HeadResult(
                size_bytes=0,
                checksum_sha256=None,
                etag=stored.etag,
                last_modified=_NOW,
                version_id=stored.version_id,
                sensitivity=request.sensitivity,
                metadata=MappingProxyType(dict(request.metadata)),
            )
            return stored

        def delete_version(self, key: str, version_id: str) -> None:
            raise AssertionError("pending recovery must not delete an absent capture")

    async def _run() -> None:
        subject = await _subject(migrated_url)
        store = _RecoveryStore()
        try:
            recovered = await recover_publication(
                subject.worker,
                cast(ObjectStore, store),
                subject.credential,
                subject.operation,
            )
            assert recovered.publication_state == "discarded"
            assert recovered.publication_tombstone_version == "tombstone-version"
            assert len(store.requests) == 1
            request = store.requests[0]
            assert request.key.endswith(f"/pcap-{subject.operation.id}")
            assert request.data == b""
            assert request.metadata == {
                "operation-id": str(subject.operation.id),
                "publication-kind": "tombstone",
            }
            assert await _operation_row(subject) == (
                "discarded",
                None,
                None,
                None,
            )
        finally:
            await _close(subject)

    asyncio.run(_run())
