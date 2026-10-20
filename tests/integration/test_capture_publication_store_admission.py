"""Live object-store proof for capture-publication conditional creation."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.artifacts.storage import (
    ConditionalArtifactWriteRequest,
    ConditionalCreateConflict,
    ConditionalCreateResult,
    StoredArtifact,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.reconciler.cleanup.uploads.upload_orphans import repair_leaked_upload_objects
from kdive.reconciler.cleanup.uploads.uploads import repair_abandoned_uploads
from kdive.store.objectstore import ObjectStore
from tests.conftest import _RoleDsns
from tests.jobs.capture_operations.conftest import _cleanup_publication, _close, _subject


def test_minio_allows_exactly_one_overlapping_capture_publication(
    minio_store: ObjectStore,
) -> None:
    operation_id = uuid4().hex
    key = f"integration/capture-publication/{operation_id}"
    requests = (
        ConditionalArtifactWriteRequest(
            key=key,
            data=b"capture-bytes",
            metadata={"operation-id": operation_id, "publication-kind": "capture"},
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="capture",
        ),
        ConditionalArtifactWriteRequest(
            key=key,
            data=b"",
            metadata={"operation-id": operation_id, "publication-kind": "tombstone"},
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="capture",
        ),
    )
    barrier = Barrier(2)

    def create(request: ConditionalArtifactWriteRequest) -> ConditionalCreateResult:
        barrier.wait()
        return minio_store.create_if_absent(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, requests))

    winners = [result for result in results if isinstance(result, StoredArtifact)]
    conflicts = [result for result in results if isinstance(result, ConditionalCreateConflict)]
    try:
        assert len(winners) == 1
        assert len(conflicts) == 1
        winner = winners[0]
        winning_request = requests[results.index(winner)]
        head = minio_store.head(key, version_id=winner.version_id)
        assert head is not None
        assert head.version_id == winner.version_id
        assert head.etag == winner.etag
        assert isinstance(winning_request.data, bytes)
        assert head.size_bytes == len(winning_request.data)
        assert head.metadata == {
            **winning_request.metadata,
            "sensitivity": winning_request.sensitivity.value,
            "retention-class": winning_request.retention_class,
        }
    finally:
        for stored in winners:
            minio_store.delete_version(stored.key, stored.version_id)

    assert minio_store.head(key) is None


@pytest.mark.parametrize("route", ["orphan", "manifest"])
@pytest.mark.parametrize("role", ["kdive_server", "kdive_reconciler"])
def test_cleanup_retains_late_publication_barrier(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    minio_store: ObjectStore,
    route: str,
    role: str,
) -> None:
    async def run() -> None:
        subject = await _subject(migrated_url)
        owned: list[StoredArtifact] = []
        try:
            run_id = UUID(subject.job.payload["run_id"])
            prefix = f"local/runs/{run_id}/"
            key = f"{prefix}pcap-{subject.operation.id}"
            metadata = {"operation-id": str(subject.operation.id), "publication-kind": "tombstone"}
            request = ConditionalArtifactWriteRequest(
                key=key,
                data=b"",
                metadata=metadata,
                sensitivity=Sensitivity.SENSITIVE,
                retention_class="pcap",
            )
            tombstone = await asyncio.to_thread(minio_store.create_if_absent, request)
            assert isinstance(tombstone, StoredArtifact)
            owned.append(tombstone)
            before = await asyncio.to_thread(minio_store.head, key)
            assert before is not None
            unrelated = await asyncio.to_thread(
                minio_store.create_if_absent,
                ConditionalArtifactWriteRequest(
                    key=prefix + "unrelated",
                    data=b"unused",
                    metadata={},
                    sensitivity=Sensitivity.SENSITIVE,
                    retention_class="pcap",
                ),
            )
            assert isinstance(unrelated, StoredArtifact)
            owned.append(unrelated)
            await _cleanup_publication(subject, "discarded", key, tombstone.version_id)
            if route == "manifest":
                await subject.admin.execute(
                    "INSERT INTO upload_manifests "
                    "(owner_kind, owner_id, prefix, manifest, deadline) "
                    "VALUES ('runs', %s, %s, %s, now() - interval '1 day')",
                    (run_id, prefix, Jsonb([])),
                )
            async with await psycopg.AsyncConnection.connect(authority_role_dsns(role)) as conn:
                for _ in range(2):
                    if route == "manifest":
                        await repair_abandoned_uploads(conn, minio_store)
                    else:
                        await repair_leaked_upload_objects(
                            conn, minio_store, timedelta(days=-1), timedelta(0)
                        )
                    assert await asyncio.to_thread(minio_store.head, key) == before
            assert await asyncio.to_thread(minio_store.head, unrelated.key) is None
            late = await asyncio.to_thread(
                minio_store.create_if_absent,
                ConditionalArtifactWriteRequest(
                    key=key,
                    data=b"late-pcap",
                    metadata={
                        "operation-id": str(subject.operation.id),
                        "publication-kind": "capture",
                    },
                    sensitivity=Sensitivity.SENSITIVE,
                    retention_class="pcap",
                ),
            )
            if isinstance(late, StoredArtifact):
                owned.append(late)
            assert isinstance(late, ConditionalCreateConflict)
            assert await asyncio.to_thread(minio_store.head, key) == before
        finally:
            try:
                for artifact in owned:
                    await asyncio.to_thread(
                        minio_store.delete_version, artifact.key, artifact.version_id
                    )
            finally:
                await _close(subject)

    asyncio.run(run())
