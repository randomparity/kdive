from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.catalog.registration import register_artifact_row
from kdive.artifacts.storage import (
    ArtifactWriteRequest,
    ConditionalArtifactWriteRequest,
    PresignPutRequest,
    StoredArtifact,
    VersionPage,
)
from kdive.artifacts.vmcore_retention import (
    VmcoreExecution,
    await_vmcore_io,
    protect_vmcore_url,
    publish_vmcore,
    signed_get_deadline,
)
from kdive.db.locks import LockScope, advisory_xact_lock, try_advisory_xact_lock
from kdive.db.repositories import ARTIFACTS
from kdive.db.vmcore_retention_migration import import_vmcore_baseline
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, CaptureVmcorePayload
from kdive.reconciler.cleanup.artifacts.vmcore_retention import sweep_vmcore_retention
from kdive.reconciler.cleanup.uploads.upload_fences import (
    UploadOrphanCandidate,
    owner_key_is_fenced,
    reclaimable_upload_keys,
)
from kdive.store.objectstore import ObjectStore
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.store.conftest import _empty_bucket
from tests.store.conftest import minio_store as minio_store


async def seed_pair(
    pool: AsyncConnectionPool, store: ObjectStore
) -> tuple[UUID, UUID, tuple[str, str]]:
    system_id = await seed_crashed_system(pool)
    run_id = UUID(await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id=None))
    execution, job_id = uuid4(), uuid4()
    keys = (
        f"local/runs/{run_id}/vmcore-host_dump-{execution}",
        f"local/runs/{run_id}/vmcore-host_dump-{execution}-redacted",
    )
    stored = []
    for key, sensitivity in zip(keys, (Sensitivity.SENSITIVE, Sensitivity.REDACTED), strict=True):
        value = store.create_if_absent(
            ConditionalArtifactWriteRequest(
                key=key,
                data=b"core-test-bytes",
                metadata={"vmcore-execution": str(execution), "vmcore-kind": "capture"},
                sensitivity=sensitivity,
                retention_class="vmcore",
            )
        )
        assert isinstance(value, StoredArtifact)
        stored.append(value)
    async with pool.connection() as conn, conn.transaction():
        for value in stored:
            await ARTIFACTS.insert(
                conn, register_artifact_row(value, owner_kind="runs", owner_id=run_id)
            )
        await conn.execute(
            "UPDATE artifacts SET created_at=now()-interval '400 days' WHERE owner_id=%s",
            (run_id,),
        )
        await conn.execute(
            "UPDATE investigations SET state='closed' WHERE id=(SELECT investigation_id "
            "FROM runs WHERE id=%s)",
            (run_id,),
        )
        await conn.execute(
            "INSERT INTO "
            "jobs(id,kind,state,attempt,max_attempts,lease_expires_at,authorizing,dedup_key) "
            "VALUES (%s,'capture_vmcore','running',1,3,now()+interval '5 minutes','{}',%s)",
            (job_id, str(job_id)),
        )
        await conn.execute("SET LOCAL ROLE kdive_worker")
        await conn.execute(
            "INSERT INTO vmcore_capture_executions "
            "(id,run_id,job_id,job_attempt,method,raw_key,redacted_key) "
            "VALUES (%s,%s,%s,1,'host_dump',%s,%s)",
            (execution, run_id, job_id, *keys),
        )
        record = VmcoreExecution(
            execution,
            run_id,
            job_id,
            1,
            CaptureMethod.HOST_DUMP,
            *keys,
            "capturing",
            False,
            None,
            None,
        )
        await publish_vmcore(conn, record, stored[0], stored[1])
        await conn.execute("RESET ROLE")
        await conn.execute("UPDATE jobs SET state='succeeded' WHERE id=%s", (job_id,))
        await conn.execute(
            "INSERT INTO vmcore_retention_baseline(mode) VALUES ('fresh') ON CONFLICT DO NOTHING"
        )
    return run_id, execution, keys


@pytest.mark.parametrize(
    "run_state,investigation_state,days,reclaims",
    [
        ("succeeded", "closed", 30, True),
        ("failed", "closed", 30, True),
        ("canceled", "abandoned", 30, True),
        ("running", "closed", 30, False),
        ("created", "closed", 30, False),
        ("succeeded", "open", 30, False),
        ("succeeded", "active", 30, False),
        ("succeeded", "closed", None, False),
    ],
)
def test_terminal_pair_policy(
    migrated_url: str,
    minio_store: ObjectStore,
    run_state: str,
    investigation_state: str,
    days: int | None,
    reclaims: bool,
) -> None:
    async def check() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            run_id, execution, keys = await seed_pair(pool, minio_store)
            try:
                async with pool.connection() as conn, conn.transaction():
                    await conn.execute("UPDATE runs SET state=%s WHERE id=%s", (run_state, run_id))
                    await conn.execute(
                        "UPDATE investigations SET state=%s WHERE id=(SELECT "
                        "investigation_id FROM runs WHERE id=%s)",
                        (investigation_state, run_id),
                    )
                async with pool.connection() as conn:
                    await conn.execute("SET ROLE kdive_reconciler")
                    await conn.commit()
                    try:
                        await sweep_vmcore_retention(conn, minio_store, days)
                    finally:
                        await conn.execute("RESET ROLE")
                        await conn.commit()
                async with pool.connection() as conn:
                    row = await (
                        await conn.execute(
                            "SELECT state FROM vmcore_capture_executions WHERE id=%s",
                            (execution,),
                        )
                    ).fetchone()
                    assert row == (("retired",) if reclaims else ("published",))
                for key in keys:
                    history = minio_store.capture_exact_versions(key, 10)
                    assert history.history_complete
                    assert len(history.targets) == 1
                    head = minio_store.head(key)
                    assert head is not None
                    assert (head.size_bytes == 0) is reclaims
                    assert head.metadata["vmcore-kind"] == ("tombstone" if reclaims else "capture")
            finally:
                for key in keys:
                    for version in minio_store.capture_exact_versions(key, 20).targets:
                        minio_store.delete_version(key, version.version_id)

    asyncio.run(check())


@pytest.mark.parametrize("guard", ["later_sibling", "download", "queued_capture"])
def test_pair_guards_defer_retirement(
    migrated_url: str, minio_store: ObjectStore, guard: str
) -> None:
    async def check() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            run_id, execution, keys = await seed_pair(pool, minio_store)
            try:
                async with pool.connection() as conn, conn.transaction():
                    if guard == "later_sibling":
                        await conn.execute(
                            "UPDATE artifacts SET created_at=now() WHERE object_key=%s",
                            (keys[1],),
                        )
                        await conn.execute(
                            "UPDATE vmcore_capture_executions SET captured_at=(SELECT "
                            "max(created_at) "
                            "FROM artifacts WHERE object_key IN (%s,%s)) WHERE id=%s",
                            (*keys, execution),
                        )
                    elif guard == "download":
                        await conn.execute(
                            "UPDATE runs SET "
                            "vmcore_download_protected_until=now()+interval '1 hour' WHERE id=%s",
                            (run_id,),
                        )
                    else:
                        await queue.enqueue(
                            conn,
                            JobKind.CAPTURE_VMCORE,
                            CaptureVmcorePayload(
                                run_id=str(run_id), method=CaptureMethod.HOST_DUMP
                            ),
                            Authorizing(principal="test", agent_session="test", project="proj"),
                            f"{run_id}:retention-test",
                        )
                async with pool.connection() as conn:
                    await sweep_vmcore_retention(conn, minio_store, 30)
                    row = await (
                        await conn.execute(
                            "SELECT state FROM vmcore_capture_executions WHERE id=%s",
                            (execution,),
                        )
                    ).fetchone()
                    assert row == ("published",)
                for key in keys:
                    head = minio_store.head(key)
                    assert head is not None and head.size_bytes > 0
            finally:
                for key in keys:
                    for version in minio_store.capture_exact_versions(key, 20).targets:
                        minio_store.delete_version(key, version.version_id)

    asyncio.run(check())


def test_actual_signed_expiration_is_persisted(migrated_url: str, minio_store: ObjectStore) -> None:
    async def check() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            run_id, _, keys = await seed_pair(pool, minio_store)
            try:
                url = minio_store.presign_get(keys[0], expires_in=900)
                async with (
                    pool.connection() as conn,
                    conn.transaction(),
                    advisory_xact_lock(conn, LockScope.RUN, run_id),
                ):
                    await conn.execute("SET LOCAL ROLE kdive_server")
                    server_time, expires = await protect_vmcore_url(conn, run_id, url)
                    assert expires == signed_get_deadline(url)
                    assert expires > server_time
                async with pool.connection() as conn:
                    row = await (
                        await conn.execute(
                            "SELECT vmcore_download_protected_until FROM runs WHERE id=%s",
                            (run_id,),
                        )
                    ).fetchone()
                    assert row == (expires,)
                    await conn.commit()
                    await sweep_vmcore_retention(conn, minio_store, 30)
                head = minio_store.head(keys[0])
                assert head is not None and head.size_bytes > 0
            finally:
                for key in keys:
                    for version in minio_store.capture_exact_versions(key, 20).targets:
                        minio_store.delete_version(key, version.version_id)

    asyncio.run(check())


@pytest.mark.parametrize("lose_session,io_error", [(False, False), (True, False), (True, True)])
def test_read_authority_boundary(migrated_url: str, lose_session: bool, io_error: bool) -> None:
    async def check() -> None:
        entered, resume = threading.Event(), threading.Event()
        run_id = uuid4()
        conn = await psycopg.AsyncConnection.connect(migrated_url)

        def blocking_read() -> str:
            entered.set()
            assert resume.wait(timeout=10)
            if io_error:
                raise CategorizedError("source retired", category=ErrorCategory.STALE_HANDLE)
            return "evidence"

        async def reader() -> str:
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
                return await await_vmcore_io(conn, blocking_read)

        task = asyncio.create_task(reader())
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            async with await psycopg.AsyncConnection.connect(migrated_url) as probe:
                async with probe.transaction():
                    assert not await try_advisory_xact_lock(probe, LockScope.RUN, run_id)
                if lose_session:
                    await probe.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))
                    await probe.commit()
            resume.set()
            if lose_session:
                with pytest.raises(psycopg.Error):
                    await task
            else:
                assert await task == "evidence"
        finally:
            resume.set()
            await asyncio.gather(task, return_exceptions=True)
            await conn.close()

    asyncio.run(check())


def test_cancel_drains_owned_reader(migrated_url: str) -> None:
    async def check() -> None:
        entered, resume = threading.Event(), threading.Event()
        run_id = uuid4()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:

            def blocking_read() -> None:
                entered.set()
                assert resume.wait(timeout=10)

            async def reader() -> None:
                async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
                    await await_vmcore_io(conn, blocking_read)

            task = asyncio.create_task(reader())
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                task.cancel()
                async with (
                    await psycopg.AsyncConnection.connect(migrated_url) as probe,
                    probe.transaction(),
                ):
                    assert not await try_advisory_xact_lock(probe, LockScope.RUN, run_id)
                resume.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                resume.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


def test_store_failure_keeps_retirement_recoverable(
    migrated_url: str,
    minio_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def check() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            run_id, execution, keys = await seed_pair(pool, minio_store)
            original_delete = minio_store.delete_version

            def fail_delete(key: str, version: str) -> None:
                raise CategorizedError(
                    "injected delete failure", category=ErrorCategory.INFRASTRUCTURE_FAILURE
                )

            try:
                monkeypatch.setattr(minio_store, "delete_version", fail_delete)
                async with pool.connection() as conn:
                    with pytest.raises(CategorizedError):
                        await sweep_vmcore_retention(conn, minio_store, 30)
                    row = await (
                        await conn.execute(
                            "SELECT state,retry_at IS NOT NULL FROM "
                            "vmcore_capture_executions WHERE id=%s",
                            (execution,),
                        )
                    ).fetchone()
                    assert row == ("retiring", True)
                    count = await (
                        await conn.execute(
                            "SELECT count(*) FROM artifacts WHERE owner_id=%s",
                            (run_id,),
                        )
                    ).fetchone()
                    assert count == (2,)
                monkeypatch.setattr(minio_store, "delete_version", original_delete)
                async with pool.connection() as conn, conn.transaction():
                    await conn.execute(
                        "UPDATE vmcore_capture_executions SET retry_at=now() WHERE id=%s",
                        (execution,),
                    )
                async with pool.connection() as conn:
                    await sweep_vmcore_retention(conn, minio_store, 30)
                    row = await (
                        await conn.execute(
                            "SELECT state FROM vmcore_capture_executions WHERE id=%s",
                            (execution,),
                        )
                    ).fetchone()
                    assert row == ("retired",)
            finally:
                monkeypatch.setattr(minio_store, "delete_version", original_delete)
                for key in keys:
                    for version in minio_store.capture_exact_versions(key, 20).targets:
                        minio_store.delete_version(key, version.version_id)

    asyncio.run(check())


@pytest.mark.parametrize("state", ["capturing", "published", "retiring", "retired"])
def test_both_generic_sweep_guards_keep_journal_keys(
    migrated_url: str,
    minio_store: ObjectStore,
    state: str,
) -> None:
    async def check() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            run_id, execution, keys = await seed_pair(pool, minio_store)
            try:
                async with pool.connection() as conn, conn.transaction():
                    await conn.execute("DELETE FROM artifacts WHERE owner_id=%s", (run_id,))
                    await conn.execute(
                        "UPDATE vmcore_capture_executions SET "
                        "state=%s,raw_tombstone_version_id='test',"
                        "redacted_tombstone_version_id='test' WHERE id=%s",
                        (state, execution),
                    )
                async with pool.connection() as conn:
                    candidates = [
                        UploadOrphanCandidate(
                            key=key,
                            owner_kind="runs",
                            owner_id=run_id,
                            last_modified=datetime.now(UTC) - timedelta(days=400),
                        )
                        for key in keys
                    ]
                    assert await reclaimable_upload_keys(conn, candidates, timedelta(hours=1)) == []
                    async with conn.transaction():
                        for key in keys:
                            assert await owner_key_is_fenced(conn, "runs", run_id, key)
            finally:
                for key in keys:
                    for version in minio_store.capture_exact_versions(key, 20).targets:
                        minio_store.delete_version(key, version.version_id)

    asyncio.run(check())


def test_history_drains_without_removing_fence(migrated_url: str, minio_store: ObjectStore) -> None:
    async def check() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            _, execution, keys = await seed_pair(pool, minio_store)
            try:
                for key in keys:
                    tenant, kind, owner_id, name = key.split("/")
                    sensitivity = (
                        Sensitivity.REDACTED
                        if name.endswith("-redacted")
                        else Sensitivity.SENSITIVE
                    )
                    for index in range(3):
                        minio_store.put_artifact(
                            ArtifactWriteRequest(
                                tenant=tenant,
                                owner_kind=kind,
                                owner_id=owner_id,
                                name=name,
                                data=f"legacy-version-{index}".encode(),
                                sensitivity=sensitivity,
                                retention_class="vmcore",
                            )
                        )
                async with pool.connection() as conn, conn.transaction():
                    await conn.execute(
                        "UPDATE vmcore_capture_executions SET legacy=true WHERE id=%s", (execution,)
                    )
                for _ in range(5):
                    async with pool.connection() as conn, conn.transaction():
                        await conn.execute(
                            "UPDATE vmcore_capture_executions SET retry_at=now() WHERE id=%s",
                            (execution,),
                        )
                    async with pool.connection() as conn:
                        await sweep_vmcore_retention(conn, minio_store, 30)
                for key in keys:
                    batch = minio_store.capture_exact_versions(key, 20)
                    assert batch.history_complete and len(batch.targets) == 1
                    head = minio_store.head(key)
                    assert head is not None and head.size_bytes == 0
                    assert head.metadata["vmcore-kind"] == "tombstone"
            finally:
                for key in keys:
                    for version in minio_store.capture_exact_versions(key, 20).targets:
                        minio_store.delete_version(key, version.version_id)

    asyncio.run(check())


@pytest.fixture
def isolated_legacy_store(minio_store: ObjectStore) -> Iterator[ObjectStore]:
    client = minio_store._client
    bucket = f"vmcore-legacy-{uuid4().hex}"
    client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    try:
        yield ObjectStore(client, bucket)
    finally:
        _empty_bucket(client, bucket)
        client.delete_bucket(Bucket=bucket)


@pytest.mark.parametrize(
    "failed_attestation",
    [
        None,
        "all_old_producers_stopped",
        "all_old_store_requests_terminated",
        "old_signers_permanently_revoked",
        "identity_inventory_verified",
        "anonymous_write_denied",
        "new_signer_verified",
        "unknown_key",
        "malformed_pair",
        "external_backend",
    ],
)
def test_legacy_import_preserves_age_and_requires_attestation(
    migrated_url: str,
    isolated_legacy_store: ObjectStore,
    failed_attestation: str | None,
) -> None:
    minio_store = isolated_legacy_store

    async def check() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            run_id, execution, new_keys = await seed_pair(pool, minio_store)
            old_keys = (
                f"local/runs/{run_id}/vmcore-host_dump",
                f"local/runs/{run_id}/vmcore-host_dump-redacted",
            )
            try:
                async with pool.connection() as conn, conn.transaction():
                    await conn.execute(
                        "DELETE FROM vmcore_capture_executions WHERE id=%s", (execution,)
                    )
                    await conn.execute("DELETE FROM vmcore_retention_baseline")
                    for new_key, old_key in zip(new_keys, old_keys, strict=True):
                        sensitivity = (
                            Sensitivity.REDACTED
                            if old_key.endswith("-redacted")
                            else Sensitivity.SENSITIVE
                        )
                        legacy = minio_store.put_artifact(
                            ArtifactWriteRequest(
                                tenant="local",
                                owner_kind="runs",
                                owner_id=str(run_id),
                                name=old_key.rsplit("/", 1)[-1],
                                data=b"legacy-core",
                                sensitivity=sensitivity,
                                retention_class="vmcore",
                            )
                        )
                        await conn.execute(
                            "UPDATE artifacts SET object_key=%s,etag=%s WHERE object_key=%s",
                            (old_key, legacy.etag, new_key),
                        )
                        for version in minio_store.capture_exact_versions(new_key, 20).targets:
                            minio_store.delete_version(new_key, version.version_id)
                receipt = {
                    "mode": "legacy_verified",
                    "backend": "bundled-single-process",
                    # Public pinned source commit.
                    # pragma: allowlist nextline secret
                    "backend_commit": "d997fba1575583a89cf0cc50dc0150642286c86d",
                    "all_old_producers_stopped": True,
                    "all_old_store_requests_terminated": True,
                    "old_signers_permanently_revoked": True,
                    "identity_inventory_verified": True,
                    "anonymous_write_denied": True,
                    "new_signer_verified": True,
                }
                if failed_attestation == "external_backend":
                    receipt["backend"] = "external"
                elif failed_attestation == "unknown_key":
                    minio_store.put_artifact(
                        ArtifactWriteRequest(
                            tenant="local",
                            owner_kind="runs",
                            owner_id=str(run_id),
                            name="vmcore-unknown",
                            data=b"unknown",
                            sensitivity=Sensitivity.SENSITIVE,
                            retention_class="vmcore",
                        )
                    )
                elif failed_attestation is not None and failed_attestation != "malformed_pair":
                    receipt[failed_attestation] = False
                async with pool.connection() as conn:
                    if failed_attestation == "malformed_pair":
                        await conn.execute(
                            "UPDATE artifacts SET sensitivity='sensitive' WHERE object_key=%s",
                            (old_keys[1],),
                        )
                        await conn.commit()
                    if failed_attestation is not None:
                        with pytest.raises(ValueError):
                            await import_vmcore_baseline(conn, minio_store, receipt)
                        return
                    assert await import_vmcore_baseline(conn, minio_store, receipt) == 1
                    assert await import_vmcore_baseline(conn, minio_store, receipt) == 0
                    row = await (
                        await conn.execute(
                            "SELECT e.captured_at=(SELECT max(a.created_at) FROM "
                            "artifacts a WHERE a.owner_id=e.run_id),"
                            "e.legacy FROM vmcore_capture_executions e WHERE e.run_id=%s",
                            (run_id,),
                        )
                    ).fetchone()
                    assert row == (True, True)
            finally:
                for key in (*new_keys, *old_keys):
                    for version in minio_store.capture_exact_versions(key, 20).targets:
                        minio_store.delete_version(key, version.version_id)

    asyncio.run(check())


def test_partial_history_requires_another_pass(
    migrated_url: str,
    minio_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def check() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            _, execution, keys = await seed_pair(pool, minio_store)
            original_list = minio_store.list_version_page
            original_delete = minio_store.delete_version
            first_page: dict[str, str] = {}
            calls: list[str] = []
            deleted: list[tuple[str, str]] = []
            all_deleted: list[tuple[str, str]] = []
            initial: dict[str, set[str]] = {}
            fences: dict[str, str] = {}

            def inventory(key: str, *, max_keys: int = 1000) -> VersionPage:
                calls.append(key)
                assert max_keys in {999, 1000}
                page = original_list(key, max_keys=max_keys)
                if key not in first_page:
                    # Real nonlatest data identities; the fence and more data are omitted
                    # into a later page. Only pagination is injected, never deletion logic.
                    older = tuple(v for v in page.entries if v.key == key and not v.is_latest)
                    assert len(older) >= 2
                    entry = older[0]
                    first_page[key] = entry.version_id
                    return VersionPage((entry,), True, key, entry.version_id)
                return page

            def delete_exact(key: str, version_id: str) -> None:
                deleted.append((key, version_id))
                all_deleted.append((key, version_id))
                assert sum(k == key for k, _ in deleted) <= 1000
                original_delete(key, version_id)

            try:
                for key in keys:
                    tenant, kind, owner_id, name = key.split("/")
                    sensitivity = (
                        Sensitivity.REDACTED
                        if name.endswith("-redacted")
                        else Sensitivity.SENSITIVE
                    )
                    for index in range(3):
                        minio_store.put_artifact(
                            ArtifactWriteRequest(
                                tenant=tenant,
                                owner_kind=kind,
                                owner_id=owner_id,
                                name=name,
                                data=f"legacy-history-{index}".encode(),
                                sensitivity=sensitivity,
                                retention_class="vmcore",
                            )
                        )
                    page = original_list(key, max_keys=1000)
                    assert not page.is_truncated
                    initial[key] = {v.version_id for v in page.entries if v.key == key}
                    # Seed an already-fenced legacy history at the store boundary. Deleting
                    # the latest data alone would reveal an older current version and make
                    # conditional creation conflict before the pagination arm can run.
                    upload = minio_store.presign_put(
                        PresignPutRequest(
                            key=key,
                            size_bytes=0,
                            # SHA256 of the empty synthetic body.
                            # pragma: allowlist nextline secret
                            sha256="47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=",
                            sensitivity=sensitivity,
                            retention_class="vmcore",
                            expires_in=60,
                            metadata={
                                "vmcore-kind": "tombstone",
                                "vmcore-execution": str(execution),
                            },
                        )
                    )
                    # Historical fixture seeding uses a one-request connection; production
                    # destination arbitration still uses its real conditional store primitive.
                    with urlopen(
                        Request(
                            upload.url, data=b"", method="PUT", headers=upload.required_headers
                        ),
                        timeout=10,
                    ) as response:
                        assert response.status == 200
                        fences[key] = response.headers["x-amz-version-id"]

                async with pool.connection() as conn, conn.transaction():
                    await conn.execute(
                        "UPDATE vmcore_capture_executions SET legacy=true WHERE id=%s",
                        (execution,),
                    )
                monkeypatch.setattr(minio_store, "list_version_page", inventory)
                monkeypatch.setattr(minio_store, "delete_version", delete_exact)
                # Actual ObjectStore.delete_batch deletes this nonempty incomplete batch,
                # returns False, and leaves the journal pending for a later bounded pass.
                async with pool.connection() as conn:
                    assert await sweep_vmcore_retention(conn, minio_store, 30) == 0
                    row = await (
                        await conn.execute(
                            "SELECT state FROM vmcore_capture_executions WHERE id=%s",
                            (execution,),
                        )
                    ).fetchone()
                    assert row == ("retiring",)
                    for key in keys:
                        assert calls.count(key) == 1
                        assert [v for k, v in deleted if k == key] == [
                            first_page[key],
                        ]
                        head = minio_store.head(key)
                        assert head is not None and head.size_bytes == 0
                        assert head.metadata["vmcore-kind"] == "tombstone"
                        assert fences[key] == head.version_id
                        remaining = original_list(key, max_keys=1000)
                        expected = initial[key] - {first_page[key]}
                        assert expected
                        assert {
                            v.version_id for v in remaining.entries if v.key == key
                        } == expected | {fences[key]}
                    calls.clear()
                    deleted.clear()
                    await conn.commit()
                    await conn.execute(
                        "UPDATE vmcore_capture_executions SET retry_at=now() WHERE id=%s",
                        (execution,),
                    )
                    await conn.commit()
                    assert await sweep_vmcore_retention(conn, minio_store, 30) == 1
                    row = await (
                        await conn.execute(
                            "SELECT state FROM vmcore_capture_executions WHERE id=%s",
                            (execution,),
                        )
                    ).fetchone()
                    assert row == ("retired",)
                for key in keys:
                    assert calls.count(key) == 2
                    expected = initial[key] - {first_page[key]}
                    actual = [v for k, v in deleted if k == key]
                    assert set(actual) == expected and len(actual) == len(expected)
                    assert len(actual) <= 1000
                    assert [v for k, v in all_deleted if k == key].count(fences[key]) == 0
                    assert {v for k, v in all_deleted if k == key} == initial[key]
                    remaining = original_list(key, max_keys=1000)
                    assert not remaining.is_truncated
                    assert [v.version_id for v in remaining.entries if v.key == key] == [
                        fences[key]
                    ]
            finally:
                monkeypatch.setattr(minio_store, "list_version_page", original_list)
                monkeypatch.setattr(minio_store, "delete_version", original_delete)
                for key in keys:
                    for version in minio_store.capture_exact_versions(key, 20).targets:
                        original_delete(key, version.version_id)

    asyncio.run(check())
