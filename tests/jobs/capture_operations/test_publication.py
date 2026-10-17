"""Successful capture publication ordering and crash-boundary persistence."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast

import psycopg
import pytest

from kdive.artifacts.storage import (
    ConditionalArtifactWriteRequest,
    ConditionalCreateResult,
    HeadResult,
    StoredArtifact,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.jobs.capture_operations.storage.publication import (
    CapturePublicationCoordinator,
    recover_publication,
)
from kdive.jobs.capture_operations.storage.repository import (
    CaptureOperation,
)
from kdive.store.objectstore import ObjectStore
from tests.db_waits import wait_until_backend_waiting
from tests.jobs.capture_operations.conftest import _close, _Store, _Subject, _subject

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


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
            assert isinstance(request.data, bytes)
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
