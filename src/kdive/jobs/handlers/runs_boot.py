"""Worker boot handler and console artifact capture for the `runs.*` plane."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.db.idempotency import claim_run_step, complete_run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import Run
from kdive.domain.operations.jobs import Job
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.runs_common import abandon_run_step_best_effort
from kdive.jobs.payloads import RunPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports import Booter
from kdive.providers.shared.runtime_paths import console_log_path, read_console_log
from kdive.security import audit
from kdive.security.artifacts.artifact_search import ArtifactSearchInputError, search_text
from kdive.security.authz.context import RequestContext
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import (
    ObjectStore,
    register_artifact_row,
)

_log = logging.getLogger(__name__)

_CONSOLE_ROW_SQL: LiteralString = (
    "SELECT id, etag FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND object_key LIKE %s"
)

_REFRESH_CONSOLE_ETAG_SQL: LiteralString = "UPDATE artifacts SET etag = %s WHERE id = %s"


class _ConsoleRow(NamedTuple):
    id: UUID
    etag: str


class _ConsoleArtifact(NamedTuple):
    id: UUID
    object_key: str
    data: bytes


async def _existing_console_row(conn: AsyncConnection, system_id: UUID) -> _ConsoleRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_CONSOLE_ROW_SQL, (system_id, "%/console"))
        row = await cur.fetchone()
    return None if row is None else _ConsoleRow(row["id"], str(row["etag"]))


async def _capture_console_artifact(
    conn: AsyncConnection,
    system_id: UUID,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
) -> _ConsoleArtifact | None:
    try:
        if artifact_store is None:
            return None
        redacted = await _read_redacted_console(system_id, secret_registry)
        if redacted is None:
            return None
        stored = await _store_console_artifact(artifact_store, system_id, redacted)
        return await _upsert_console_artifact_row(conn, system_id, stored, redacted)
    except CategorizedError as exc:
        if exc.details.get("operation") == "read_console_log":
            raise
        _log.warning(
            "console artifact registration failed for system %s; boot outcome unaffected",
            system_id,
            exc_info=True,
        )
        return None
    except Exception:
        _log.warning(
            "console artifact registration failed for system %s; boot outcome unaffected",
            system_id,
            exc_info=True,
        )
        return None


async def _read_redacted_console(system_id: UUID, secret_registry: SecretRegistry) -> bytes | None:
    raw = await asyncio.to_thread(read_console_log, console_log_path(system_id))
    if not raw:
        _log.warning(
            "console log for system %s is empty or unreadable; registering no console artifact",
            system_id,
        )
        return None
    return (
        Redactor(registry=secret_registry)
        .redact_text(raw.decode("utf-8", "replace"))
        .encode("utf-8")
    )


async def _store_console_artifact(
    artifact_store: ObjectStore, system_id: UUID, redacted: bytes
) -> StoredArtifact:
    def _put() -> StoredArtifact:
        return artifact_store.put_artifact(
            ArtifactWriteRequest(
                tenant="local",
                owner_kind="systems",
                owner_id=str(system_id),
                name="console",
                data=redacted,
                sensitivity=Sensitivity.REDACTED,
                retention_class="console",
            )
        )

    return await asyncio.to_thread(_put)


async def _upsert_console_artifact_row(
    conn: AsyncConnection,
    system_id: UUID,
    stored: StoredArtifact,
    redacted: bytes,
) -> _ConsoleArtifact:
    async with conn.transaction():
        existing = await _existing_console_row(conn, system_id)
        if existing is None:
            inserted = await ARTIFACTS.insert(
                conn, register_artifact_row(stored, owner_kind="systems", owner_id=system_id)
            )
            return _ConsoleArtifact(inserted.id, inserted.object_key, redacted)
        if existing.etag != stored.etag:
            await conn.execute(_REFRESH_CONSOLE_ETAG_SQL, (stored.etag, existing.id))
        return _ConsoleArtifact(existing.id, stored.key, redacted)


def _expected_crash_matches(run: Run, redacted_console: bytes) -> bool:
    expected = run.expected_boot_failure
    if expected is None or expected.get("kind") != "console_crash":
        return False
    pattern = expected.get("pattern")
    if not isinstance(pattern, str):
        return False
    try:
        return (
            search_text(
                redacted_console,
                pattern=pattern,
                before_lines=0,
                after_lines=0,
                max_matches=1,
            ).match_count
            > 0
        )
    except ArtifactSearchInputError:
        return False


async def _record_boot_audit(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
) -> None:
    await audit.record(
        conn,
        job_ctx,
        audit.AuditEvent(
            tool="runs.boot",
            object_kind="runs",
            object_id=run.id,
            transition="boot",
            args={"run_id": str(run.id)},
            project=run.project,
        ),
    )


async def _run_boot_and_capture_outcome(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
    booter: Booter,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
) -> dict[str, Any]:
    system_id = run.require_system_id()
    try:
        await asyncio.to_thread(booter.boot, system_id)
    except CategorizedError as exc:
        artifact = None
        if (
            exc.category is ErrorCategory.READINESS_FAILURE
            and run.expected_boot_failure is not None
        ):
            artifact = await _capture_console_artifact(
                conn, system_id, secret_registry, artifact_store
            )
        if artifact is not None and artifact.data and _expected_crash_matches(run, artifact.data):
            await _record_boot_audit(conn, job_ctx, run)
            return {
                "system_id": str(system_id),
                "boot_outcome": "expected_crash_observed",
                "expectation_matched": True,
                "evidence_kind": "console",
                "evidence_artifact_id": str(artifact.id),
            }
        raise
    artifact = await _capture_console_artifact(conn, system_id, secret_registry, artifact_store)
    await _record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(system_id),
        "boot_outcome": "ready",
        **({"evidence_artifact_id": str(artifact.id)} if artifact else {}),
    }


async def boot_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None = None,
) -> str | None:
    """Boot the installed kernel and confirm run-readiness, recording the `boot` step."""
    run_id = UUID(load_payload(job, RunPayload).run_id)
    run = await RUNS.get(conn, run_id)
    if run is None:
        raise CategorizedError(
            "boot target run is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id)},
        )
    job_ctx = job_context_from_job(job, run.project)
    claim = await claim_run_step(conn, run_id, "boot")
    if not claim.claimed:
        return str(run_id)
    binding = await resolver.binding_for_run(conn, run_id)
    set_provider_kind(binding.kind.value)
    booter = binding.runtime.booter
    system_id = run.require_system_id()

    try:
        result = await _run_boot_and_capture_outcome(
            conn, job_ctx, run, booter, secret_registry, artifact_store
        )
    except CategorizedError:
        await abandon_run_step_best_effort(conn, run_id, "boot")
        try:
            await _capture_console_artifact(conn, system_id, secret_registry, artifact_store)
        finally:
            raise
    except Exception:
        await abandon_run_step_best_effort(conn, run_id, "boot")
        raise
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
        advisory_xact_lock(conn, LockScope.RUN, run_id),
    ):
        await complete_run_step(conn, run_id, "boot", result)
    return str(run_id)
