"""`runs.complete_build` MCP handler."""

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
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts.reassembly import reassemble_chunked
from kdive.artifacts.storage import HeadResult, StoredArtifact, chunk_key
from kdive.artifacts.uploads import ManifestEntry
from kdive.build_artifacts.results import BuildOutput, ValidatedUpload
from kdive.build_artifacts.validation import validate_external_artifacts
from kdive.components.catalog import load_fixture_catalog
from kdive.components.requirements import ConfigRequirements
from kdive.config.core_settings import UPLOAD_TTL_SECONDS
from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import Run
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.profiles.build import BuildProfile, ExternalBuildProfile
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.steps import BuildStepResult, platform_owned_cmdline_token
from kdive.services.runs.steps import existing_build_result as _existing_build_result
from kdive.store.objectstore import (
    object_store_from_env,
    register_artifact_row,
)

_log = logging.getLogger(__name__)


class ExternalBuildStore(Protocol):
    """Object-store surface the external-build finalize path needs (ADR-0104)."""

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
    [Sequence[ManifestEntry], Mapping[str, str], str | None, ConfigRequirements | None],
    ValidatedUpload,
]
type ObjectStoreFactory = Callable[[], ExternalBuildStore]


@dataclass(frozen=True, slots=True)
class CompleteBuildHandlers:
    """External-build completion handler with validation and object-store seams."""

    validate_complete_build: CompleteBuildValidation | None = None
    object_store_factory: ObjectStoreFactory = object_store_from_env

    async def complete_build(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        run_id: str,
        *,
        build_id: str | None,
        cmdline: str,
    ) -> ToolResponse:
        """Validate an external Run's uploads and finalize it ``created -> succeeded``."""
        uid = _as_uuid(run_id)
        if uid is None:
            return _config_error(run_id)
        owned = platform_owned_cmdline_token(cmdline)
        if owned is not None:
            return _config_error(
                run_id, data={"reason": "cmdline_overrides_platform_args", "token": owned}
            )
        with bind_context(principal=ctx.principal):
            async with pool.connection() as conn:
                prepared = await self._prepare_external_build_completion(conn, ctx, uid, run_id)
                if isinstance(prepared, ToolResponse):
                    return prepared
                finalization = await self._validate_external_build_upload(
                    conn,
                    uid,
                    run_id,
                    prepared,
                    build_id=build_id,
                    cmdline=cmdline,
                )
                if isinstance(finalization, ToolResponse):
                    return finalization
                return await _finalize_external_build(conn, ctx, finalization)

    async def _validate_external_build_upload(
        self,
        conn: AsyncConnection,
        uid: UUID,
        run_id: str,
        prepared: _ExternalBuildCompletion,
        *,
        build_id: str | None,
        cmdline: str,
    ) -> _ExternalBuildFinalization | ToolResponse:
        """Reassemble chunked uploads and validate the external-build artifact set."""
        if prepared.store is not None:
            guard = await _reassemble_chunked_artifacts(
                conn, uid, run_id, prepared.manifest_row, prepared.store
            )
            if guard is not None:
                return guard

        try:
            validated = await asyncio.to_thread(
                self._validate_complete_build,
                list(prepared.manifest_row.entries),
                prepared.keys,
                build_id,
                prepared.requirements,
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(run_id, exc)

        return _ExternalBuildFinalization(
            prepared.run,
            output=validated.output,
            cmdline=cmdline,
            keys=prepared.keys,
            heads=validated.heads,
            store=prepared.store,
            entries=prepared.manifest_row.entries,
            prefix=prepared.manifest_row.prefix,
            chunked=prepared.has_chunks,
        )

    async def _prepare_external_build_completion(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        uid: UUID,
        run_id: str,
    ) -> _ExternalBuildCompletion | ToolResponse:
        run = await RUNS.get(conn, uid)
        if run is None or run.project not in ctx.projects:
            return _config_error(run_id)
        require_role(ctx, run.project, Role.OPERATOR)

        recorded = await _existing_build_result(conn, uid)
        if recorded is not None:
            return _complete_envelope(uid, recorded)

        try:
            profile = _external_build_profile(run)
            requirements = _external_config_requirements(profile)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(run_id, exc)
        guard = _created_run_guard(run)
        if guard is not None:
            return guard

        manifest_row = await upload_manifest.get_manifest(conn, "runs", uid)
        if manifest_row is None:
            return _config_error(run_id, data={"reason": "no_upload_manifest"})
        has_chunks = any(entry.chunks is not None for entry in manifest_row.entries)
        keys = {entry.name: f"{manifest_row.prefix}{entry.name}" for entry in manifest_row.entries}
        try:
            store = self.object_store_factory() if has_chunks else None
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(run_id, exc)
        return _ExternalBuildCompletion(
            run=run,
            manifest_row=manifest_row,
            keys=keys,
            requirements=requirements,
            has_chunks=has_chunks,
            store=store,
        )

    def _validate_complete_build(
        self,
        manifest: Sequence[ManifestEntry],
        keys: Mapping[str, str],
        declared_build_id: str | None,
        profile_requirements: ConfigRequirements | None,
    ) -> ValidatedUpload:
        if self.validate_complete_build is not None:
            return self.validate_complete_build(
                manifest, keys, declared_build_id, profile_requirements
            )
        return validate_external_artifacts(
            self.object_store_factory(),
            manifest=manifest,
            keys=keys,
            declared_build_id=declared_build_id,
            profile_requirements=profile_requirements,
        )


@dataclass(frozen=True, slots=True)
class _ExternalBuildCompletion:
    """Prepared external-build completion inputs."""

    run: Run
    manifest_row: upload_manifest.UploadManifest
    keys: dict[str, str]
    requirements: ConfigRequirements | None
    has_chunks: bool
    store: ExternalBuildStore | None


@dataclass(frozen=True, slots=True)
class _ExternalBuildFinalization:
    """Validated external-build finalization inputs."""

    run: Run
    output: BuildOutput
    cmdline: str
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
) -> ToolResponse | None:
    """Refresh the upload window under the per-Run lock, then reassemble chunked artifacts."""
    ttl = timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, uid):
        refreshed = await upload_manifest.refresh_deadline(conn, "runs", uid, ttl)
    if not refreshed:
        if await upload_manifest.get_manifest(conn, "runs", uid) is None:
            return _config_error(run_id, data={"reason": "no_upload_manifest"})
        return _config_error(run_id, data={"reason": "upload_window_expired"})
    prefix = manifest_row.prefix
    try:
        for entry in manifest_row.entries:
            if entry.chunks is not None:
                await asyncio.to_thread(
                    reassemble_chunked,
                    store,
                    prefix=prefix,
                    final_key=f"{prefix}{entry.name}",
                    entry=entry,
                )
    except CategorizedError as exc:
        recorded = await _existing_build_result(conn, uid)
        if recorded is not None:
            return _complete_envelope(uid, recorded)
        return ToolResponse.failure_from_error(run_id, exc)
    return None


def _external_build_profile(run: Run) -> ExternalBuildProfile:
    parsed = BuildProfile.parse(run.build_profile)
    if not isinstance(parsed, ExternalBuildProfile):
        raise CategorizedError(
            "run is not an external build",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return parsed


def _created_run_guard(run: Run) -> ToolResponse | None:
    """Reject a non-CREATED Run; ``None`` means proceed to finalize."""
    if run.state is not RunState.CREATED:
        return _config_error(str(run.id), data={"current_status": run.state.value})
    return None


def _external_config_requirements(profile: ExternalBuildProfile) -> ConfigRequirements | None:
    if profile.profile_requirements is None:
        return None
    entry = load_fixture_catalog().profile(
        profile.profile_requirements.provider,
        profile.profile_requirements.name,
    )
    if entry is None:
        raise CategorizedError(
            "unknown build profile requirements",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return entry.requires.config


def _complete_envelope(run_id: UUID, result: BuildStepResult) -> ToolResponse:
    """Build the success envelope from a ledger ``result``."""
    return ToolResponse.success(
        str(run_id), "succeeded", suggested_next_actions=["runs.get"], refs=result.refs()
    )


async def _finalize_external_build(
    conn: AsyncConnection,
    ctx: RequestContext,
    finalization: _ExternalBuildFinalization,
) -> ToolResponse:
    """Write artifact rows, ledger result, and created -> succeeded under the per-Run lock."""
    result = BuildStepResult(
        kernel_ref=finalization.output.kernel_ref,
        debuginfo_ref=finalization.output.debuginfo_ref,
        initrd_ref=finalization.keys.get("initrd"),
        build_id=finalization.output.build_id,
        cmdline=finalization.cmdline,
    )
    run = finalization.run
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None:
            return _config_error(str(run.id))
        state = RunState(row["state"])
        if state is RunState.SUCCEEDED:
            recorded = await _existing_build_result(conn, run.id)
            return _complete_envelope(run.id, recorded or result)
        if state is not RunState.CREATED:
            return _config_error(str(run.id), data={"current_status": state.value})
        for name, head in finalization.heads.items():
            stored = StoredArtifact(
                finalization.keys[name], head.etag, Sensitivity.SENSITIVE, "build"
            )
            await ARTIFACTS.insert(
                conn, register_artifact_row(stored, owner_kind="runs", owner_id=run.id)
            )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run.id, Jsonb(result.dump())),
        )
        await conn.execute(
            "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = %s "
            "WHERE id = %s AND state = %s",
            (
                finalization.output.kernel_ref,
                finalization.output.debuginfo_ref or None,
                RunState.SUCCEEDED.value,
                run.id,
                RunState.CREATED.value,
            ),
        )
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
    return _complete_envelope(run.id, result)


async def _cleanup_chunks_and_manifest(
    conn: AsyncConnection,
    store: ExternalBuildStore,
    run_id: UUID,
    entries: Sequence[ManifestEntry],
    prefix: str,
) -> None:
    """Best-effort post-commit reclamation of the reassembled chunks, then the manifest."""
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
    "CompleteBuildHandlers",
    "CompleteBuildValidation",
    "ExternalBuildStore",
    "ObjectStoreFactory",
]
