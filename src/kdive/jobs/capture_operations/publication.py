"""Fence-bound successful capture publication for issue #1952."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import SecretStr

from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import (
    ConditionalArtifactWriteRequest,
    ConditionalCreateConflict,
    ConditionalCreateResult,
    HeadResult,
    StoredArtifact,
    artifact_key,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.catalog.artifacts import Artifact, Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.capture_operations.repository import (
    CaptureOperation,
    begin_cancel_publication,
    begin_publication,
    commit_discarded,
    commit_published,
    record_capture_version,
    record_cleanup_capture_version,
)
from kdive.jobs.payloads import CaptureTrafficPayload, load_payload
from kdive.security.audit import AuditEvent
from kdive.store.objectstore import ObjectStore

if TYPE_CHECKING:
    from kdive.jobs.capture_operations.supervisor import CaptureSnapshot

_TENANT = "local"
_OWNER_KIND = "runs"
_RETENTION_CLASS = "pcap"
_PUBLICATION_KIND = "capture"
_TOMBSTONE_KIND = "tombstone"


class CapturePublicationIdentityConflict(RuntimeError):
    """An object at the publication key cannot be safely adopted or deleted."""

    def __init__(self, operation_id: UUID, key: str, reason: str) -> None:
        self.operation_id = operation_id
        self.key = key
        self.reason = reason
        super().__init__(
            f"capture publication object identity conflict for {operation_id} at {key}: {reason}"
        )


@dataclass(frozen=True, slots=True)
class PublicationObjectIdentity:
    """Verified operation and kind encoded in immutable object metadata."""

    operation_id: UUID
    kind: Literal["capture", "tombstone"]
    version_id: str

    @classmethod
    def parse(cls, operation: CaptureOperation, head: HeadResult) -> PublicationObjectIdentity:
        key = operation.publication_object_key
        assert key is not None
        raw_operation_id = head.metadata.get("operation-id")
        try:
            observed_operation_id = UUID(raw_operation_id or "")
        except ValueError as error:
            raise CapturePublicationIdentityConflict(
                operation.id, key, "invalid_operation_id"
            ) from error
        if observed_operation_id != operation.id:
            raise CapturePublicationIdentityConflict(operation.id, key, "operation_id_mismatch")
        kind = head.metadata.get("publication-kind")
        if kind not in {_PUBLICATION_KIND, _TOMBSTONE_KIND}:
            raise CapturePublicationIdentityConflict(operation.id, key, "unknown_publication_kind")
        if kind == _TOMBSTONE_KIND and head.size_bytes != 0:
            raise CapturePublicationIdentityConflict(operation.id, key, "nonempty_tombstone")
        return cls(operation.id, kind, head.version_id)


def _publication_error(operation: CaptureOperation, reason: str) -> CategorizedError:
    return CategorizedError(
        "capture publication did not reach a committed artifact",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={
            "reason": reason,
            "operation_id": str(operation.id),
            "job_id": str(operation.job_id),
            "attempt": operation.job_attempt,
        },
    )


async def _existing_artifact(conn: AsyncConnection, run_id: UUID, key: str) -> Artifact | None:
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT * FROM artifacts "
            "WHERE owner_kind = 'runs' AND owner_id = %s AND object_key = %s",
            (run_id, key),
        )
        row = await cursor.fetchone()
    return None if row is None else Artifact.model_validate(row)


async def _finish_owned_task[T](task: asyncio.Task[T]) -> T:
    """Drain a blocking store task without letting repeated cancellation orphan it."""
    current = asyncio.current_task()
    assert current is not None
    completed = asyncio.Event()
    task.add_done_callback(lambda _task: completed.set())
    while not completed.is_set():
        try:
            await completed.wait()
        except asyncio.CancelledError:
            current.uncancel()
    return task.result()


async def _conditional_create(
    store: ObjectStore, request: ConditionalArtifactWriteRequest
) -> ConditionalCreateResult:
    task = asyncio.create_task(asyncio.to_thread(store.create_if_absent, request))
    completed = asyncio.Event()
    task.add_done_callback(lambda _task: completed.set())
    try:
        await completed.wait()
    except asyncio.CancelledError as cancellation:
        current = asyncio.current_task()
        assert current is not None
        current.uncancel()
        # Authority loss owns this outcome; the drained store failure cannot replace it.
        with contextlib.suppress(Exception):
            await _finish_owned_task(task)
        raise cancellation
    return task.result()


async def _publication_key(conn: AsyncConnection, operation: CaptureOperation) -> str:
    if operation.publication_object_key is not None:
        return operation.publication_object_key
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT payload ->> 'run_id' AS run_id FROM jobs WHERE id = %s",
            (operation.job_id,),
        )
        row = await cursor.fetchone()
    if row is None or not isinstance(row.get("run_id"), str):
        raise _publication_error(operation, "capture_publication_run_missing")
    try:
        run_id = UUID(row["run_id"])
    except ValueError as error:
        raise _publication_error(operation, "capture_publication_run_invalid") from error
    return artifact_key(_TENANT, _OWNER_KIND, str(run_id), f"pcap-{operation.id}")


async def _head(
    store: ObjectStore, key: str, *, version_id: str | None = None
) -> HeadResult | None:
    return await asyncio.to_thread(store.head, key, version_id=version_id)


async def _delete_verified_version(store: ObjectStore, operation: CaptureOperation) -> None:
    key = operation.publication_object_key
    version_id = operation.cleanup_capture_version_id
    assert key is not None and version_id is not None
    observed = await _head(store, key, version_id=version_id)
    if observed is not None:
        identity = PublicationObjectIdentity.parse(operation, observed)
        if identity.kind != _PUBLICATION_KIND:
            raise CapturePublicationIdentityConflict(
                operation.id, key, "cleanup_version_is_not_capture"
            )
        await asyncio.to_thread(store.delete_version, key, version_id)
    if await _head(store, key, version_id=version_id) is not None:
        raise _publication_error(operation, "capture_publication_delete_unverified")


async def _create_or_adopt_tombstone(store: ObjectStore, operation: CaptureOperation) -> str:
    key = operation.publication_object_key
    assert key is not None
    created = await _conditional_create(
        store,
        ConditionalArtifactWriteRequest(
            key=key,
            data=b"",
            metadata={
                "operation-id": str(operation.id),
                "publication-kind": _TOMBSTONE_KIND,
            },
            sensitivity=Sensitivity.SENSITIVE,
            retention_class=_RETENTION_CLASS,
        ),
    )
    if isinstance(created, StoredArtifact):
        return created.version_id
    head = await _head(store, key)
    if head is None:
        raise _publication_error(operation, "capture_publication_conflict_missing")
    identity = PublicationObjectIdentity.parse(operation, head)
    if identity.kind != _TOMBSTONE_KIND:
        raise CapturePublicationIdentityConflict(
            operation.id, key, "create_conflict_is_not_tombstone"
        )
    return identity.version_id


async def recover_publication(
    conn: AsyncConnection,
    store: ObjectStore,
    credential: SecretStr,
    operation: CaptureOperation,
) -> CaptureOperation:
    """Close one exited attempt's publication monotonically and without unsafe deletion."""
    if operation.publication_state in {"published", "discarded"}:
        return operation
    key = await _publication_key(conn, operation)
    canceling = await begin_cancel_publication(conn, credential, operation.id, key)
    if canceling.publication_state == "discarded":
        return canceling

    if canceling.cleanup_capture_version_id is not None:
        await _delete_verified_version(store, canceling)
    else:
        current = await _head(store, key)
        if current is not None:
            identity = PublicationObjectIdentity.parse(canceling, current)
            if identity.kind == _TOMBSTONE_KIND:
                return await commit_discarded(conn, credential, canceling.id, identity.version_id)
            canceling = await record_cleanup_capture_version(
                conn, credential, canceling.id, identity.version_id
            )
            await _delete_verified_version(store, canceling)

    tombstone_version = await _create_or_adopt_tombstone(store, canceling)
    return await commit_discarded(conn, credential, canceling.id, tombstone_version)


def _audit(run_id: UUID, project: str) -> AuditEvent:
    return AuditEvent(
        tool="control.capture_traffic",
        object_kind="runs",
        object_id=run_id,
        transition="capture_traffic",
        args={"run_id": str(run_id)},
        project=project,
    )


class CapturePublicationCoordinator:
    """Publish one operation-unique pcap under an exact worker credential."""

    def __init__(self, store: ObjectStore, credential: SecretStr) -> None:
        self._store = store
        self._credential = credential

    async def publish(
        self,
        conn: AsyncConnection,
        job: Job,
        operation: CaptureOperation,
        snapshot: CaptureSnapshot,
        data: bytes,
    ) -> UUID:
        """Conditionally create, journal, claim, audit, and publish one pcap."""
        payload = load_payload(job, CaptureTrafficPayload)
        run_id = UUID(payload.run_id)
        key = artifact_key(
            _TENANT,
            _OWNER_KIND,
            str(run_id),
            f"pcap-{operation.id}",
        )
        publishing = await begin_publication(conn, self._credential, operation.id, key)
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
            existing = await _existing_artifact(conn, run_id, key)
        if publishing.publication_state == "published":
            if (
                publishing.publication_artifact_id is None
                or existing is None
                or existing.id != publishing.publication_artifact_id
            ):
                raise _publication_error(operation, "capture_publication_row_missing")
            return existing.id
        if existing is not None:
            adopted = await commit_published(
                conn,
                self._credential,
                operation.id,
                existing,
                _audit(run_id, snapshot.project),
            )
            assert adopted.publication_artifact_id is not None
            return adopted.publication_artifact_id

        created = await _conditional_create(
            self._store,
            ConditionalArtifactWriteRequest(
                key=key,
                data=data,
                metadata={
                    "operation-id": str(operation.id),
                    "publication-kind": _PUBLICATION_KIND,
                },
                sensitivity=Sensitivity.SENSITIVE,
                retention_class=_RETENTION_CLASS,
            ),
        )
        if isinstance(created, ConditionalCreateConflict):
            raise _publication_error(operation, "capture_publication_create_conflict")
        assert isinstance(created, StoredArtifact)
        journaled = await record_capture_version(
            conn,
            self._credential,
            operation.id,
            created.version_id,
            created.etag,
        )
        artifact = register_artifact_row(
            created,
            owner_kind=_OWNER_KIND,
            owner_id=run_id,
            run_id=run_id,
        )
        published = await commit_published(
            conn,
            self._credential,
            journaled.id,
            artifact,
            _audit(run_id, snapshot.project),
        )
        if published.publication_artifact_id != artifact.id:
            raise _publication_error(operation, "capture_publication_claim_mismatch")
        return artifact.id
