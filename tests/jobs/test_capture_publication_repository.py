"""Credential-fenced persistence for the capture publication state product."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.domain.catalog.artifacts import Artifact, Sensitivity
from kdive.jobs.capture_operations.repository import (
    CaptureOperation,
    CaptureOperationIdentity,
    CaptureOperationSnapshot,
    RecoveryEvidence,
    acknowledge_exit,
    begin_cancel_publication,
    begin_publication,
    commit_discarded,
    commit_published,
    create_launching,
    mark_running,
    record_capture_version,
    record_cleanup_capture_version,
    record_identity,
    record_spool_disposed,
)
from kdive.security.audit import AuditEvent
from kdive.services.runs.worker_incarnations import CURRENT_WORKER_FENCE_PROTOCOL
from tests.db_waits import wait_until_backend_waiting
from tests.reconciler.conftest import connect


def _hash(credential: SecretStr) -> bytes:
    return hashlib.sha256(credential.get_secret_value().encode()).digest()


async def _as_worker(url: str) -> AsyncConnection:
    conn = await connect(url)
    role = sql.Identifier("kdive_worker")
    await conn.execute(sql.SQL("SET SESSION AUTHORIZATION {}").format(role))
    return conn


async def _register(
    conn: AsyncConnection,
    worker_id: str,
    credential: SecretStr,
    *,
    host: str = "host-a",
) -> None:
    await conn.execute(
        "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
        "fence_protocol, credential_hash) VALUES (%s, 'local', %s, %s, %s)",
        (
            worker_id,
            Jsonb({"host": host}),
            CURRENT_WORKER_FENCE_PROTOCOL,
            _hash(credential),
        ),
    )


async def _seed_job(
    conn: AsyncConnection,
    worker_id: str,
) -> tuple[UUID, UUID, CaptureOperationSnapshot]:
    resource_id, allocation_id, system_id, investigation_id, run_id, job_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'pool', 'local', 'available', 'qemu:///system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'principal', 'project')",
        (allocation_id, resource_id),
    )
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, domain_name, "
        "principal, project) VALUES (%s, %s, 'ready', '{}'::jsonb, 'guest', "
        "'principal', 'project')",
        (system_id, allocation_id),
    )
    await conn.execute(
        "INSERT INTO investigations (id, title, state, principal, project) "
        "VALUES (%s, 'capture', 'active', 'principal', 'project')",
        (investigation_id,),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, state, build_profile, "
        "target_kind, principal, project) VALUES (%s, %s, %s, 'running', '{}'::jsonb, "
        "'local-libvirt', 'principal', 'project')",
        (run_id, investigation_id, system_id),
    )
    await conn.execute(
        "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
        "(%s, 'capture_traffic', jsonb_build_object('run_id', %s::text), 'running', 1, 3, "
        "%s, now() + interval '5 min', now(), "
        '\'{"principal":"principal","agent_session":null,'
        '"project":"project"}\'::jsonb, %s)',
        (job_id, run_id, worker_id, f"capture-{job_id}"),
    )
    return (
        job_id,
        run_id,
        CaptureOperationSnapshot(
            provider_kind="local-libvirt",
            resource_id=resource_id,
            system_id=system_id,
            domain_name="guest",
            request_digest="a" * 64,
        ),
    )


async def _exited_operation(
    admin: AsyncConnection,
    worker: AsyncConnection,
    worker_id: str,
    credential: SecretStr,
) -> tuple[CaptureOperation, UUID]:
    job_id, run_id, snapshot = await _seed_job(admin, worker_id)
    operation = await create_launching(worker, credential, job_id, 1, snapshot)
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
    return operation, run_id


def _artifact(run_id: UUID, key: str, etag: str = "etag-a") -> Artifact:
    now = datetime.now(UTC)
    return Artifact(
        id=uuid4(),
        owner_kind="runs",
        owner_id=run_id,
        object_key=key,
        etag=etag,
        sensitivity=Sensitivity.SENSITIVE,
        retention_class="pcap",
        run_id=run_id,
        created_at=now,
        updated_at=now,
    )


def _audit(run_id: UUID) -> AuditEvent:
    return AuditEvent(
        tool="control.capture_traffic",
        object_kind="runs",
        object_id=run_id,
        transition="capture_traffic",
        args={"run_id": str(run_id)},
        project="project",
    )


def test_publication_product_is_monotonic_and_exactly_replayable(migrated_url: str) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        worker = await _as_worker(migrated_url)
        credential = SecretStr("publication-owner")
        worker_id = "local:publication-owner"
        key = f"local/runs/{uuid4()}/pcap"
        try:
            await _register(admin, worker_id, credential)
            exited, run_id = await _exited_operation(admin, worker, worker_id, credential)
            assert exited.publication_state == "pending"
            assert exited.publication_started_at is None

            publishing = await begin_publication(worker, credential, exited.id, key)
            assert publishing.publication_state == "publishing"
            assert publishing.publication_object_key == key
            assert publishing.publication_started_at is not None
            assert await begin_publication(worker, credential, exited.id, key) == publishing
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await begin_publication(worker, credential, exited.id, f"{key}-conflict")

            journaled = await record_capture_version(
                worker, credential, exited.id, "capture-version-a", "etag-a"
            )
            assert journaled.cleanup_capture_version_id == "capture-version-a"
            assert journaled.publication_etag == "etag-a"
            assert (
                await record_capture_version(
                    worker, credential, exited.id, "capture-version-a", "etag-a"
                )
                == journaled
            )
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await record_capture_version(
                    worker, credential, exited.id, "capture-version-b", "etag-a"
                )

            artifact = _artifact(run_id, key)
            published = await commit_published(
                worker, credential, exited.id, artifact, _audit(run_id)
            )
            assert published.publication_state == "published"
            assert published.publication_artifact_id == artifact.id
            assert published.publication_closed_at is not None
            assert published.cleanup_capture_version_id == "capture-version-a"
            assert (
                await commit_published(worker, credential, exited.id, artifact, _audit(run_id))
                == published
            )
            conflicting_artifact = artifact.model_copy(update={"id": uuid4()})
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await commit_published(
                    worker, credential, exited.id, conflicting_artifact, _audit(run_id)
                )
            assert await (
                await admin.execute(
                    "SELECT object_key, etag FROM artifacts WHERE id = %s", (artifact.id,)
                )
            ).fetchone() == (key, "etag-a")
            assert await (
                await admin.execute(
                    "SELECT count(*) FROM audit_log WHERE object_id = %s", (run_id,)
                )
            ).fetchone() == (1,)

            disposed = await record_spool_disposed(worker, credential, exited.id)
            assert disposed.spool_disposed_at is not None
            assert await record_spool_disposed(worker, credential, exited.id) == disposed
        finally:
            await worker.close()
            await admin.close()

    asyncio.run(_run())


def test_canceling_never_moves_backward_and_discard_identity_is_exact(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        worker = await _as_worker(migrated_url)
        credential = SecretStr("discard-owner")
        worker_id = "local:discard-owner"
        key = f"local/runs/{uuid4()}/pcap"
        try:
            await _register(admin, worker_id, credential)
            exited, _run_id = await _exited_operation(admin, worker, worker_id, credential)
            canceling = await begin_cancel_publication(worker, credential, exited.id, key)
            assert canceling.publication_state == "canceling"
            assert await begin_cancel_publication(worker, credential, exited.id, key) == canceling
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await begin_cancel_publication(worker, credential, exited.id, f"{key}-conflict")
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await begin_publication(worker, credential, exited.id, key)

            cleanup = await record_cleanup_capture_version(
                worker, credential, exited.id, "capture-version-a"
            )
            assert cleanup.cleanup_capture_version_id == "capture-version-a"
            assert (
                await record_cleanup_capture_version(
                    worker, credential, exited.id, "capture-version-a"
                )
                == cleanup
            )
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await record_cleanup_capture_version(
                    worker, credential, exited.id, "capture-version-b"
                )

            discarded = await commit_discarded(worker, credential, exited.id, "tombstone-version-a")
            assert discarded.publication_state == "discarded"
            assert discarded.publication_tombstone_version == "tombstone-version-a"
            assert await (
                await admin.execute("SELECT count(*) FROM artifacts WHERE object_key = %s", (key,))
            ).fetchone() == (0,)
            assert (
                await commit_discarded(worker, credential, exited.id, "tombstone-version-a")
                == discarded
            )
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await commit_discarded(worker, credential, exited.id, "tombstone-version-b")
            closed = await record_spool_disposed(worker, credential, exited.id)
            assert closed.spool_disposed_at is not None
        finally:
            await worker.close()
            await admin.close()

    asyncio.run(_run())


def test_publication_rejects_wrong_credential_attempt_link_and_terminal_job(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        worker = await _as_worker(migrated_url)
        credential = SecretStr("fenced-owner")
        worker_id = "local:fenced-owner"
        key = f"local/runs/{uuid4()}/pcap"
        try:
            await _register(admin, worker_id, credential)
            exited, _run_id = await _exited_operation(admin, worker, worker_id, credential)
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await begin_publication(worker, SecretStr("wrong"), exited.id, key)

            await admin.execute(
                "UPDATE jobs SET current_capture_operation_id = NULL, attempt = attempt + 1 "
                "WHERE id = %s",
                (exited.job_id,),
            )
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await begin_publication(worker, credential, exited.id, key)

            await admin.execute(
                "UPDATE jobs SET attempt = %s, current_capture_operation_id = %s, "
                "state = 'canceled' WHERE id = %s",
                (exited.job_attempt, exited.id, exited.job_id),
            )
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await begin_publication(worker, credential, exited.id, key)
        finally:
            await worker.close()
            await admin.close()

    asyncio.run(_run())


@pytest.mark.parametrize("mismatch", ["key", "etag", "owner", "run_id"])
def test_published_requires_exact_artifact_row_and_etag_identity(
    migrated_url: str,
    mismatch: str,
) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        worker = await _as_worker(migrated_url)
        credential = SecretStr(f"identity-{mismatch}")
        worker_id = f"local:identity-{mismatch}"
        key = f"local/runs/{uuid4()}/pcap"
        try:
            await _register(admin, worker_id, credential)
            exited, run_id = await _exited_operation(admin, worker, worker_id, credential)
            await begin_publication(worker, credential, exited.id, key)
            await record_capture_version(worker, credential, exited.id, "capture-version", "etag-a")
            artifact = _artifact(run_id, key)
            changes = cast(
                dict[str, object],
                {
                    "key": {"object_key": f"{key}-wrong"},
                    "etag": {"etag": "etag-wrong"},
                    "owner": {"owner_id": uuid4()},
                    "run_id": {"run_id": uuid4()},
                }[mismatch],
            )
            conflicting = artifact.model_copy(update=changes)
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await commit_published(worker, credential, exited.id, conflicting, _audit(run_id))
            row = await (
                await admin.execute(
                    "SELECT publication_state, publication_artifact_id "
                    "FROM capture_operations WHERE id = %s",
                    (exited.id,),
                )
            ).fetchone()
            assert row == ("publishing", None)
            assert await (
                await admin.execute("SELECT count(*) FROM artifacts WHERE id = %s", (artifact.id,))
            ).fetchone() == (0,)
        finally:
            await worker.close()
            await admin.close()

    asyncio.run(_run())


def test_published_refuses_an_artifact_id_owned_by_another_row(migrated_url: str) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        worker = await _as_worker(migrated_url)
        credential = SecretStr("artifact-id-owner")
        worker_id = "local:artifact-id-owner"
        key = f"local/runs/{uuid4()}/pcap"
        try:
            await _register(admin, worker_id, credential)
            exited, run_id = await _exited_operation(admin, worker, worker_id, credential)
            await begin_publication(worker, credential, exited.id, key)
            await record_capture_version(worker, credential, exited.id, "capture-version", "etag-a")
            artifact = _artifact(run_id, key)
            await admin.execute(
                "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
                "retention_class, run_id) VALUES (%s, 'runs', %s, %s, 'occupied-etag', "
                "'sensitive', 'pcap', %s)",
                (artifact.id, run_id, f"{key}-occupied", run_id),
            )

            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await commit_published(worker, credential, exited.id, artifact, _audit(run_id))

            assert await (
                await admin.execute(
                    "SELECT publication_state, publication_artifact_id "
                    "FROM capture_operations WHERE id = %s",
                    (exited.id,),
                )
            ).fetchone() == ("publishing", None)
            assert await (
                await admin.execute(
                    "SELECT count(*) FROM audit_log WHERE object_id = %s", (run_id,)
                )
            ).fetchone() == (0,)
        finally:
            await worker.close()
            await admin.close()

    asyncio.run(_run())


def test_spool_disposal_requires_terminal_publication(migrated_url: str) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        worker = await _as_worker(migrated_url)
        credential = SecretStr("spool-owner")
        worker_id = "local:spool-owner"
        try:
            await _register(admin, worker_id, credential)
            exited, _run_id = await _exited_operation(admin, worker, worker_id, credential)
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await record_spool_disposed(worker, credential, exited.id)
            assert exited.spool_disposed_at is None
        finally:
            await worker.close()
            await admin.close()

    asyncio.run(_run())


def test_authorized_replacement_can_only_advance_cancellation(migrated_url: str) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        owner = await _as_worker(migrated_url)
        replacement = await _as_worker(migrated_url)
        owner_credential = SecretStr("publication-old-owner")
        replacement_credential = SecretStr("publication-replacement")
        owner_id = "local:publication-old-owner"
        replacement_id = "local:publication-replacement"
        key = f"local/runs/{uuid4()}/pcap"
        try:
            await _register(admin, owner_id, owner_credential)
            await _register(admin, replacement_id, replacement_credential)
            exited, _run_id = await _exited_operation(admin, owner, owner_id, owner_credential)
            await admin.execute(
                "UPDATE worker_incarnations SET state = 'terminated', outcome = 'killed', "
                "terminated_at = clock_timestamp() WHERE incarnation = %s",
                (owner_id,),
            )

            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await begin_publication(replacement, replacement_credential, exited.id, key)
            canceling = await begin_cancel_publication(
                replacement, replacement_credential, exited.id, key
            )
            assert canceling.publication_state == "canceling"
            cleanup = await record_cleanup_capture_version(
                replacement, replacement_credential, exited.id, "capture-version"
            )
            assert cleanup.cleanup_capture_version_id == "capture-version"
            discarded = await commit_discarded(
                replacement, replacement_credential, exited.id, "tombstone-version"
            )
            assert discarded.publication_state == "discarded"
            assert (
                await record_spool_disposed(replacement, replacement_credential, exited.id)
            ).spool_disposed_at is not None
        finally:
            await replacement.close()
            await owner.close()
            await admin.close()

    asyncio.run(_run())


def test_publication_revalidates_current_link_after_lock_contention(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        blocker = await connect(migrated_url)
        worker = await _as_worker(migrated_url)
        credential = SecretStr("link-race-owner")
        worker_id = "local:link-race-owner"
        key = f"local/runs/{uuid4()}/pcap"
        operation: CaptureOperation | None = None
        try:
            await _register(admin, worker_id, credential)
            operation, _run_id = await _exited_operation(admin, worker, worker_id, credential)
            await blocker.execute(
                "SELECT pg_advisory_lock("
                "hashtextextended('kdive:capture-operation:' || %s::text, 1951))",
                (operation.id,),
            )
            transition = asyncio.create_task(
                begin_publication(worker, credential, operation.id, key)
            )
            await wait_until_backend_waiting(admin, worker.info.backend_pid, locktype="advisory")
            await admin.execute(
                "UPDATE jobs SET current_capture_operation_id = NULL WHERE id = %s",
                (operation.job_id,),
            )
            await blocker.execute(
                "SELECT pg_advisory_unlock("
                "hashtextextended('kdive:capture-operation:' || %s::text, 1951))",
                (operation.id,),
            )
            with pytest.raises(ValueError, match="capture publication transition was refused"):
                await transition
            row = await (
                await admin.execute(
                    "SELECT publication_state, publication_object_key "
                    "FROM capture_operations WHERE id = %s",
                    (operation.id,),
                )
            ).fetchone()
            assert row == ("pending", None)
        finally:
            if operation is not None:
                await blocker.execute(
                    "SELECT pg_advisory_unlock("
                    "hashtextextended('kdive:capture-operation:' || %s::text, 1951))",
                    (operation.id,),
                )
            await worker.close()
            await blocker.close()
            await admin.close()

    asyncio.run(_run())
