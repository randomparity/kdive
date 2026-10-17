"""Bounded retirement of journaled vmcore destinations (ADR-0786)."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.storage import (
    ConditionalArtifactWriteRequest,
    ConditionalCreateConflict,
    ConditionalCreateResult,
    HeadResult,
    VersionBatch,
    VersionPage,
)
from kdive.artifacts.vmcore_retention import VmcoreExecution
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)


class VmcoreRetentionStore(Protocol):
    def head(self, key: str, *, version_id: str | None = None) -> HeadResult | None: ...
    def create_if_absent(
        self, request: ConditionalArtifactWriteRequest
    ) -> ConditionalCreateResult: ...
    def list_version_page(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
        max_keys: int = 1000,
    ) -> VersionPage: ...
    def delete_version(self, key: str, version_id: str) -> None: ...
    def delete_batch(self, batch: VersionBatch) -> bool: ...


async def fence_vmcore_key(
    store: VmcoreRetentionStore,
    *,
    execution_id: UUID,
    key: str,
    sensitivity: Sensitivity,
    legacy: bool,
) -> str | None:
    head = await asyncio.to_thread(store.head, key)
    if head is not None:
        kind = head.metadata.get("vmcore-kind")
        same_execution = head.metadata.get("vmcore-execution") == str(execution_id)
        if kind == "tombstone" and same_execution and head.size_bytes == 0:
            fence_version = head.version_id
        else:
            valid_capture = kind == "capture" and same_execution
            valid_legacy = legacy and kind is None and head.sensitivity == sensitivity
            if not (valid_capture or valid_legacy):
                raise CategorizedError(
                    "vmcore retention found conflicting object identity",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"reason": "vmcore_identity_conflict"},
                )
            await asyncio.to_thread(store.delete_version, key, head.version_id)
            fence_version = None
    else:
        fence_version = None
    if fence_version is None:
        result = await asyncio.to_thread(
            store.create_if_absent,
            ConditionalArtifactWriteRequest(
                key=key,
                data=b"",
                metadata={
                    "vmcore-execution": str(execution_id),
                    "vmcore-kind": "tombstone",
                },
                sensitivity=sensitivity,
                retention_class="vmcore",
            ),
        )
        if isinstance(result, ConditionalCreateConflict):
            return None
        fence_version = result.version_id
    observed = await asyncio.to_thread(store.head, key, version_id=fence_version)
    if (
        observed is None
        or observed.size_bytes != 0
        or observed.metadata.get("vmcore-kind") != "tombstone"
        or observed.metadata.get("vmcore-execution") != str(execution_id)
    ):
        raise CategorizedError(
            "vmcore retention could not verify its destination fence",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    page = await asyncio.to_thread(store.list_version_page, key, max_keys=999)
    complete = not (page.is_truncated and page.next_key_marker == key)
    targets = tuple(v for v in page.entries if v.key == key and v.version_id != fence_version)
    if targets:
        deleted = await asyncio.to_thread(
            store.delete_batch,
            VersionBatch(key=key, targets=targets, history_complete=complete),
        )
        if not deleted:
            return None
    if not complete:
        return None
    remaining = await asyncio.to_thread(store.list_version_page, key, max_keys=1000)
    exact = tuple(v for v in remaining.entries if v.key == key)
    if (remaining.is_truncated and remaining.next_key_marker == key) or any(
        v.version_id != fence_version for v in exact
    ):
        return None
    if not any(v.version_id == fence_version for v in exact):
        raise CategorizedError(
            "vmcore destination fence disappeared during verification",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    return fence_version


async def retire_run_pair(conn: AsyncConnection, run_id: UUID, days: int) -> bool:
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.RUN, run_id),
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT r.id FROM runs r JOIN investigations i ON i.id=r.investigation_id "
            "JOIN vmcore_capture_executions e ON e.run_id=r.id AND e.state='published' "
            "WHERE r.id=%s AND r.vmcore_retired_at IS NULL "
            "AND r.state IN ('succeeded','failed','canceled') "
            "AND i.state IN ('closed','abandoned') "
            "AND e.captured_at + %s * interval '1 day' <= now() "
            "AND (r.vmcore_download_protected_until IS NULL "
            "     OR r.vmcore_download_protected_until <= now()) "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='capture_vmcore' "
            "    AND j.payload->>'run_id'=r.id::text AND j.state IN ('queued','running')) "
            "AND NOT EXISTS (SELECT 1 FROM object_write_leases l JOIN jobs j "
            "    ON j.id=l.job_id WHERE l.owner_kind='runs' AND l.owner_id=r.id "
            "    AND j.state='running' AND j.lease_expires_at>now())",
            (run_id, days),
        )
        if await cur.fetchone() is None:
            return False
        await cur.execute("UPDATE runs SET vmcore_retired_at=now() WHERE id=%s", (run_id,))
        await cur.execute(
            "UPDATE vmcore_capture_executions SET state='retiring', retry_at=now() "
            "WHERE run_id=%s AND state IN ('capturing','published')",
            (run_id,),
        )
    return True


async def drain_execution(
    conn: AsyncConnection, store: VmcoreRetentionStore, execution: VmcoreExecution
) -> bool:
    if execution.state != "retiring":
        raise RuntimeError("only retiring vmcore executions may be drained")
    raw_fence = await fence_vmcore_key(
        store,
        execution_id=execution.id,
        key=execution.raw_key,
        sensitivity=Sensitivity.SENSITIVE,
        legacy=execution.legacy,
    )
    redacted_fence = await fence_vmcore_key(
        store,
        execution_id=execution.id,
        key=execution.redacted_key,
        sensitivity=Sensitivity.REDACTED,
        legacy=execution.legacy,
    )
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.RUN, execution.run_id),
        conn.cursor() as cur,
    ):
        if raw_fence is None or redacted_fence is None:
            await cur.execute(
                "UPDATE vmcore_capture_executions SET retry_at=now()+interval '30 seconds' "
                "WHERE id=%s AND state='retiring'",
                (execution.id,),
            )
            return False
        await cur.execute(
            "UPDATE vmcore_capture_executions SET state='retired', "
            "raw_tombstone_version_id=%s, redacted_tombstone_version_id=%s, retry_at=NULL "
            "WHERE id=%s AND state='retiring' RETURNING id",
            (raw_fence, redacted_fence, execution.id),
        )
        if await cur.fetchone() is None:
            return False
        await cur.execute(
            "DELETE FROM artifacts WHERE owner_kind='runs' AND owner_id=%s "
            "AND retention_class='vmcore' AND object_key IN (%s,%s)",
            (execution.run_id, execution.raw_key, execution.redacted_key),
        )
    return True


async def sweep_vmcore_retention(
    conn: AsyncConnection, store: VmcoreRetentionStore, days: int | None
) -> int:
    if days is None:
        return 0
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM vmcore_retention_baseline WHERE singleton")
        if await cur.fetchone() is None:
            raise CategorizedError(
                "vmcore retention requires the verified offline rollout baseline",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        await cur.execute(
            "SELECT e.id,e.run_id FROM vmcore_capture_executions e "
            "JOIN runs r ON r.id=e.run_id JOIN investigations i ON i.id=r.investigation_id "
            "WHERE (e.retry_at IS NULL OR e.retry_at<=now()) AND (e.state='retiring' OR "
            "(e.state='capturing' AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id=e.job_id "
            "AND j.attempt=e.job_attempt AND j.state='running' AND j.lease_expires_at>now())) OR "
            "(e.state='published' AND r.state IN ('succeeded','failed','canceled') "
            "AND i.state IN ('closed','abandoned') "
            "AND e.captured_at + %s * interval '1 day'<=now() "
            "AND (r.vmcore_download_protected_until IS NULL "
            "OR r.vmcore_download_protected_until<=now()))) "
            "ORDER BY e.retry_at NULLS FIRST,e.created_at,e.id LIMIT 32",
            (days,),
        )
        candidates = await cur.fetchall()
    completed = 0
    failures = 0
    for execution_id, run_id in candidates:
        try:
            async with (
                conn.transaction(),
                advisory_xact_lock(conn, LockScope.RUN, run_id),
                conn.cursor() as cur,
            ):
                await cur.execute(
                    "UPDATE vmcore_capture_executions e SET state='retiring' "
                    "WHERE e.id=%s AND e.state='capturing' AND NOT EXISTS ("
                    "SELECT 1 FROM jobs j WHERE j.id=e.job_id AND j.attempt=e.job_attempt "
                    "AND j.state='running' AND j.lease_expires_at>now())",
                    (execution_id,),
                )
            await retire_run_pair(conn, run_id, days)
            async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT id,run_id,job_id,job_attempt,method,raw_key,redacted_key,state,legacy,"
                    "raw_version_id,redacted_version_id FROM vmcore_capture_executions "
                    "WHERE id=%s",
                    (execution_id,),
                )
                row = await cur.fetchone()
            if row is not None and row["state"] == "retiring":
                row["method"] = CaptureMethod(row["method"])
                execution = VmcoreExecution(**row)
                completed += int(await drain_execution(conn, store, execution))
            else:
                async with conn.transaction(), conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE vmcore_capture_executions SET retry_at=now()+interval '30 seconds' "
                        "WHERE id=%s AND state <> 'retired'",
                        (execution_id,),
                    )
        except CategorizedError:
            failures += 1
            _log.exception("vmcore retirement failed for execution %s", execution_id)
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "UPDATE vmcore_capture_executions SET retry_at=now()+interval '30 seconds' "
                    "WHERE id=%s AND state <> 'retired'",
                    (execution_id,),
                )
    if failures:
        raise CategorizedError(
            f"vmcore retirement completed {completed} executions and failed {failures}; "
            f"retry pending",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    return completed
