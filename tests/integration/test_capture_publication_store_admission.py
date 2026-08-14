"""Live object-store proof for capture-publication conditional creation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from kdive.artifacts.storage import (
    ConditionalArtifactWriteRequest,
    ConditionalCreateConflict,
    ConditionalCreateResult,
    StoredArtifact,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.store.objectstore import ObjectStore


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
