"""Round-trip tests for the ``content_encoding`` user-metadata field (feat/892)."""

from __future__ import annotations

from uuid import uuid4

from kdive.artifacts.storage import ArtifactWriteRequest
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.store.objectstore import ObjectStore


def test_put_records_content_encoding_and_head_reads_it(minio_store: ObjectStore) -> None:
    req = ArtifactWriteRequest(
        tenant="t",
        owner_kind="systems",
        owner_id=str(uuid4()),
        name="console-part-0-000000",
        data=b"x",
        sensitivity=Sensitivity.REDACTED,
        retention_class="evidence",
        content_encoding="gzip",
    )
    stored = minio_store.put_artifact(req)
    head = minio_store.head(stored.key)
    assert head is not None and head.content_encoding == "gzip"


def test_put_without_content_encoding_heads_none(minio_store: ObjectStore) -> None:
    req = ArtifactWriteRequest(
        tenant="t",
        owner_kind="systems",
        owner_id=str(uuid4()),
        name="dmesg-redacted",
        data=b"x",
        sensitivity=Sensitivity.REDACTED,
        retention_class="evidence",
    )
    stored = minio_store.put_artifact(req)
    head = minio_store.head(stored.key)
    assert head is not None and head.content_encoding is None
