"""Device ledger writes retain a credential-bound job row lock (ADR-0831)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.repositories import JOBS
from kdive.domain.capacity.state import SystemDeviceState
from kdive.domain.operations.jobs import Job, JobKind, PowerAction
from kdive.jobs import queue
from kdive.jobs.payloads import (
    ActivePayloadModel,
    Authorizing,
    PlugDevicePayload,
    PowerPayload,
    UnplugDevicePayload,
)
from tests.conftest import _RoleDsns
from tests.db.system_ledger_support import connect
from tests.db_waits import wait_until_backend_waiting
from tests.support.worker_fence import dequeue_as_current_worker, incarnation_credential


async def _claimed(conn: psycopg.AsyncConnection, kind: JobKind) -> tuple[Job, SecretStr]:
    system_id, device_id = str(uuid4()), str(uuid4())
    payload: ActivePayloadModel
    if kind is JobKind.PLUG_DEVICE:
        payload = PlugDevicePayload(system_id=system_id, device_id=device_id)
    elif kind is JobKind.POWER:
        payload = PowerPayload(system_id=system_id, action=PowerAction.ON)
    else:
        payload = UnplugDevicePayload(
            system_id=system_id, device_id=device_id, from_state=SystemDeviceState.FAILED
        )
    queued = await queue.enqueue(
        conn,
        kind,
        payload,
        Authorizing(principal="p", agent_session=None, project="test"),
        str(uuid4()),
        max_attempts=2,
    )
    worker_id = f"device-fence-{uuid4()}"
    job = await dequeue_as_current_worker(conn, worker_id)
    assert job is not None and job.id == queued.id
    return job, incarnation_credential(worker_id)


async def _lock(conn: psycopg.AsyncConnection, job: Job, credential: SecretStr) -> bool:
    cur = await conn.execute(
        "SELECT public.lock_device_job_attempt(%s, sha256(convert_to(%s, 'UTF8')), "
        "%s, %s, %s, %s, %s)",
        (
            job.id,
            credential.get_secret_value(),
            job.attempt,
            job.worker_id,
            job.created_at,
            job.kind.value,
            Jsonb(job.payload),
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(
    "kind", [JobKind.PLUG_DEVICE, JobKind.UNPLUG_DEVICE, JobKind.FORCE_UNPLUG_DEVICE]
)
def test_worker_can_lock_its_exact_device_claim_without_changing_it(
    migrated_url: str, authority_role_dsns: _RoleDsns, kind: JobKind
) -> None:
    async def run() -> None:
        async with (
            await connect(migrated_url) as owner,
            await connect(authority_role_dsns("kdive_worker")) as worker,
        ):
            job, credential = await _claimed(owner, kind)
            async with worker.transaction():
                assert await _lock(worker, job, credential)
            assert await JOBS.get(owner, job.id) == job

    asyncio.run(run())


@pytest.mark.parametrize("fact", ["attempt", "worker_id", "created_at", "kind", "payload"])
def test_a_changed_claim_snapshot_cannot_be_locked(
    migrated_url: str, authority_role_dsns: _RoleDsns, fact: str
) -> None:
    async def run() -> None:
        async with (
            await connect(migrated_url) as owner,
            await connect(authority_role_dsns("kdive_worker")) as worker,
        ):
            job, credential = await _claimed(owner, JobKind.PLUG_DEVICE)
            changed = {
                "attempt": job.attempt + 1,
                "worker_id": "another-worker",
                "created_at": job.created_at + timedelta(microseconds=1),
                "kind": JobKind.UNPLUG_DEVICE,
                "payload": {**job.payload, "device_id": str(uuid4())},
            }
            async with worker.transaction():
                assert not await _lock(
                    worker, job.model_copy(update={fact: changed[fact]}), credential
                )
            assert await JOBS.get(owner, job.id) == job

    asyncio.run(run())


def test_unknown_or_another_workers_credential_cannot_lock_the_claim(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with (
            await connect(migrated_url) as owner,
            await connect(authority_role_dsns("kdive_worker")) as worker,
        ):
            job, _credential = await _claimed(owner, JobKind.PLUG_DEVICE)
            _other, other_credential = await _claimed(owner, JobKind.PLUG_DEVICE)
            for credential in (SecretStr("unregistered credential"), other_credential):
                async with worker.transaction():
                    assert not await _lock(worker, job, credential)
            assert await JOBS.get(owner, job.id) == job

    asyncio.run(run())


@pytest.mark.parametrize("case", ["nondevice", "terminal"])
def test_only_running_device_claims_can_be_locked(
    migrated_url: str, authority_role_dsns: _RoleDsns, case: str
) -> None:
    async def run() -> None:
        async with (
            await connect(migrated_url) as owner,
            await connect(authority_role_dsns("kdive_worker")) as worker,
        ):
            job, credential = await _claimed(
                owner, JobKind.POWER if case == "nondevice" else JobKind.PLUG_DEVICE
            )
            if case == "terminal":
                await owner.execute("UPDATE jobs SET state = 'succeeded' WHERE id = %s", (job.id,))
            before = await JOBS.get(owner, job.id)
            async with worker.transaction():
                assert not await _lock(worker, job, credential)
            assert await JOBS.get(owner, job.id) == before

    asyncio.run(run())


@pytest.mark.parametrize("role", ["kdive_server", "kdive_reconciler", "kdive_lifecycle_witness"])
def test_other_process_roles_cannot_call_the_device_claim_lock(
    migrated_url: str, authority_role_dsns: _RoleDsns, role: str
) -> None:
    async def run() -> None:
        async with await connect(migrated_url) as owner:
            job, credential = await _claimed(owner, JobKind.PLUG_DEVICE)
            cur = await owner.execute(
                "SELECT NOT EXISTS (SELECT 1 FROM pg_proc AS p, "
                "LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) AS a "
                "WHERE p.oid = 'public.lock_device_job_attempt(uuid, bytea, integer, "
                "text, timestamptz, text, jsonb)'::regprocedure "
                "AND a.grantee = 0 AND a.privilege_type = 'EXECUTE')"
            )
            assert await cur.fetchone() == (True,)
            cur = await owner.execute(
                "SELECT has_function_privilege(%s, 'public.lock_device_job_attempt(uuid, "
                "bytea, integer, text, timestamptz, text, jsonb)', 'EXECUTE')",
                (role,),
            )
            assert await cur.fetchone() == (False,)
            if role == "kdive_lifecycle_witness":
                # The shared LOGIN fixture excludes witnesses; exercise its exact role here.
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    async with owner.transaction():
                        await owner.execute("SET LOCAL ROLE kdive_lifecycle_witness")
                        await _lock(owner, job, credential)
                return
        async with await connect(authority_role_dsns(role)) as denied:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with denied.transaction():
                    await _lock(denied, job, credential)

    asyncio.run(run())


def test_worker_still_cannot_lock_or_update_jobs_directly(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with await connect(migrated_url) as owner:
            job, _credential = await _claimed(owner, JobKind.PLUG_DEVICE)
        async with await connect(authority_role_dsns("kdive_worker")) as worker:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with worker.transaction():
                    await worker.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (job.id,))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with worker.transaction():
                    await worker.execute("UPDATE jobs SET attempt = 99 WHERE id = %s", (job.id,))

    asyncio.run(run())


def test_device_claim_lock_blocks_a_job_writer_until_ledger_commit(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with (
            await connect(migrated_url) as owner,
            await connect(authority_role_dsns("kdive_worker")) as worker,
            await connect(authority_role_dsns("kdive_reconciler")) as writer,
        ):
            job, credential = await _claimed(owner, JobKind.PLUG_DEVICE)
            task = None
            try:
                async with worker.transaction():
                    assert await _lock(worker, job, credential)
                    task = asyncio.create_task(
                        writer.execute(
                            "UPDATE jobs SET state = 'failed', error_category = 'control_failure' "
                            "WHERE id = %s RETURNING id",
                            (job.id,),
                        )
                    )
                    await wait_until_backend_waiting(
                        owner, writer.info.backend_pid, locktype="transactionid"
                    )
                    assert not task.done()
                    assert await JOBS.get(owner, job.id) == job
                cur = await asyncio.wait_for(task, 5)
                assert await cur.fetchone() == (job.id,)
            finally:
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)

    asyncio.run(run())
