"""External-build finalization service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import kdive.config as config
from kdive.artifacts import upload_manifest
from kdive.artifacts.reassembly import reassemble_chunked
from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import HeadResult, StoredArtifact, chunk_key
from kdive.artifacts.uploads import ManifestEntry
from kdive.build_artifacts.results import BuildOutput, ValidatedUpload
from kdive.build_artifacts.validation import validate_external_artifacts
from kdive.config.core_settings import UPLOAD_TTL_SECONDS
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS
from kdive.domain.capacity.state import RunState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import Run
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.serialization import JsonValue
from kdive.services.runs.steps import BuildStepResult
from kdive.services.runs.steps import existing_build_result as _existing_build_result
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)


class ExternalBuildStore(Protocol):
    """Object-store surface the external-build finalize path needs."""

    def head(self, key: str) -> HeadResult | None: ...
    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str: ...
    def upload_part_copy(
        self, key: str, upload_id: str, *, part_number: int, source_key: str
    ) -> str: ...
    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> str: ...
    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


type CompleteBuildValidation = Callable[
    [Sequence[ManifestEntry], Mapping[str, str], str | None],
    ValidatedUpload,
]
type ObjectStoreFactory = Callable[[], ExternalBuildStore]


@dataclass(frozen=True, slots=True)
class CompleteBuildConfigurationError(Exception):
    """Caller-correctable configuration rejection data for the MCP envelope."""

    data: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class CompleteBuildValidationError(Exception):
    """External upload validation rejected the artifact set."""

    error: CategorizedError


@dataclass(frozen=True, slots=True)
class _CompleteBuildAlreadyRecorded(Exception):
    result: BuildStepResult


@dataclass(frozen=True, slots=True)
class CompleteBuildFinalizer:
    """Finalize validated external-build uploads for a Run."""

    validate_complete_build: CompleteBuildValidation | None = None
    object_store_factory: ObjectStoreFactory = object_store_from_env

    async def complete(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        run: Run,
        *,
        build_id: str | None,
        cmdline: str | None,
        source_provenance: dict[str, str | bool | list[str]] | None = None,
    ) -> BuildStepResult:
        """Validate uploads and finalize an external Run from ``created`` to ``succeeded``."""
        try:
            prepared = await self._prepare(conn, run)
            validated = await self._validate_uploads(
                conn, run.id, str(run.id), prepared, build_id=build_id
            )
            return await _finalize_external_build(
                conn, ctx, validated, cmdline=cmdline, source_provenance=source_provenance
            )
        except _CompleteBuildAlreadyRecorded as exc:
            return exc.result

    async def _prepare(
        self,
        conn: AsyncConnection,
        run: Run,
    ) -> _ExternalBuildCompletion:
        _require_created_run(run)

        manifest_row = await upload_manifest.get_manifest(conn, "runs", run.id)
        if manifest_row is None:
            raise CompleteBuildConfigurationError({"reason": "no_upload_manifest"})
        has_chunks = any(entry.chunks is not None for entry in manifest_row.entries)
        keys = {entry.name: f"{manifest_row.prefix}{entry.name}" for entry in manifest_row.entries}
        store = self.object_store_factory() if has_chunks else None
        return _ExternalBuildCompletion(
            run=run,
            manifest_row=manifest_row,
            keys=keys,
            has_chunks=has_chunks,
            store=store,
        )

    async def _validate_uploads(
        self,
        conn: AsyncConnection,
        uid: UUID,
        run_id: str,
        prepared: _ExternalBuildCompletion,
        *,
        build_id: str | None,
    ) -> _ExternalBuildFinalization:
        if prepared.store is not None:
            await _reassemble_chunked_artifacts(
                conn, uid, run_id, prepared.manifest_row, prepared.store
            )

        try:
            validated = await asyncio.to_thread(
                self._validate_complete_build,
                list(prepared.manifest_row.entries),
                prepared.keys,
                build_id,
            )
        except CategorizedError as exc:
            raise CompleteBuildValidationError(exc) from exc

        return _ExternalBuildFinalization(
            prepared.run,
            output=validated.output,
            keys=prepared.keys,
            heads=validated.heads,
            store=prepared.store,
            entries=prepared.manifest_row.entries,
            prefix=prepared.manifest_row.prefix,
            chunked=prepared.has_chunks,
        )

    def _validate_complete_build(
        self,
        manifest: Sequence[ManifestEntry],
        keys: Mapping[str, str],
        declared_build_id: str | None,
    ) -> ValidatedUpload:
        if self.validate_complete_build is not None:
            return self.validate_complete_build(manifest, keys, declared_build_id)
        return validate_external_artifacts(
            self.object_store_factory(),
            manifest=manifest,
            keys=keys,
            declared_build_id=declared_build_id,
        )


@dataclass(frozen=True, slots=True)
class _ExternalBuildCompletion:
    run: Run
    manifest_row: upload_manifest.UploadManifest
    keys: dict[str, str]
    has_chunks: bool
    store: ExternalBuildStore | None


@dataclass(frozen=True, slots=True)
class _ExternalBuildFinalization:
    run: Run
    output: BuildOutput
    keys: dict[str, str]
    heads: dict[str, HeadResult]
    store: ExternalBuildStore | None
    entries: Sequence[ManifestEntry]
    prefix: str
    chunked: bool


async def _reassemble_chunked_artifacts(
    conn: AsyncConnection,
    uid: UUID,
    run_id: str,
    manifest_row: upload_manifest.UploadManifest,
    store: ExternalBuildStore,
) -> None:
    ttl = timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, uid):
        refreshed = await upload_manifest.refresh_deadline(conn, "runs", uid, ttl)
    if refreshed:
        try:
            await _reassemble_artifacts(manifest_row, store)
        except CategorizedError as exc:
            recorded = await _existing_build_result(conn, uid)
            if recorded is not None:
                raise _CompleteBuildAlreadyRecorded(recorded) from exc
            raise
        return
    if await upload_manifest.get_manifest(conn, "runs", uid) is None:
        raise CompleteBuildConfigurationError({"reason": "no_upload_manifest"})
    raise CompleteBuildConfigurationError({"reason": "upload_window_expired"})


async def _reassemble_artifacts(
    manifest_row: upload_manifest.UploadManifest,
    store: ExternalBuildStore,
) -> None:
    for entry in manifest_row.entries:
        if entry.chunks is not None:
            await asyncio.to_thread(
                reassemble_chunked,
                store,
                prefix=manifest_row.prefix,
                final_key=f"{manifest_row.prefix}{entry.name}",
                entry=entry,
            )


def _require_created_run(run: Run) -> None:
    if run.state is not RunState.CREATED:
        raise CompleteBuildConfigurationError({"current_status": run.state.value})


async def _finalize_external_build(
    conn: AsyncConnection,
    ctx: RequestContext,
    finalization: _ExternalBuildFinalization,
    *,
    cmdline: str | None,
    source_provenance: dict[str, str | bool | list[str]] | None = None,
) -> BuildStepResult:
    result = BuildStepResult(
        kernel_ref=finalization.output.kernel_ref,
        debuginfo_ref=finalization.output.debuginfo_ref,
        initrd_ref=finalization.keys.get("initrd"),
        build_id=finalization.output.build_id,
        cmdline=cmdline,
        build_provenance=source_provenance,
    )
    run = finalization.run
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None:
            raise CompleteBuildConfigurationError({})
        state = RunState(row["state"])
        if state is RunState.SUCCEEDED:
            return await _existing_build_result(conn, run.id) or result
        if state is not RunState.CREATED:
            raise CompleteBuildConfigurationError({"current_status": state.value})
        await _insert_artifact_rows(conn, run.id, finalization)
        await _record_build_step(conn, run.id, result)
        await _mark_run_succeeded(conn, run.id, finalization.output)
        await _record_complete_build_audit(conn, ctx, run)
        if not finalization.chunked:
            await upload_manifest.delete_manifest(conn, "runs", run.id)
    if finalization.chunked and finalization.store is not None:
        await _cleanup_chunks_and_manifest(
            conn,
            finalization.store,
            run.id,
            finalization.entries,
            finalization.prefix,
        )
    return result


async def _insert_artifact_rows(
    conn: AsyncConnection,
    run_id: UUID,
    finalization: _ExternalBuildFinalization,
) -> None:
    for name, head in finalization.heads.items():
        stored = StoredArtifact(finalization.keys[name], head.etag, Sensitivity.SENSITIVE, "build")
        row = register_artifact_row(stored, owner_kind="runs", owner_id=run_id)
        await ARTIFACTS.insert(conn, row)


async def _record_build_step(
    conn: AsyncConnection,
    run_id: UUID,
    result: BuildStepResult,
) -> None:
    await conn.execute(
        "INSERT INTO run_steps (run_id, step, state, result) "
        "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
        (run_id, Jsonb(result.dump())),
    )


async def _mark_run_succeeded(
    conn: AsyncConnection,
    run_id: UUID,
    output: BuildOutput,
) -> None:
    await conn.execute(
        "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = %s "
        "WHERE id = %s AND state = %s",
        (
            output.kernel_ref,
            output.debuginfo_ref or None,
            RunState.SUCCEEDED.value,
            run_id,
            RunState.CREATED.value,
        ),
    )


async def _record_complete_build_audit(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
) -> None:
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="runs.complete_build",
            object_kind="runs",
            object_id=run.id,
            transition="created->succeeded",
            args={"run_id": str(run.id)},
            project=run.project,
        ),
    )


async def _cleanup_chunks_and_manifest(
    conn: AsyncConnection,
    store: ExternalBuildStore,
    run_id: UUID,
    entries: Sequence[ManifestEntry],
    prefix: str,
) -> None:
    for entry in entries:
        if entry.chunks is None:
            continue
        for part_number in range(1, len(entry.chunks) + 1):
            key = chunk_key(prefix, entry.name, part_number)
            try:
                await asyncio.to_thread(store.delete, key)
            except CategorizedError as exc:
                _log.warning("chunk cleanup failed for %s: %s", key, exc)
                return
    try:
        await upload_manifest.delete_manifest(conn, "runs", run_id)
    except CategorizedError as exc:
        _log.warning("manifest cleanup failed for run %s: %s", run_id, exc)


__all__ = [
    "CompleteBuildConfigurationError",
    "CompleteBuildFinalizer",
    "CompleteBuildValidation",
    "CompleteBuildValidationError",
    "ExternalBuildStore",
    "ObjectStoreFactory",
]
