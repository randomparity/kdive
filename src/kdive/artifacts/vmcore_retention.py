"""Run-owned vmcore publication and read protection (ADR-0786)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row

import kdive.config as config
from kdive.artifacts.storage import (
    ConditionalCreateConflict,
    ConditionalCreateResult,
    StoredArtifact,
    artifact_key,
)
from kdive.config.core_settings import VMCORE_RETENTION_DAYS
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.serialization import JsonValue


def vmcore_keys(
    tenant: str, run_id: UUID, method: CaptureMethod, execution_id: UUID
) -> tuple[str, str]:
    if method not in {CaptureMethod.KDUMP, CaptureMethod.FADUMP, CaptureMethod.HOST_DUMP}:
        raise CategorizedError(
            "capture method does not produce a vmcore",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    name = f"vmcore-{method.value}-{execution_id}"
    return (
        artifact_key(tenant, "runs", str(run_id), name),
        artifact_key(tenant, "runs", str(run_id), name + "-redacted"),
    )


def capture_metadata(execution_id: UUID) -> dict[str, str]:
    return {"vmcore-execution": str(execution_id), "vmcore-kind": "capture"}


def require_capture_write(result: ConditionalCreateResult) -> StoredArtifact:
    if isinstance(result, ConditionalCreateConflict):
        raise CategorizedError(
            "vmcore capture destination is already occupied or retired",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"reason": "vmcore_destination_conflict"},
        )
    return result


@dataclass(frozen=True, slots=True)
class VmcoreExecution:
    id: UUID
    run_id: UUID
    job_id: UUID | None
    job_attempt: int | None
    method: CaptureMethod
    raw_key: str
    redacted_key: str
    state: str
    legacy: bool
    raw_version_id: str | None
    redacted_version_id: str | None


async def published_vmcore(conn: AsyncConnection, run_id: UUID) -> VmcoreExecution | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, run_id, job_id, job_attempt, method, raw_key, redacted_key, state, "
            "legacy, raw_version_id, redacted_version_id FROM vmcore_capture_executions "
            "WHERE run_id = %s AND state = 'published'",
            (run_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    row["method"] = CaptureMethod(row["method"])
    return VmcoreExecution(**row)


async def require_vmcore_available(conn: AsyncConnection, run_id: UUID) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT vmcore_retired_at FROM runs WHERE id = %s", (run_id,))
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError("Run not found", category=ErrorCategory.NOT_FOUND)
    if row[0] is not None:
        raise CategorizedError(
            "vmcore evidence has retired",
            category=ErrorCategory.NOT_FOUND,
            details={"reason": "vmcore_retired"},
        )


async def require_current_capture(conn: AsyncConnection, job_id: UUID, attempt: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT attempt, state, lease_expires_at > clock_timestamp() FROM jobs WHERE id = %s",
            (job_id,),
        )
        row = await cur.fetchone()
    if row is None or row != (attempt, "running", True):
        raise CategorizedError(
            "vmcore capture attempt no longer owns publication",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"reason": "vmcore_attempt_expired"},
        )


async def journal_vmcore(
    conn: AsyncConnection,
    *,
    run_id: UUID,
    job_id: UUID,
    attempt: int,
    method: CaptureMethod,
    tenant: str,
) -> VmcoreExecution:
    await require_vmcore_available(conn, run_id)
    await require_current_capture(conn, job_id, attempt)
    execution_id = uuid4()
    raw_key, redacted_key = vmcore_keys(tenant, run_id, method, execution_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO vmcore_capture_executions "
            "(id,run_id,job_id,job_attempt,method,raw_key,redacted_key) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (execution_id, run_id, job_id, attempt, method.value, raw_key, redacted_key),
        )
    return VmcoreExecution(
        execution_id,
        run_id,
        job_id,
        attempt,
        method,
        raw_key,
        redacted_key,
        "capturing",
        False,
        None,
        None,
    )


async def publish_vmcore(
    conn: AsyncConnection,
    execution: VmcoreExecution,
    raw: StoredArtifact,
    redacted: StoredArtifact,
) -> None:
    await require_vmcore_available(conn, execution.run_id)
    if execution.job_id is None or execution.job_attempt is None:
        raise RuntimeError("a legacy execution cannot publish from a worker")
    await require_current_capture(conn, execution.job_id, execution.job_attempt)
    if raw.key != execution.raw_key or redacted.key != execution.redacted_key:
        raise CategorizedError(
            "provider returned a vmcore outside its journaled destinations",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE vmcore_capture_executions SET state='published', published_at=now(), "
            "captured_at=(SELECT max(created_at) FROM artifacts WHERE object_key IN (%s,%s)), "
            "raw_version_id=%s, redacted_version_id=%s "
            "WHERE id=%s AND state='capturing' RETURNING id",
            (raw.key, redacted.key, raw.version_id, redacted.version_id, execution.id),
        )
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError(
            "vmcore execution no longer accepts publication",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )


def signed_get_deadline(url: str) -> datetime:
    try:
        query = parse_qs(urlsplit(url).query, strict_parsing=True)
        dates = query["X-Amz-Date"]
        expiries = query["X-Amz-Expires"]
        if len(dates) != 1 or len(expiries) != 1:
            raise ValueError("duplicate signing time")
        signed_at = datetime.strptime(dates[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        seconds = int(expiries[0])
        if seconds <= 0:
            raise ValueError("nonpositive expiration")
        return signed_at + timedelta(seconds=seconds)
    except (KeyError, ValueError, OverflowError) as exc:
        raise CategorizedError(
            "vmcore download signing deadline could not be established",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc


async def protect_vmcore_url(
    conn: AsyncConnection, run_id: UUID, url: str
) -> tuple[datetime, datetime]:
    expires_at = signed_get_deadline(url)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE runs SET vmcore_download_protected_until = "
            "GREATEST(vmcore_download_protected_until, %s) "
            "WHERE id=%s AND vmcore_retired_at IS NULL AND %s>clock_timestamp() "
            "RETURNING clock_timestamp()",
            (expires_at, run_id, expires_at),
        )
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError(
            "vmcore download protection could not be committed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    return row[0], expires_at


async def await_vmcore_io[T](conn: AsyncConnection, operation: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise
    except Exception:
        await conn.execute("SELECT 1")
        raise
    await conn.execute("SELECT 1")
    return result


async def vmcore_retention_view(conn: AsyncConnection, run_id: UUID) -> dict[str, JsonValue]:
    days = config.get(VMCORE_RETENTION_DAYS)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT clock_timestamp(), r.state, i.state, r.vmcore_retired_at, "
            "r.vmcore_download_protected_until, e.captured_at, "
            "e.captured_at + %s * interval '1 day' "
            "FROM runs r JOIN investigations i ON i.id=r.investigation_id "
            "LEFT JOIN vmcore_capture_executions e ON e.run_id=r.id AND e.state='published' "
            "WHERE r.id=%s",
            (days, run_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError("Run not found", category=ErrorCategory.NOT_FOUND)
    now, run_state, inv_state, retired, protected, captured, eligibility = row
    reason = None
    if retired is not None:
        reason = "vmcore_retired"
    elif days is None:
        reason = "policy_disabled"
    elif inv_state not in {"closed", "abandoned"}:
        reason = "investigation_open"
    elif run_state not in {"succeeded", "failed", "canceled"}:
        reason = "run_nonterminal"
    elif captured is None:
        reason = "no_vmcore"
    return {
        "server_time": now.isoformat(),
        "policy_enabled": days is not None,
        "eligible_at": eligibility.isoformat() if reason is None else None,
        "protected_until": protected.isoformat() if protected is not None else None,
        "retired_at": retired.isoformat() if retired is not None else None,
        "protection_reason": reason,
    }
