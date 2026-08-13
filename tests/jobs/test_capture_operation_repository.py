"""Durable transition and authority fences for supervised capture operations."""

from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db import migrate
from kdive.jobs import queue
from kdive.jobs.capture_operations.repository import (
    CaptureOperation,
    CaptureOperationIdentity,
    CaptureOperationSnapshot,
    RecoveryEvidence,
    acknowledge_exit,
    create_launching,
    list_recovery_candidates,
    mark_running,
    record_identity,
    recover_operation,
    request_cancel,
)
from tests.reconciler.conftest import connect


def _hash(credential: SecretStr) -> bytes:
    return hashlib.sha256(credential.get_secret_value().encode()).digest()


async def _as_role(url: str, role: str) -> AsyncConnection:
    conn = await connect(url)
    await conn.execute(sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(role)))
    return conn


async def _seed_job(
    conn: AsyncConnection,
    worker_id: str,
    credential: SecretStr,
    *,
    attempt: int = 1,
) -> tuple[UUID, CaptureOperationSnapshot]:
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
        "(%s, 'capture_traffic', jsonb_build_object('run_id', %s::text), 'running', "
        "%s, 3, %s, now() + interval '5 min', now(), "
        '\'{"principal":"principal","agent_session":null,'
        '"project":"project"}\'::jsonb, %s)',
        (job_id, run_id, attempt, worker_id, f"capture-{job_id}"),
    )
    return job_id, CaptureOperationSnapshot(
        provider_kind="local-libvirt",
        resource_id=resource_id,
        system_id=system_id,
        domain_name="guest",
        request_digest="a" * 64,
    )


async def _register(
    admin: AsyncConnection,
    worker_id: str,
    credential: SecretStr,
    *,
    authority_kind: str = "local",
    binding: dict[str, str] | None = None,
) -> None:
    await admin.execute(
        "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
        "fence_protocol, credential_hash) VALUES (%s, %s, %s, 3, %s)",
        (
            worker_id,
            authority_kind,
            Jsonb(binding or {"host": "host-a"}),
            _hash(credential),
        ),
    )


def _launch_abort_evidence(operation: CaptureOperation, **changes: object) -> RecoveryEvidence:
    provider_quiescence: dict[str, object] = {
        "evidence_kind": "closed_gate_boundary_token_scan_v1",
        "gate_closed": True,
        "boundary_scan_complete": True,
        "boundary_processes_absent": True,
        "host_instance": operation.host_instance,
        "launch_token": operation.launch_token,
        "launch_token_absent": True,
    }
    provider_quiescence.update(changes)
    return RecoveryEvidence(
        process_absent=True,
        provider_quiescence=provider_quiescence,
        exit_outcome="aborted_before_identity",
        exit_code=None,
    )


def test_capture_operation_owner_transitions_are_unique_and_idempotent(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        worker = await _as_role(migrated_url, "kdive_worker")
        credential = SecretStr("owner-credential")
        worker_id = "local:owner"
        try:
            await _register(admin, worker_id, credential)
            job_id, snapshot = await _seed_job(admin, worker_id, credential)
            mismatched_snapshot = CaptureOperationSnapshot(
                provider_kind="remote-libvirt",
                resource_id=snapshot.resource_id,
                system_id=snapshot.system_id,
                domain_name=snapshot.domain_name,
                request_digest=snapshot.request_digest,
            )
            with pytest.raises(PermissionError, match="capture operation launch was refused"):
                await create_launching(worker, credential, job_id, 1, mismatched_snapshot)
            operation = await create_launching(worker, credential, job_id, 1, snapshot)
            replay = await create_launching(worker, credential, job_id, 1, snapshot)
            assert replay == operation
            assert operation.state == "launching"
            assert len(operation.launch_token) == 64

            identity = CaptureOperationIdentity("host-a", "boot-a", 314, 159)
            gated = await record_identity(worker, credential, operation.id, identity)
            assert gated.state == "gated"
            assert await record_identity(worker, credential, operation.id, identity) == gated
            running = await mark_running(worker, credential, operation.id)
            assert running.state == "running"
            canceled = await request_cancel(worker, credential, operation.id)
            assert canceled.state == "cancel_requested"
            evidence = RecoveryEvidence(
                process_absent=True,
                provider_quiescence={"result": "absent", "qom_id": f"kdive-dump-{job_id}"},
                exit_outcome="canceled",
                exit_code=-15,
            )
            exited = await acknowledge_exit(worker, credential, operation.id, evidence)
            assert exited.state == "exited"
            assert await acknowledge_exit(worker, credential, operation.id, evidence) == exited

            other_job_id, _ = await _seed_job(admin, worker_id, credential)
            with pytest.raises(
                errors.CheckViolation,
                match="current capture operation must match the exact job attempt",
            ):
                await admin.execute(
                    "UPDATE jobs SET current_capture_operation_id = %s WHERE id = %s",
                    (operation.id, other_job_id),
                )

            with pytest.raises(errors.UniqueViolation):
                await admin.execute(
                    "INSERT INTO capture_operations (job_id, job_attempt, worker_incarnation, "
                    "provider_kind, resource_id, system_id, domain_name, request_digest, "
                    "launch_token, host_instance) SELECT job_id, job_attempt, "
                    "worker_incarnation, provider_kind, resource_id, system_id, domain_name, "
                    "request_digest, repeat('b', 64), host_instance FROM capture_operations "
                    "WHERE id = %s",
                    (operation.id,),
                )
        finally:
            await worker.close()
            await admin.close()

    asyncio.run(_run())


def test_conflicting_or_non_owner_transitions_fail_closed(migrated_url: str) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        owner = await _as_role(migrated_url, "kdive_worker")
        stranger = await _as_role(migrated_url, "kdive_worker")
        owner_credential = SecretStr("owner")
        stranger_credential = SecretStr("stranger")
        try:
            await _register(admin, "local:owner", owner_credential)
            await _register(
                admin,
                "local:stranger",
                stranger_credential,
                binding={"host": "host-b"},
            )
            job_id, snapshot = await _seed_job(admin, "local:owner", owner_credential)
            operation = await create_launching(owner, owner_credential, job_id, 1, snapshot)
            identity = CaptureOperationIdentity("host-a", "boot-a", 20, 30)
            await record_identity(owner, owner_credential, operation.id, identity)

            with pytest.raises(PermissionError, match="capture operation transition was refused"):
                await request_cancel(stranger, stranger_credential, operation.id)
            with pytest.raises(ValueError, match="capture operation transition was refused"):
                await record_identity(
                    owner,
                    owner_credential,
                    operation.id,
                    CaptureOperationIdentity("host-a", "boot-a", 21, 30),
                )
            with pytest.raises(ValueError, match="capture operation transition was refused"):
                await mark_running(owner, owner_credential, uuid4())
        finally:
            await stranger.close()
            await owner.close()
            await admin.close()

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("authority_kind", "owner_binding", "replacement_binding", "old_terminated", "allowed"),
    [
        (
            "local",
            {"host": "host-a"},
            {"host": "host-a"},
            False,
            False,
        ),
        (
            "local",
            {"host": "host-a"},
            {"host": "host-a"},
            True,
            True,
        ),
        (
            "local",
            {"host": "host-a"},
            {"host": "host-b"},
            True,
            False,
        ),
        (
            "docker",
            {"container_id": "a" * 64, "project": "kdive", "service": "worker", "ordinal": "0"},
            {"container_id": "b" * 64, "project": "kdive", "service": "worker", "ordinal": "0"},
            True,
            True,
        ),
        (
            "docker",
            {"container_id": "a" * 64, "project": "kdive", "service": "worker", "ordinal": "0"},
            {"container_id": "b" * 64, "project": "other", "service": "worker", "ordinal": "0"},
            True,
            False,
        ),
    ],
)
def test_recovery_is_derived_from_durable_authority_scope(
    migrated_url: str,
    authority_kind: str,
    owner_binding: dict[str, str],
    replacement_binding: dict[str, str],
    old_terminated: bool,
    allowed: bool,
) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        replacement = await _as_role(migrated_url, "kdive_worker")
        owner_credential = SecretStr("old-owner")
        replacement_credential = SecretStr("replacement")
        try:
            await _register(
                admin,
                "local:old",
                owner_credential,
                authority_kind=authority_kind,
                binding=owner_binding,
            )
            await _register(
                admin,
                "local:replacement",
                replacement_credential,
                authority_kind=authority_kind,
                binding=replacement_binding,
            )
            job_id, snapshot = await _seed_job(admin, "local:old", owner_credential)
            owner = await _as_role(migrated_url, "kdive_worker")
            try:
                operation = await create_launching(owner, owner_credential, job_id, 1, snapshot)
                await record_identity(
                    owner,
                    owner_credential,
                    operation.id,
                    CaptureOperationIdentity(operation.host_instance, "boot-a", 40, 50),
                )
                await mark_running(owner, owner_credential, operation.id)
            finally:
                await owner.close()
            if old_terminated:
                await admin.execute(
                    "UPDATE worker_incarnations SET state = 'terminated', outcome = 'killed', "
                    "terminated_at = clock_timestamp() WHERE incarnation = 'local:old'"
                )
            evidence = RecoveryEvidence(
                process_absent=True,
                provider_quiescence={"result": "absent"},
                exit_outcome="recovered",
                exit_code=-9,
            )
            if allowed:
                recovered = await recover_operation(
                    replacement, replacement_credential, operation.id, evidence
                )
                assert recovered.state == "exited"
            else:
                with pytest.raises(PermissionError, match="capture operation recovery was refused"):
                    await recover_operation(
                        replacement, replacement_credential, operation.id, evidence
                    )
        finally:
            await replacement.close()
            await admin.close()

    asyncio.run(_run())


def test_recovery_candidates_are_credential_fenced_and_bounded(migrated_url: str) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        replacement = await _as_role(migrated_url, "kdive_worker")
        owner = await _as_role(migrated_url, "kdive_worker")
        owner_credential = SecretStr("candidate-owner")
        replacement_credential = SecretStr("candidate-replacement")
        other_credential = SecretStr("candidate-other")
        stale_credential = SecretStr("candidate-stale")
        try:
            await _register(admin, "local:candidate-owner", owner_credential)
            await _register(admin, "local:candidate-replacement", replacement_credential)
            await _register(
                admin,
                "local:candidate-other",
                other_credential,
                binding={"host": "host-b"},
            )
            await _register(admin, "local:candidate-stale", stale_credential)
            job_id, snapshot = await _seed_job(admin, "local:candidate-owner", owner_credential)
            operation = await create_launching(owner, owner_credential, job_id, 1, snapshot)
            await admin.execute(
                "UPDATE worker_incarnations SET state = 'terminated', outcome = 'killed', "
                "terminated_at = clock_timestamp() "
                "WHERE incarnation IN ('local:candidate-owner', 'local:candidate-stale')"
            )

            candidates = await list_recovery_candidates(replacement, replacement_credential)
            assert len(candidates) == 1
            candidate = candidates[0]
            assert candidate.id == operation.id
            assert candidate.launch_token == operation.launch_token
            assert candidate.state == "launching"
            assert not hasattr(candidate, "request_digest")

            assert await list_recovery_candidates(replacement, other_credential) == ()
            assert await list_recovery_candidates(replacement, stale_credential) == ()
            with pytest.raises(errors.InsufficientPrivilege):
                await replacement.execute("SELECT id FROM capture_operations")
        finally:
            await owner.close()
            await replacement.close()
            await admin.close()

    asyncio.run(_run())


def test_recovery_candidate_is_revalidated_after_discovery_race(migrated_url: str) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        replacement = await _as_role(migrated_url, "kdive_worker")
        owner = await _as_role(migrated_url, "kdive_worker")
        owner_credential = SecretStr("candidate-race-owner")
        replacement_credential = SecretStr("candidate-race-replacement")
        try:
            await _register(admin, "local:candidate-race-owner", owner_credential)
            await _register(admin, "local:candidate-race-new", replacement_credential)
            job_id, snapshot = await _seed_job(
                admin, "local:candidate-race-owner", owner_credential
            )
            operation = await create_launching(owner, owner_credential, job_id, 1, snapshot)
            await admin.execute(
                "UPDATE worker_incarnations SET state = 'terminated', outcome = 'killed', "
                "terminated_at = clock_timestamp() "
                "WHERE incarnation = 'local:candidate-race-owner'"
            )
            assert (await list_recovery_candidates(replacement, replacement_credential))[
                0
            ].id == operation.id

            await admin.execute(
                "UPDATE worker_incarnations SET state = 'active', outcome = NULL, "
                "terminated_at = NULL WHERE incarnation = 'local:candidate-race-owner'"
            )
            with pytest.raises(PermissionError, match="capture operation recovery was refused"):
                await recover_operation(
                    replacement,
                    replacement_credential,
                    operation.id,
                    _launch_abort_evidence(operation),
                )
        finally:
            await owner.close()
            await replacement.close()
            await admin.close()

    asyncio.run(_run())


def test_owner_launch_abort_requires_evidence_bound_to_operation(migrated_url: str) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        worker = await _as_role(migrated_url, "kdive_worker")
        credential = SecretStr("launch-owner")
        try:
            await _register(admin, "local:launch-owner", credential)

            incomplete_job, incomplete_snapshot = await _seed_job(
                admin, "local:launch-owner", credential
            )
            incomplete = await create_launching(
                worker, credential, incomplete_job, 1, incomplete_snapshot
            )
            with pytest.raises(ValueError, match="capture operation transition was refused"):
                await acknowledge_exit(
                    worker,
                    credential,
                    incomplete.id,
                    RecoveryEvidence(
                        process_absent=True,
                        provider_quiescence={"launch_token_absent": True},
                        exit_outcome="aborted_before_identity",
                        exit_code=None,
                    ),
                )

            mismatch_job, mismatch_snapshot = await _seed_job(
                admin, "local:launch-owner", credential
            )
            mismatch = await create_launching(
                worker, credential, mismatch_job, 1, mismatch_snapshot
            )
            with pytest.raises(ValueError, match="capture operation transition was refused"):
                await acknowledge_exit(
                    worker,
                    credential,
                    mismatch.id,
                    _launch_abort_evidence(mismatch, launch_token="0" * 64),
                )

            accepted_job, accepted_snapshot = await _seed_job(
                admin, "local:launch-owner", credential
            )
            accepted = await create_launching(
                worker, credential, accepted_job, 1, accepted_snapshot
            )
            exited = await acknowledge_exit(
                worker, credential, accepted.id, _launch_abort_evidence(accepted)
            )
            assert exited.state == "exited"
        finally:
            await worker.close()
            await admin.close()

    asyncio.run(_run())


def test_recovered_launch_abort_requires_evidence_bound_to_operation(migrated_url: str) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        replacement = await _as_role(migrated_url, "kdive_worker")
        owner = await _as_role(migrated_url, "kdive_worker")
        owner_credential = SecretStr("launch-old-owner")
        replacement_credential = SecretStr("launch-replacement")
        try:
            await _register(admin, "local:launch-old", owner_credential)
            await _register(admin, "local:launch-new", replacement_credential)
            jobs = [await _seed_job(admin, "local:launch-old", owner_credential) for _ in range(3)]
            operations = [
                await create_launching(owner, owner_credential, job_id, 1, snapshot)
                for job_id, snapshot in jobs
            ]
            await admin.execute(
                "UPDATE worker_incarnations SET state = 'terminated', outcome = 'killed', "
                "terminated_at = clock_timestamp() WHERE incarnation = 'local:launch-old'"
            )

            with pytest.raises(PermissionError, match="capture operation recovery was refused"):
                await recover_operation(
                    replacement,
                    replacement_credential,
                    operations[0].id,
                    RecoveryEvidence(
                        process_absent=True,
                        provider_quiescence={"launch_token_absent": True},
                        exit_outcome="aborted_before_identity",
                        exit_code=None,
                    ),
                )
            with pytest.raises(PermissionError, match="capture operation recovery was refused"):
                await recover_operation(
                    replacement,
                    replacement_credential,
                    operations[1].id,
                    _launch_abort_evidence(operations[1], host_instance="other-host"),
                )
            exited = await recover_operation(
                replacement,
                replacement_credential,
                operations[2].id,
                _launch_abort_evidence(operations[2]),
            )
            assert exited.state == "exited"
        finally:
            await owner.close()
            await replacement.close()
            await admin.close()

    asyncio.run(_run())


def test_capture_retry_is_not_charged_until_prior_evidence_is_complete(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        admin = await connect(migrated_url)
        worker_id = "local:retry"
        credential = SecretStr("retry-credential")
        await _register(admin, worker_id, credential)
        job_id, snapshot = await _seed_job(admin, worker_id, credential)
        worker = await _as_role(migrated_url, "kdive_worker")
        try:
            operation = await create_launching(worker, credential, job_id, 1, snapshot)
            await admin.execute(
                "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = %s",
                (job_id,),
            )
            refused = await queue.dequeue(
                worker,
                worker_id,
                incarnation_credential=credential,
            )
            assert refused is None
            row = await (
                await admin.execute("SELECT attempt FROM jobs WHERE id = %s", (job_id,))
            ).fetchone()
            assert row == (1,)

            await acknowledge_exit(
                worker,
                credential,
                operation.id,
                RecoveryEvidence(
                    process_absent=True,
                    provider_quiescence={"result": "not_started"},
                    exit_outcome="aborted_before_spawn",
                    exit_code=None,
                ),
            )
            claimed = await queue.dequeue(
                worker,
                worker_id,
                incarnation_credential=credential,
            )
            assert claimed is not None
            assert claimed.attempt == 2
        finally:
            await worker.close()
            await admin.close()

    asyncio.run(_run())


def test_pre_0112_schema_has_no_capture_operation_relation(pg_conn: psycopg.Connection) -> None:
    for migration in migrate.discover_migrations():
        if migration.version == "0112":
            break
        pg_conn.execute(migration.sql.encode())
    assert pg_conn.execute("SELECT to_regclass('public.capture_operations')").fetchone() == (None,)
