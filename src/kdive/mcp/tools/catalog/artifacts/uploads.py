"""Artifact upload-admission handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import NamedTuple, Protocol, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts.storage import PresignedUpload, PresignPutRequest
from kdive.artifacts.uploads import (
    MAX_PART_BYTES,
    MAX_PARTS,
    MIN_PART_BYTES,
    SINGLE_PUT_MAX_BYTES,
    ChunkEntry,
    ManifestEntry,
)
from kdive.build_artifacts.validation import EFFECTIVE_CONFIG_MAX_BYTES
from kdive.config.core_settings import MAX_UPLOAD_BYTES, UPLOAD_TTL_SECONDS
from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capacity.state import RunState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.profiles.build import BuildProfile, ExternalBuildProfile
from kdive.profiles.provider_policy import rootfs_upload_window_allowed
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.serialization import JsonValue
from kdive.store.objectstore import (
    artifact_key,
    chunk_key,
    object_store_from_env,
    owner_prefix,
)

_log = logging.getLogger(__name__)

_TENANT = "local"
# Literal upload tool names and accepted artifact-name vocabularies. Public so the
# ``artifacts.expected_uploads`` discovery tool (ADR-0166) projects the same sets the
# validator below enforces — the advertisement can never drift from the accepted names.
CREATE_RUN_UPLOAD_TOOL = "artifacts.create_run_upload"
CREATE_SYSTEM_UPLOAD_TOOL = "artifacts.create_system_upload"
RUN_ARTIFACT_NAMES = frozenset({"effective_config", "kernel", "initrd", "vmlinux"})
_ROOTFS_NAME = "rootfs"
SYSTEM_ARTIFACT_NAMES = frozenset({_ROOTFS_NAME})
_RETENTION_CLASS = "build"

# Upper bound on an offending artifact-name string echoed back in an error's ``data.value``
# (ADR-0166). The name is user-supplied; echoing a short string makes the rejection
# self-correcting, but a longer or non-string value is never reflected so no large or
# binary payload can ride the error envelope back to the client.
_MAX_ECHOED_NAME_LEN = 64
_REQUIRED_DECLARATION_FIELDS = ("name", "sha256", "size_bytes")

# JSON Schema for one upload-declaration item, advertised to MCP clients via the
# registrar's ``json_schema_extra`` (ADR-0173). It is *advertisement only*: the registrar
# keeps the runtime parameter type a permissive ``Mapping`` so a malformed declaration
# still reaches ``_validate_one_declaration`` and gets ADR-0166's self-correcting
# ``bad_artifact_declaration`` rejection instead of a generic pydantic boundary error. The
# ``required`` list is derived from ``_REQUIRED_DECLARATION_FIELDS`` so the advertised
# shape can never drift from what the validator enforces.
_CHUNK_ITEM_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": ["sha256", "size_bytes"],
    "properties": {
        "sha256": {"type": "string", "description": "Base64-encoded SHA-256 of this chunk."},
        "size_bytes": {"type": "integer", "description": "This chunk's size in bytes."},
    },
}
UPLOAD_DECLARATION_ITEM_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": list(_REQUIRED_DECLARATION_FIELDS),
    "properties": {
        "name": {
            "type": "string",
            "description": "Accepted artifact name (see artifacts.expected_uploads).",
        },
        "sha256": {
            "type": "string",
            "description": "Base64-encoded SHA-256 of the whole object.",
        },
        "size_bytes": {"type": "integer", "description": "Total object size in bytes."},
        "chunks": {
            "type": "array",
            "description": "Optional chunked-upload parts; omit for a single PUT.",
            "items": _CHUNK_ITEM_SCHEMA,
        },
    },
}


# One single-PUT and one chunked declaration per owner-kind, rendered into the generated
# tool reference (ADR-0047/0173). The item *shape* is shared, but the example artifact
# names match each tool's accepted vocabulary (run: kernel/…; system: rootfs). The chunked
# example's chunk sizes sum to ``size_bytes`` with the non-final part at the 5 MiB minimum,
# mirroring the validator's contract.
def _declaration_examples(single_name: str, chunked_name: str) -> list[JsonValue]:
    return [
        [{"name": single_name, "sha256": "rL0Y20zC...base64...", "size_bytes": 12582912}],
        [
            {
                "name": chunked_name,
                "sha256": "kZ8s1f9q...base64...",
                "size_bytes": 7340032,
                "chunks": [
                    {"sha256": "p1...base64...", "size_bytes": 5242880},
                    {"sha256": "p2...base64...", "size_bytes": 2097152},
                ],
            }
        ],
    ]


RUN_DECLARATION_EXAMPLES: list[JsonValue] = _declaration_examples("kernel", "vmlinux")
SYSTEM_DECLARATION_EXAMPLES: list[JsonValue] = _declaration_examples("rootfs", "rootfs")


def _upload_ttl() -> timedelta:
    return timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))


def _max_upload_bytes() -> int:
    return config.require(MAX_UPLOAD_BYTES)


def _presign_ttl_seconds() -> int:
    return min(3600, int(_upload_ttl().total_seconds()))


class _PresignStore(Protocol):
    def presign_put(self, request: PresignPutRequest) -> PresignedUpload: ...


class _MaterializedUpload(NamedTuple):
    entry: ManifestEntry
    key: str
    presigned: PresignedUpload
    part_number: int | None = None


type ArtifactDeclaration = Mapping[str, object]
"""Raw MCP declaration for one artifact upload before value validation."""


@dataclass(frozen=True)
class _UploadOwnerSpec:
    owner_kind: upload_manifest.UploadOwnerKind
    required_role: Role
    lock_scope: LockScope
    allowed_names: frozenset[str]
    next_action: str
    project: Callable[[AsyncConnection, UUID], Awaitable[str | None]]
    accepts_upload: Callable[[AsyncConnection, UUID, ProviderResolver], Awaitable[bool]]


def _bad_declaration(
    object_id: str, allowed: frozenset[str], *, field: str, value: object = None
) -> ToolResponse:
    """Build a self-correcting ``bad_artifact_declaration`` rejection (ADR-0166).

    Names the failing ``field`` and lists the accepted artifact-name vocabulary so the
    response is self-correcting from a black-box client. The offending ``value`` is
    echoed in ``data.value`` only for a name rejection and only when it is a short string
    (``<= _MAX_ECHOED_NAME_LEN``); a non-string or oversized value is never reflected, so
    no large or binary payload rides the error envelope back.

    Args:
        object_id: The owner id the failed declaration targets.
        allowed: The accepted artifact-name set for this owner kind.
        field: Which declared field failed (``name``/``sha256``/``size_bytes``/``chunks``).
        value: The offending value; echoed only when ``field == "name"`` and it is a
            short string.

    Returns:
        A ``configuration_error`` :class:`ToolResponse` with structured ``data`` and a
        non-null human-readable ``detail``.
    """
    accepted = sorted(allowed)
    accepted_names: list[JsonValue] = list(accepted)
    data: dict[str, JsonValue] = {
        "reason": "bad_artifact_declaration",
        "field": field,
        "accepted_names": accepted_names,
    }
    if field == "name" and isinstance(value, str) and len(value) <= _MAX_ECHOED_NAME_LEN:
        data["value"] = value
    detail = f"artifact declaration rejected: field {field!r} must be one of {', '.join(accepted)}"
    return _config_error(object_id, detail=detail, data=data)


def _validate_one_declaration(
    object_id: str, declaration: ArtifactDeclaration, allowed: frozenset[str]
) -> tuple[str, str, int] | ToolResponse:
    """Validate one declaration's required fields, naming the specific failure (ADR-0166)."""
    for key in _REQUIRED_DECLARATION_FIELDS:
        if key not in declaration:
            return _bad_declaration(object_id, allowed, field=key)
    name, sha256, size = declaration["name"], declaration["sha256"], declaration["size_bytes"]
    if not isinstance(name, str) or name not in allowed:
        return _bad_declaration(object_id, allowed, field="name", value=name)
    if not isinstance(sha256, str):
        return _bad_declaration(object_id, allowed, field="sha256")
    if not isinstance(size, int):
        return _bad_declaration(object_id, allowed, field="size_bytes")
    return name, sha256, size


def _validate_artifact_declarations(
    object_id: str, artifacts: Sequence[ArtifactDeclaration], allowed: frozenset[str], cap: int
) -> list[ManifestEntry] | ToolResponse:
    entries: list[ManifestEntry] = []
    for declaration in artifacts:
        validated_declaration = _validate_one_declaration(object_id, declaration, allowed)
        if isinstance(validated_declaration, ToolResponse):
            return validated_declaration
        name, sha256, size = validated_declaration
        artifact_cap = EFFECTIVE_CONFIG_MAX_BYTES if name == "effective_config" else cap
        raw_chunks = declaration.get("chunks")
        if raw_chunks is None:
            if size <= 0 or size > min(SINGLE_PUT_MAX_BYTES, artifact_cap):
                return _config_error(object_id, data={"reason": "size_out_of_range"})
            entries.append(ManifestEntry(name=name, sha256=sha256, size_bytes=size))
            continue
        if name == "effective_config":
            return _config_error(object_id, data={"reason": "size_out_of_range"})
        validated = _validate_chunks(object_id, raw_chunks, size, artifact_cap, allowed)
        if isinstance(validated, ToolResponse):
            return validated
        entries.append(ManifestEntry(name=name, sha256=sha256, size_bytes=size, chunks=validated))
    if not entries:
        return _config_error(object_id, data={"reason": "no_artifacts_declared"})
    return entries


def _validate_chunks(
    object_id: str, raw_chunks: object, declared_total: int, cap: int, allowed: frozenset[str]
) -> tuple[ChunkEntry, ...] | ToolResponse:
    if not isinstance(raw_chunks, list) or not (1 <= len(raw_chunks) <= MAX_PARTS):
        return _config_error(object_id, data={"reason": "too_many_chunks"})
    chunks: list[ChunkEntry] = []
    total = 0
    last = len(raw_chunks) - 1
    for i, chunk in enumerate(raw_chunks):
        if not isinstance(chunk, Mapping):
            return _bad_declaration(object_id, allowed, field="chunks")
        chunk_map = cast("Mapping[str, object]", chunk)
        csha, csize = chunk_map.get("sha256"), chunk_map.get("size_bytes")
        if not isinstance(csha, str) or not isinstance(csize, int) or csize <= 0:
            return _bad_declaration(object_id, allowed, field="chunks")
        if csize > MAX_PART_BYTES:
            return _config_error(object_id, data={"reason": "size_out_of_range"})
        if i != last and csize < MIN_PART_BYTES:
            return _config_error(object_id, data={"reason": "chunk_too_small"})
        chunks.append(ChunkEntry(sha256=csha, size_bytes=csize))
        total += csize
    if total != declared_total or not (0 < declared_total <= cap):
        return _config_error(object_id, data={"reason": "chunk_size_mismatch"})
    return tuple(chunks)


def _materialize_uploads(
    entries: list[ManifestEntry],
    *,
    kind: upload_manifest.UploadOwnerKind,
    owner_id: UUID,
    store: _PresignStore,
) -> list[_MaterializedUpload]:
    uploads: list[_MaterializedUpload] = []
    expires_in = _presign_ttl_seconds()
    prefix = owner_prefix(_TENANT, kind, str(owner_id))
    for entry in entries:
        if entry.chunks is None:
            key = artifact_key(_TENANT, kind, str(owner_id), entry.name)
            uploads.append(
                _materialize_one(
                    store, key, entry.sha256, entry.size_bytes, entry, None, expires_in
                )
            )
            continue
        for part_number, chunk in enumerate(entry.chunks, start=1):
            key = chunk_key(prefix, entry.name, part_number)
            uploads.append(
                _materialize_one(
                    store, key, chunk.sha256, chunk.size_bytes, entry, part_number, expires_in
                )
            )
    return uploads


def _materialize_one(
    store: _PresignStore,
    key: str,
    sha256: str,
    size_bytes: int,
    entry: ManifestEntry,
    part_number: int | None,
    expires_in: int,
) -> _MaterializedUpload:
    presigned = store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=sha256,
            size_bytes=size_bytes,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class=_RETENTION_CLASS,
            expires_in=expires_in,
        )
    )
    return _MaterializedUpload(entry, key, presigned, part_number)


async def _run_project(conn: AsyncConnection, owner_id: UUID) -> str | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT project FROM runs WHERE id = %s", (owner_id,))
        row = await cur.fetchone()
    return row["project"] if row else None


async def _system_project(conn: AsyncConnection, owner_id: UUID) -> str | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT project FROM systems WHERE id = %s", (owner_id,))
        row = await cur.fetchone()
    return row["project"] if row else None


async def _run_accepts_upload(
    conn: AsyncConnection, owner_id: UUID, _resolver: ProviderResolver
) -> bool:
    run = await RUNS.get(conn, owner_id)
    if run is None or run.state is not RunState.CREATED:
        return False
    parsed = BuildProfile.parse(run.build_profile)
    return isinstance(parsed, ExternalBuildProfile)


async def _system_accepts_upload(
    conn: AsyncConnection, owner_id: UUID, resolver: ProviderResolver
) -> bool:
    system = await SYSTEMS.get(conn, owner_id)
    if system is None or system.state is not SystemState.DEFINED:
        return False
    parsed = ProvisioningProfile.parse(system.provisioning_profile)
    runtime = await resolver.runtime_for_system(conn, owner_id)
    return rootfs_upload_window_allowed(runtime.profile_policy, parsed)


_RUN_UPLOAD = _UploadOwnerSpec(
    owner_kind=upload_manifest.RUN_UPLOAD_OWNER,
    required_role=Role.CONTRIBUTOR,
    lock_scope=LockScope.RUN,
    allowed_names=RUN_ARTIFACT_NAMES,
    next_action="runs.complete_build",
    project=_run_project,
    accepts_upload=_run_accepts_upload,
)
_SYSTEM_UPLOAD = _UploadOwnerSpec(
    owner_kind=upload_manifest.SYSTEM_UPLOAD_OWNER,
    required_role=Role.OPERATOR,
    lock_scope=LockScope.SYSTEM,
    allowed_names=SYSTEM_ARTIFACT_NAMES,
    next_action="systems.provision_defined",
    project=_system_project,
    accepts_upload=_system_accepts_upload,
)


async def _create_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    spec: _UploadOwnerSpec,
    owner_id: str,
    artifacts: Sequence[ArtifactDeclaration],
    resolver: ProviderResolver,
    store: _PresignStore | None = None,
) -> ToolResponse:
    uid = _as_uuid(owner_id)
    if uid is None:
        return _config_error(owner_id)
    try:
        store = store or object_store_from_env()
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(
            owner_id, exc, suggested_next_actions=[_upload_tool_name(spec)]
        )

    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            project = await spec.project(conn, uid)
            if project is None or project not in ctx.projects:
                return _config_error(owner_id)
            require_role(ctx, project, spec.required_role)

            validated = _validate_artifact_declarations(
                owner_id, artifacts, spec.allowed_names, _max_upload_bytes()
            )
            if isinstance(validated, ToolResponse):
                return validated
            entries = validated

            prefix = owner_prefix(_TENANT, spec.owner_kind, str(uid))
            try:
                async with conn.transaction(), advisory_xact_lock(conn, spec.lock_scope, uid):
                    if not await spec.accepts_upload(conn, uid, resolver):
                        return _config_error(
                            owner_id, data={"reason": "owner_not_accepting_upload"}
                        )
                    uploads = _materialize_uploads(
                        entries,
                        kind=spec.owner_kind,
                        owner_id=uid,
                        store=store,
                    )
                    await upload_manifest.replace_manifest(
                        conn,
                        upload_manifest.UploadManifestReplaceRequest(
                            owner_kind=spec.owner_kind,
                            owner_id=uid,
                            prefix=prefix,
                            entries=entries,
                            ttl=_upload_ttl(),
                        ),
                    )
            except CategorizedError as exc:
                _log.warning("create_upload failed for %s %s: %s", spec.owner_kind, owner_id, exc)
                return ToolResponse.failure_from_error(owner_id, exc)

    items = [_upload_response(upload, next_action=spec.next_action) for upload in uploads]
    return ToolResponse.collection(
        owner_id,
        "upload_ready",
        items,
        suggested_next_actions=[spec.next_action],
        data={"owner_kind": spec.owner_kind},
    )


def _upload_response(upload: _MaterializedUpload, *, next_action: str) -> ToolResponse:
    return ToolResponse.success(
        upload.key,
        "upload_ready",
        suggested_next_actions=[next_action],
        refs={"upload_url": upload.presigned.url},
        data={
            "name": upload.entry.name,
            "artifact_name": upload.entry.name,
            "expires_in": str(_presign_ttl_seconds()),
            **({"part_number": str(upload.part_number)} if upload.part_number is not None else {}),
            **upload.presigned.required_headers,
        },
    )


def _upload_tool_name(spec: _UploadOwnerSpec) -> str:
    if spec.owner_kind == upload_manifest.RUN_UPLOAD_OWNER:
        return CREATE_RUN_UPLOAD_TOOL
    return CREATE_SYSTEM_UPLOAD_TOOL


async def create_run_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    artifacts: Sequence[ArtifactDeclaration],
    resolver: ProviderResolver,
    store: _PresignStore | None = None,
) -> ToolResponse:
    """Mint presigned PUTs for an external Run's declared build artifacts."""
    return await _create_upload(
        pool,
        ctx,
        spec=_RUN_UPLOAD,
        owner_id=run_id,
        artifacts=artifacts,
        resolver=resolver,
        store=store,
    )


async def create_system_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    artifacts: Sequence[ArtifactDeclaration],
    resolver: ProviderResolver,
    store: _PresignStore | None = None,
) -> ToolResponse:
    """Mint presigned PUTs for a DEFINED System's uploaded rootfs."""
    return await _create_upload(
        pool,
        ctx,
        spec=_SYSTEM_UPLOAD,
        owner_id=system_id,
        artifacts=artifacts,
        resolver=resolver,
        store=store,
    )
