"""Cancellation recovery for durable capture publication boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from kdive.artifacts.storage import HeadResult, StoredArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.jobs.capture_operations import publication
from kdive.jobs.capture_operations.publication import (
    CapturePublicationIdentityConflict,
    recover_publication,
)
from kdive.jobs.capture_operations.repository import CaptureOperation

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _operation(launch_token: str, **changes: object) -> CaptureOperation:
    operation = CaptureOperation(
        id=uuid4(),
        job_id=uuid4(),
        job_attempt=1,
        worker_incarnation="local:worker:1",
        provider_kind="local-libvirt",
        resource_id=uuid4(),
        system_id=uuid4(),
        domain_name="guest",
        request_digest="a" * 64,
        launch_token=launch_token,
        host_instance="host-a",
        boot_id="boot-a",
        pid=41,
        start_ticks=59,
        state="exited",
        exit_outcome="recovered",
        exit_code=None,
        process_absent=True,
        provider_quiescence={"result": "absent"},
        recovered_by="local:worker:2",
        publication_state="canceling",
        publication_object_key="tenant/runs/run/pcap-operation",
        publication_etag=None,
        publication_artifact_id=None,
        cleanup_capture_version_id=None,
        publication_tombstone_version=None,
        publication_started_at=_NOW,
        publication_closed_at=None,
        spool_disposed_at=None,
        created_at=_NOW,
        identity_recorded_at=_NOW,
        running_at=_NOW,
        cancel_requested_at=None,
        exited_at=_NOW,
        updated_at=_NOW,
    )
    return replace(operation, **changes)


def _head(
    operation: CaptureOperation,
    *,
    kind: str,
    version_id: str = "version-1",
    size_bytes: int = 0,
    operation_id: UUID | None = None,
) -> HeadResult:
    return HeadResult(
        size_bytes=size_bytes,
        checksum_sha256=None,
        etag="etag-1",
        last_modified=_NOW,
        version_id=version_id,
        sensitivity=Sensitivity.SENSITIVE,
        metadata=MappingProxyType(
            {
                "operation-id": str(operation.id if operation_id is None else operation_id),
                "publication-kind": kind,
            }
        ),
    )


class _Store:
    def __init__(
        self, head: HeadResult | None, *, operation: CaptureOperation | None = None
    ) -> None:
        self.current = head
        self.operation = operation
        self.deleted: list[tuple[str, str]] = []
        self.created = []

    def head(self, key: str, *, version_id: str | None = None) -> HeadResult | None:
        if self.current is None:
            return None
        if version_id is not None and self.current.version_id != version_id:
            return None
        return self.current

    def delete_version(self, key: str, version_id: str) -> None:
        self.deleted.append((key, version_id))
        if self.current is not None and self.current.version_id == version_id:
            self.current = None

    def create_if_absent(self, request: Any) -> StoredArtifact:
        self.created.append(request)
        assert self.operation is not None
        stored = StoredArtifact(
            request.key,
            "tombstone-etag",
            request.sensitivity,
            request.retention_class,
            "tombstone-version",
        )
        self.current = _head(
            self.operation,
            kind="tombstone",
            version_id=stored.version_id,
        )
        return stored


@pytest.mark.parametrize(
    ("head", "reason"),
    [
        (lambda operation: _head(operation, kind="unknown"), "unknown_publication_kind"),
        (
            lambda operation: _head(operation, kind="tombstone", size_bytes=1),
            "nonempty_tombstone",
        ),
        (
            lambda operation: _head(operation, kind="capture", operation_id=uuid4(), size_bytes=64),
            "operation_id_mismatch",
        ),
    ],
)
def test_publication_identity_conflicts_are_stable_and_redacted(
    head: Any, reason: str, launch_token: str
) -> None:
    operation = _operation(launch_token)

    with pytest.raises(CapturePublicationIdentityConflict) as caught:
        publication.PublicationObjectIdentity.parse(operation, head(operation))

    assert caught.value.reason == reason
    assert caught.value.operation_id == operation.id
    assert caught.value.key == operation.publication_object_key
    assert "etag" not in str(caught.value)


@pytest.mark.anyio
async def test_recovery_adopts_existing_tombstone(
    monkeypatch: pytest.MonkeyPatch, launch_token: str
) -> None:
    operation = _operation(launch_token)
    store = _Store(_head(operation, kind="tombstone", version_id="tombstone-version"))
    committed: list[str] = []

    async def begin(*_args: object) -> CaptureOperation:
        return operation

    async def commit(*_args: object) -> CaptureOperation:
        committed.append(cast(str, _args[-1]))
        return replace(
            operation,
            publication_state="discarded",
            publication_tombstone_version="tombstone-version",
        )

    monkeypatch.setattr(publication, "begin_cancel_publication", begin)
    monkeypatch.setattr(publication, "commit_discarded", commit)

    recovered = await recover_publication(
        cast(Any, SimpleNamespace()), cast(Any, store), SecretStr("credential"), operation
    )

    assert recovered.publication_state == "discarded"
    assert committed == ["tombstone-version"]
    assert store.deleted == []


@pytest.mark.anyio
async def test_recovery_deletes_only_journaled_capture_then_tombstones(
    monkeypatch: pytest.MonkeyPatch,
    launch_token: str,
) -> None:
    operation = _operation(
        launch_token,
        cleanup_capture_version_id="capture-version",
        publication_etag="capture-etag",
    )
    store = _Store(
        _head(operation, kind="capture", version_id="capture-version", size_bytes=64),
        operation=operation,
    )

    async def begin(*_args: object) -> CaptureOperation:
        return operation

    async def commit(*_args: object) -> CaptureOperation:
        return replace(
            operation,
            publication_state="discarded",
            publication_tombstone_version="tombstone-version",
        )

    monkeypatch.setattr(publication, "begin_cancel_publication", begin)
    monkeypatch.setattr(publication, "commit_discarded", commit)

    recovered = await recover_publication(
        cast(Any, SimpleNamespace()), cast(Any, store), SecretStr("credential"), operation
    )

    assert recovered.publication_state == "discarded"
    assert store.deleted == [(cast(str, operation.publication_object_key), "capture-version")]
    assert len(store.created) == 1
    assert store.created[0].data == b""
    assert store.created[0].metadata["publication-kind"] == "tombstone"


@pytest.mark.anyio
async def test_recovery_leaves_conflicting_object_untouched(
    monkeypatch: pytest.MonkeyPatch,
    launch_token: str,
) -> None:
    operation = _operation(launch_token)
    store = _Store(_head(operation, kind="capture", operation_id=uuid4(), size_bytes=64))

    async def begin(*_args: object) -> CaptureOperation:
        return operation

    monkeypatch.setattr(publication, "begin_cancel_publication", begin)

    with pytest.raises(CapturePublicationIdentityConflict):
        await recover_publication(
            cast(Any, SimpleNamespace()), cast(Any, store), SecretStr("credential"), operation
        )

    assert store.deleted == []
    assert store.created == []
