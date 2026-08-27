"""External-build finalization service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import kdive.config as config
from kdive.artifacts.catalog.registration import register_artifact_row
from kdive.artifacts.storage import HeadResult, MultipartCompletion, StoredArtifact
from kdive.artifacts.uploads import upload_manifest
from kdive.artifacts.uploads.reassembly import reassemble_chunked
from kdive.artifacts.uploads.uploads import ManifestEntry
from kdive.build_artifacts.results import BuildOutput, ValidatedUpload
from kdive.build_artifacts.validation import validate_external_artifacts
from kdive.config.core_settings import (
    BUILD_ARTIFACT_RETENTION_DAYS,
    UPLOAD_TTL_SECONDS,
    UPLOAD_WINDOW_MAX_TTL_MULTIPLE,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, INVESTIGATIONS
from kdive.domain.capacity.state import InvestigationState, RunState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import Run
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.serialization import JsonValue
from kdive.services.runs.build_catalog import BuildPublication, publish_or_reuse_build
from kdive.services.runs.steps import BuildStepResult
from kdive.services.runs.steps import existing_build_result as _existing_build_result
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)


class ExternalBuildStore(Protocol):
    """Object-store surface the external-build finalize path needs."""

    def head(self, key: str, *, version_id: str | None = None) -> HeadResult | None: ...
    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes: ...
    def delete_version(self, key: str, version_id: str) -> None: ...
    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str: ...
    def upload_part_copy(
        self,
        key: str,
        upload_id: str,
        *,
        part_number: int,
        source_key: str,
        source_version_id: str,
    ) -> str: ...
    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> MultipartCompletion: ...
    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


class CompleteBuildValidation(Protocol):
    """The upload-validation seam: validate a manifest for a given target ``arch`` (ADR-0343)."""

    def __call__(
        self,
        manifest: Sequence[ManifestEntry],
        keys: Mapping[str, str],
        declared_build_id: str | None,
        *,
        arch: str = "x86_64",
    ) -> ValidatedUpload: ...


type ObjectStoreFactory = Callable[[], ExternalBuildStore]

NO_UPLOAD_MANIFEST = "no_upload_manifest"
"""No upload window is recorded for this Run — never minted, or already reaped."""

UPLOAD_WINDOW_REPLACED = "upload_window_replaced"
"""A re-mint replaced the window this finalize validated (ADR-0448 §2)."""

# The expired-window rejection is raised as `CompleteBuildExpiredWindowError` rather than through
# `CompleteBuildConfigurationError`, because the response layer needs the clock pair to render the
# self-correcting payload. Its reason string is `upload_manifest.UPLOAD_WINDOW_EXPIRED`, which
# lives beside the predicate that decides it so the investigations lane can name the same constant
# without importing a runs service module (ADR-0512).

# Every exception below is `eq=False` and unfrozen, deliberately. `contextlib` assigns
# `__traceback__` to an exception it re-raises out of an async context manager, so a *frozen*
# dataclass exception raised anywhere inside `advisory_xact_lock` dies with `FrozenInstanceError`
# instead of the rejection it carries — a trap that already cost this module two silently broken
# raise sites. `eq=False` restores the identity `__eq__`/`__hash__` an exception should have;
# unfreezing alone would set `__hash__ = None` and make two distinct rejections compare equal.


@dataclass(slots=True, eq=False)
class CompleteBuildConfigurationError(Exception):
    """Caller-correctable configuration rejection data for the MCP envelope."""

    data: dict[str, JsonValue]


@dataclass(slots=True, eq=False)
class CompleteBuildExpiredWindowError(Exception):
    """The Run's upload window lapsed before this finalize arrived (ADR-0448).

    Carries the raw Postgres clock pair rather than a rendered envelope: the response layer
    owns the agent-facing ``server_time`` / ``manifest_deadline`` / ``on_expiry`` rendering
    (ADR-0394), and this service does not import it.
    """

    stamp: upload_manifest.ManifestStamp


@dataclass(slots=True, eq=False)
class CompleteBuildValidationError(Exception):
    """External upload validation rejected the artifact set."""

    error: CategorizedError


@dataclass(slots=True, eq=False)
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
                conn, run.id, prepared, build_id=build_id, arch=_build_arch(run)
            )
            return await _finalize_external_build(
                conn,
                ctx,
                validated,
                cmdline=cmdline,
                source_provenance=source_provenance,
                object_store_factory=self.object_store_factory,
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
            raise CompleteBuildConfigurationError({"reason": NO_UPLOAD_MANIFEST})
        await _require_open_window(conn, run.id, manifest_row)
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
        run_id: UUID,
        prepared: _ExternalBuildCompletion,
        *,
        build_id: str | None,
        arch: str,
    ) -> _ExternalBuildFinalization:
        window_deadline = prepared.manifest_row.deadline
        if prepared.store is not None:
            window_deadline, chunk_heads, final_versions = await _reassemble_chunked_artifacts(
                conn,
                run_id,
                prepared.run.investigation_id,
                prepared.manifest_row,
                prepared.store,
            )
        else:
            chunk_heads = {}
            final_versions = {}

        try:
            validated = await asyncio.to_thread(
                self._validate_complete_build,
                list(prepared.manifest_row.entries),
                prepared.keys,
                build_id,
                arch,
                final_versions,
            )
        except CategorizedError as exc:
            raise CompleteBuildValidationError(exc) from exc

        return _ExternalBuildFinalization(
            prepared.run,
            output=validated.output,
            keys=prepared.keys,
            heads=validated.heads,
            verified_identities={
                prepared.keys[entry.name]: _manifest_content_identity(entry)
                for entry in prepared.manifest_row.entries
            },
            store=prepared.store,
            chunked=prepared.has_chunks,
            chunk_heads=chunk_heads,
            window_deadline=window_deadline,
        )

    def _validate_complete_build(
        self,
        manifest: Sequence[ManifestEntry],
        keys: Mapping[str, str],
        declared_build_id: str | None,
        arch: str,
        exact_versions: Mapping[str, str],
    ) -> ValidatedUpload:
        if self.validate_complete_build is not None:
            return self.validate_complete_build(manifest, keys, declared_build_id, arch=arch)
        store = self.object_store_factory()
        return validate_external_artifacts(
            _VersionPinnedStore(store, exact_versions),
            manifest=manifest,
            keys=keys,
            declared_build_id=declared_build_id,
            arch=arch,
        )


@dataclass(slots=True)
class _VersionPinnedStore:
    """Bind every validation HEAD/range read to one captured immutable version."""

    store: ExternalBuildStore
    versions: Mapping[str, str]
    _observed: dict[str, str] = field(default_factory=dict)

    def head(self, key: str) -> HeadResult | None:
        version_id = self.versions.get(key) or self._observed.get(key)
        head = self.store.head(key, version_id=version_id) if version_id else self.store.head(key)
        if head is not None:
            self._observed[key] = head.version_id
        return head

    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes:
        version_id = version_id or self.versions.get(key) or self._observed.get(key)
        if version_id is None:
            return self.store.get_range(key, start=start, length=length)
        return self.store.get_range(key, start=start, length=length, version_id=version_id)


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
    verified_identities: dict[str, JsonValue]
    store: ExternalBuildStore | None
    chunked: bool
    chunk_heads: dict[str, HeadResult]
    window_deadline: datetime
    """The deadline of the manifest these artifacts were validated against — the window's identity.

    A manifest row carrying a different deadline at commit time is one a concurrent re-mint
    replaced, not the one this finalize read (ADR-0448 §2).
    """


def _manifest_content_identity(entry: ManifestEntry) -> JsonValue:
    """Return the validator-backed identity, excluding an advisory multipart whole hash."""
    if entry.chunks is None:
        return {"checksum_sha256": entry.sha256}
    return {
        "chunks": [
            {"checksum_sha256": chunk.sha256, "size_bytes": chunk.size_bytes}
            for chunk in entry.chunks
        ],
        "size_bytes": entry.size_bytes,
    }


async def _require_open_window(
    conn: AsyncConnection, run_id: UUID, manifest_row: upload_manifest.UploadManifest
) -> None:
    """Reject a finalize past the manifest deadline; return while the window is open (ADR-0448).

    Sited in ``_prepare`` so it governs the single-PUT and chunked paths from one place — the
    asymmetry #1534 reported was two paths reading (or not reading) the same field independently.
    The deadline is measured against the Postgres clock that stamped it and that the upload reaper
    measures against, never a Python-side ``datetime.now()``, so the two cannot reach opposite
    verdicts on one manifest under clock skew.

    ``now()`` is ``transaction_timestamp()`` and the whole request runs inside one transaction, so
    this is a verdict on the request's *arrival*: a finalize that arrived inside the window is not
    rejected for the time it then spends reading a multi-GiB payload. Arrival is not sufficient on
    its own — on the single-PUT path nothing is locked while that payload is read, so the reaper
    can collect the window meanwhile — which is why ``_finalize_external_build`` re-reads the
    manifest under the ``RUN`` lock before committing. This check is the fail-fast that keeps a
    lapsed window from being read at all, and the one that produces the self-correcting payload.

    Raises:
        CompleteBuildExpiredWindowError: The window closed before this finalize arrived.
    """
    stamp = await upload_manifest.deadline_stamp(conn, manifest_row)
    if stamp.expired:
        _log.info(
            "runs.complete_build rejected: upload window expired (run %s, deadline %s, "
            "server_time %s)",
            run_id,
            stamp.deadline,
            stamp.server_time,
        )
        raise CompleteBuildExpiredWindowError(stamp)


async def _reassemble_chunked_artifacts(
    conn: AsyncConnection,
    run_id: UUID,
    investigation_id: UUID,
    manifest_row: upload_manifest.UploadManifest,
    store: ExternalBuildStore,
) -> tuple[datetime, dict[str, HeadResult], dict[str, str]]:
    """Extend the window, reassemble chunked artifacts, and return their deadline and HEADs.

    The ``RUN`` lock taken here is transaction-scoped and this ``conn.transaction()`` is a
    savepoint (the request's transaction is already open), so ``RELEASE SAVEPOINT`` does *not*
    drop it: the lock is held until successful finalization's explicit commit. That is deliberate
    — it keeps the reaper off the chunk objects through reassembly and row registration, then the
    commit releases the lock before the post-commit exact chunk deletes. It is why the unlocked
    stretch ``_require_unreaped_window`` guards exists only on the single-PUT path.

    The savepoint also commits the extension independently of the rest of the finalize, which is
    why the extension has to be bounded here rather than unwound later. Every failure past this
    point — a reassembly error, a validation rejection — is caught at the MCP tool layer and
    returned as a ``ToolResponse``, so the pooled connection exits its ``async with`` cleanly and
    psycopg commits. Nothing undoes the refresh, so an unbounded one let a retry loop hold its
    uncommitted objects forever (#1553); ``max_window`` is the bound (ADR-0511).
    """
    ttl = timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))
    max_window = ttl * config.require(UPLOAD_WINDOW_MAX_TTL_MULTIPLE)
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
        advisory_xact_lock(conn, LockScope.RUN, run_id),
    ):
        refreshed = await upload_manifest.refresh_deadline(
            conn, "runs", run_id, ttl, max_window=max_window
        )
    if refreshed is None:
        # `_require_open_window` passed on this transaction's clock, and `now()` is
        # `transaction_timestamp()`, so the refresh predicate cannot have flipped on time since:
        # a declined refresh here means the row is gone, reaped between the two reads.
        raise CompleteBuildConfigurationError({"reason": NO_UPLOAD_MANIFEST})
    if refreshed.capped:
        # The one place a spent extension budget is visible. The reassembly still runs — it holds
        # the `RUN` lock the reaper needs — but this window will not outlive its deadline again,
        # so an operator watching a Run retry in a loop sees why it eventually stops.
        #
        # The cap is reported as a fact beside the deadline rather than as the thing that bound
        # this refresh, because it is not always what did: a `KDIVE_UPLOAD_TTL_SECONDS` lowered
        # after the mint leaves a standing deadline past `window_started_at + max_window`, and
        # naming the cap as the cause there would contradict the deadline printed next to it.
        _log.warning(
            "runs.complete_build: upload window extension capped — the deadline stands at %s "
            "(run %s, cap %s past its mint); artifacts.create_run_upload re-mints a fresh window",
            refreshed.deadline,
            run_id,
            max_window,
        )
    try:
        chunk_heads, final_versions = await _reassemble_artifacts(manifest_row, store)
    except CategorizedError as exc:
        recorded = await _existing_build_result(conn, run_id)
        if recorded is not None:
            raise _CompleteBuildAlreadyRecorded(recorded) from exc
        raise
    return refreshed.deadline, chunk_heads, final_versions


async def _reassemble_artifacts(
    manifest_row: upload_manifest.UploadManifest,
    store: ExternalBuildStore,
) -> tuple[dict[str, HeadResult], dict[str, str]]:
    chunk_heads: dict[str, HeadResult] = {}
    final_versions: dict[str, str] = {}
    for entry in manifest_row.entries:
        if entry.chunks is not None:
            heads, completion = await asyncio.to_thread(
                reassemble_chunked,
                store,
                prefix=manifest_row.prefix,
                final_key=f"{manifest_row.prefix}{entry.name}",
                entry=entry,
            )
            chunk_heads.update(heads)
            final_versions[f"{manifest_row.prefix}{entry.name}"] = completion.version_id
    return chunk_heads, final_versions


def _require_created_run(run: Run) -> None:
    if run.state is not RunState.CREATED:
        raise CompleteBuildConfigurationError({"current_status": run.state.value})


def _build_arch(run: Run) -> str:
    """The target arch for payload validation, read straight from the persisted build profile.

    The build profile was arch-validated at ``runs.create`` (ADR-0343); reading the field here
    (defaulting to ``x86_64`` when absent) — rather than re-parsing the whole profile — keeps a
    Run finalizable even if the arch vocabulary shifts after create, and the validator's own
    fail-fast is the backstop for an unrecognized value. A present-but-non-string arch is a
    corrupt profile (unreachable via ``runs.create``): fail loudly rather than mask it as x86_64.
    """
    arch = run.build_profile.get("arch")
    if arch is None:
        return "x86_64"
    if not isinstance(arch, str):
        raise CompleteBuildConfigurationError({"reason": "invalid_build_profile_arch"})
    return arch


async def _finalize_external_build(
    conn: AsyncConnection,
    ctx: RequestContext,
    finalization: _ExternalBuildFinalization,
    *,
    cmdline: str | None,
    source_provenance: dict[str, str | bool | list[str]] | None = None,
    object_store_factory: ObjectStoreFactory,
) -> BuildStepResult:
    candidate = BuildStepResult(
        kernel_ref=finalization.output.kernel_ref,
        debuginfo_ref=finalization.output.debuginfo_ref,
        initrd_ref=finalization.keys.get("initrd"),
        build_id=finalization.output.build_id,
        cmdline=cmdline,
        build_provenance=source_provenance,
    )
    run = finalization.run
    heads = _artifact_heads_by_key(finalization)
    publication: BuildPublication | None = None
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, run.investigation_id),
        advisory_xact_lock(conn, LockScope.RUN, run.id),
    ):
        investigation = await INVESTIGATIONS.get(conn, run.investigation_id)
        if investigation is None or investigation.state not in {
            InvestigationState.OPEN,
            InvestigationState.ACTIVE,
        }:
            raise CompleteBuildConfigurationError({"reason": "investigation_not_accepting_upload"})
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None:
            raise CompleteBuildConfigurationError({})
        state = RunState(row["state"])
        if state is RunState.SUCCEEDED:
            return await _existing_build_result(conn, run.id) or candidate
        if state is not RunState.CREATED:
            raise CompleteBuildConfigurationError({"current_status": state.value})
        await _require_unreaped_window(conn, run.id, finalization.window_deadline)
        publication = await publish_or_reuse_build(
            conn,
            run=run,
            result=candidate,
            heads=heads,
            verified_identities=finalization.verified_identities,
            retention=timedelta(days=config.require(BUILD_ARTIFACT_RETENTION_DAYS)),
        )
        result = _published_result(publication)
        if publication.created:
            await _insert_published_artifact_rows(conn, run.investigation_id, result, heads)
        await _insert_run_only_artifact_rows(conn, run.id, candidate, finalization)
        await _record_build_step(conn, run.id, result)
        await _mark_run_succeeded(conn, run.id, result)
        await _record_complete_build_audit(conn, ctx, run)
        if not finalization.chunked:
            await upload_manifest.delete_manifest(conn, "runs", run.id)
    try:
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    if finalization.chunked and finalization.store is not None:
        await _cleanup_chunks_and_manifest(
            conn,
            finalization.store,
            run.id,
            finalization.chunk_heads,
        )
    if publication is not None and not publication.created:
        try:
            store = finalization.store or object_store_factory()
        except CategorizedError as exc:
            _log.warning("losing build cleanup setup failed: %s", exc)
        else:
            await _cleanup_losing_build_versions(store, candidate, heads)
    return result


async def _require_unreaped_window(
    conn: AsyncConnection, run_id: UUID, expected_deadline: datetime
) -> None:
    """Refuse to commit unless the validated window is still the one on the row (ADR-0448 §2).

    ``_require_open_window`` judges the request's arrival. On the **single-PUT** path nothing is
    locked between that check and the commit, and reading the payload takes long enough for the
    30-second upload reaper to take this same ``RUN`` lock, delete every still-uncommitted object
    under the Run's prefix, and drop the manifest. (The chunked path holds the ``RUN`` lock from
    ``_reassemble_chunked_artifacts`` onward, so no reaper or re-mint can interleave there; this
    check simply re-asserts an invariant that path already has.)

    Presence alone is not enough. A reap followed by a re-mint leaves *a* manifest row, and the
    run object keys are owner-addressed, so nothing downstream would notice the swap — the commit
    would register ``artifacts`` rows carrying the deleted objects' etags and mark the Run
    ``succeeded`` with a dangling ``kernel_ref``. The deadline of the window that was validated is
    therefore the identity compared here: it is stamped from ``now()`` on every re-mint, so a
    different value is a different window.

    Raises:
        CompleteBuildConfigurationError: The window was reaped or replaced; the caller must
            re-mint and finalize against the window it actually uploaded to.
    """
    current = await upload_manifest.window_deadline(conn, "runs", run_id)
    if current is None:
        _log.info(
            "runs.complete_build rejected: upload window reaped mid-finalize (run %s)", run_id
        )
        raise CompleteBuildConfigurationError({"reason": NO_UPLOAD_MANIFEST})
    if current != expected_deadline:
        _log.info(
            "runs.complete_build rejected: upload window replaced mid-finalize (run %s)", run_id
        )
        raise CompleteBuildConfigurationError({"reason": UPLOAD_WINDOW_REPLACED})


def _artifact_heads_by_key(
    finalization: _ExternalBuildFinalization,
) -> dict[str, HeadResult]:
    """Translate manifest names to the object keys required by the catalog publisher."""
    return {finalization.keys[name]: head for name, head in finalization.heads.items()}


def _published_result(publication: BuildPublication) -> BuildStepResult:
    """Attach the selected generation metadata to its immutable stored build result."""
    stored = BuildStepResult.load(publication.build.build_result)
    if stored is None:  # Invariant: build_catalog stores BuildStepResult.dump().
        raise RuntimeError("investigation build has an invalid stored build result")
    return replace(
        stored,
        build_ref=publication.build.build_ref,
        expires_at=publication.build.expires_at.isoformat(),
        artifact_versions={
            name: artifact["version_id"] for name, artifact in publication.build.artifacts.items()
        },
    )


async def _insert_published_artifact_rows(
    conn: AsyncConnection,
    investigation_id: UUID,
    result: BuildStepResult,
    heads: Mapping[str, HeadResult],
) -> None:
    for key in result.refs().values():
        head = heads[key]
        stored = StoredArtifact(key, head.etag, Sensitivity.SENSITIVE, "build", head.version_id)
        row = register_artifact_row(stored, owner_kind="investigations", owner_id=investigation_id)
        await ARTIFACTS.insert(conn, row)


async def _insert_run_only_artifact_rows(
    conn: AsyncConnection,
    run_id: UUID,
    result: BuildStepResult,
    finalization: _ExternalBuildFinalization,
) -> None:
    """Keep non-reusable uploaded provenance, such as effective config, Run-owned."""
    reusable_keys = set(result.refs().values())
    for name, head in finalization.heads.items():
        key = finalization.keys[name]
        if key in reusable_keys:
            continue
        stored = StoredArtifact(key, head.etag, Sensitivity.SENSITIVE, "build", head.version_id)
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
    result: BuildStepResult,
) -> None:
    await conn.execute(
        "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, build_ref = %s, state = %s "
        "WHERE id = %s AND state = %s",
        (
            result.kernel_ref,
            result.debuginfo_ref,
            result.build_ref,
            RunState.SUCCEEDED.value,
            run_id,
            RunState.CREATED.value,
        ),
    )


async def _cleanup_losing_build_versions(
    store: ExternalBuildStore,
    result: BuildStepResult,
    heads: Mapping[str, HeadResult],
) -> None:
    """Delete only a converged candidate's exact reusable object versions after commit."""
    for key in result.refs().values():
        head = heads[key]
        try:
            await asyncio.to_thread(store.delete_version, key, head.version_id)
        except CategorizedError as exc:
            _log.warning("losing build cleanup failed for %s: %s", key, exc)


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
    chunk_heads: Mapping[str, HeadResult],
) -> None:
    for key, head in chunk_heads.items():
        try:
            await asyncio.to_thread(store.delete_version, key, head.version_id)
        except CategorizedError as exc:
            _log.warning("chunk cleanup failed for %s: %s", key, exc)
            return
    try:
        await upload_manifest.delete_manifest(conn, "runs", run_id)
    except CategorizedError as exc:
        _log.warning("manifest cleanup failed for run %s: %s", run_id, exc)


__all__ = [
    "NO_UPLOAD_MANIFEST",
    "UPLOAD_WINDOW_REPLACED",
    "CompleteBuildConfigurationError",
    "CompleteBuildExpiredWindowError",
    "CompleteBuildFinalizer",
    "CompleteBuildValidation",
    "CompleteBuildValidationError",
    "ExternalBuildStore",
    "ObjectStoreFactory",
]
