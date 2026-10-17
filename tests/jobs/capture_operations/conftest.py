"""Shared fixtures for the capture-operations tests."""

from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.artifacts.storage import (
    ConditionalArtifactWriteRequest,
    ConditionalCreateResult,
    StoredArtifact,
)
from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import Job, JobKind
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
from kdive.worker_lifecycle.authority_store import CURRENT_WORKER_FENCE_PROTOCOL
from tests.reconciler.conftest import connect

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def launch_token() -> str:
    """A launch token unique to this test.

    ``scan_launch_token`` enumerates all of ``/proc`` and matches every process that
    shares this euid and interpreter and carries the token in its argv, and the
    launcher's cleanup path SIGKILLs each match. Production is safe because
    ``0112_capture_operation_supervision.sql`` declares ``launch_token`` UNIQUE, so no
    two operations ever share one.

    Tests must honour that same invariant. Under xdist every worker runs as this uid
    with this interpreter, so a token literal shared between tests lets one worker's
    cleanup kill another worker's live capture child, and the victim fails with
    ``-9`` (#2063). Take the token from this fixture rather than writing a literal;
    ``tests/guards/test_capture_operations_launch_tokens_are_unique.py`` enforces it.
    """
    return secrets.token_hex(32)


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
        assert isinstance(request.data, bytes)
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


async def _close(subject: _Subject) -> None:
    await subject.worker.close()
    await subject.admin.close()


async def _cleanup_publication(
    subject: _Subject, state: str, key: str, version: str = "v1"
) -> None:
    if state not in {"publishing", "canceling", "discarded"}:
        raise ValueError(f"unsupported cleanup fixture state: {state}")
    await subject.admin.execute(
        "UPDATE capture_operations SET publication_state = %s, "
        "publication_object_key = %s, publication_started_at = now(), "
        "publication_tombstone_version = CASE WHEN %s = 'discarded' THEN %s END, "
        "publication_closed_at = CASE WHEN %s = 'discarded' THEN now() END "
        "WHERE id = %s",
        (state, key, state, version, state, subject.operation.id),
    )
    await subject.admin.execute(
        "UPDATE jobs SET state = 'canceled', lease_expires_at = now() - interval '1 hour' "
        "WHERE id = %s",
        (subject.job.id,),
    )
