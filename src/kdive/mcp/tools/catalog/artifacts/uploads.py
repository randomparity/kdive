"""Artifact upload-admission handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple, Protocol, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts import upload_manifest
from kdive.artifacts.read_model import RUN_ARTIFACT_NAMES, SYSTEM_ARTIFACT_NAMES
from kdive.artifacts.storage import PresignedUpload, PresignPutRequest
from kdive.artifacts.transport_encoding import (
    GZIP_ENCODING,
    IDENTITY_ENCODING,
    KNOWN_ENCODINGS,
    normalize_encoding,
)
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
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capacity.state import RunState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.profiles.provider_policy import rootfs_upload_window_allowed
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.security import audit
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
_RETENTION_CLASS = "build"

# Agent-visible PUT ergonomics, carried in the collection ``data`` so a tool-schema-only
# client sees the two presigned-PUT footguns in the response itself, not only in a resource
# doc (#1338, ADR-0395). Each presigned URL is signed (SigV4) over exactly the returned
# ``required_headers``: (1) sending ANY extra header — most often an HTTP client's implicit
# ``Content-Type`` (e.g. ``curl --data-binary``) — breaks the signature and the store answers
# ``403 SignatureDoesNotMatch``; (2) bypassing the presigned PUT with a direct upload stores
# the object without the signed ``x-amz-checksum-sha256`` integrity binding. The run
# build-artifact finalize (``runs.complete_build``) rejects an object with no stored checksum,
# so the caution is stated generically here and the run tool's docstring names that rejection.
_UPLOAD_HINT = (
    "PUT each object to its refs.upload_url sending ONLY the headers in data.required_headers "
    "and nothing else: the URL is SigV4-signed over exactly that header set, so any extra "
    "header (most often an HTTP client's implicit Content-Type, e.g. curl --data-binary) "
    "breaks the signature and the store returns 403 SignatureDoesNotMatch. Prefer "
    "`curl -T <file> -H 'Content-Type:'` plus the required_headers. The required_headers also "
    "bind this object's declared sha256 (x-amz-checksum-sha256): the bytes you PUT must match "
    "the sha256/size_bytes you declared, or the store rejects the PUT with a checksum mismatch "
    "(re-declare with the correct digest, do not retry the same bytes). Do not fall back to a "
    "direct put_object: it stores the object without the signed x-amz-checksum-sha256 "
    "integrity binding."
)

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

# The transport-encoding properties (ADR-0437/0439), advertised only on an owner whose consumer
# strips the encoding (systems/rootfs; runs rejects a non-identity encoding at declaration). Kept in
# a separate dict so ``sha256``/``size_bytes`` above stay described as the stored (transport) bytes.
_ENCODING_DECLARATION_PROPERTIES: dict[str, JsonValue] = {
    "encoding": {
        "type": "string",
        "enum": [GZIP_ENCODING, IDENTITY_ENCODING],
        "description": (
            "Optional transport encoding of the uploaded object. 'gzip' means you upload a gzip of "
            "the canonical qcow2 (kdive strips it on download to recover the qcow2); omit (or "
            "'identity') to upload the qcow2 directly. gzip is single-PUT only — it cannot be "
            "combined with chunks — and requires uncompressed_size. sha256/size_bytes still "
            "describe the uploaded (compressed) bytes."
        ),
    },
    "uncompressed_size": {
        "type": "integer",
        "description": (
            "Required with encoding='gzip': the canonical (decompressed) qcow2 size in bytes. It "
            "is the gzip-bomb bound and is checked at declaration against the 50 GiB "
            "canonical-object cap. Omit when there is no encoding."
        ),
    },
}


def _with_encoding(base: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Return a copy of ``base`` whose ``properties`` also advertise the transport-encoding fields.

    Used to build the systems declaration item schema (its rootfs consumer strips the encoding)
    without mutating the shared ``UPLOAD_DECLARATION_ITEM_SCHEMA`` runs advertises unchanged.
    """
    properties = {
        **cast("dict[str, JsonValue]", base["properties"]),
        **_ENCODING_DECLARATION_PROPERTIES,
    }
    return {**base, "properties": properties}


# The systems (rootfs) declaration item schema: the shared shape plus the transport-encoding fields
# its ADR-0438 consumer strips. Runs keeps the base schema (it rejects a non-identity encoding).
SYSTEM_UPLOAD_DECLARATION_ITEM_SCHEMA: dict[str, JsonValue] = _with_encoding(
    UPLOAD_DECLARATION_ITEM_SCHEMA
)


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
# Systems reject chunked uploads (ADR-0436) and their rootfs consumer strips a gzip transport
# encoding (ADR-0438), so the two worked examples are both single-PUT: an identity qcow2 and a gzip
# of a canonical qcow2 larger than the 5 GiB single-PUT cap (compressed under it) carrying
# encoding + uncompressed_size (ADR-0439).
SYSTEM_DECLARATION_EXAMPLES: list[JsonValue] = [
    [{"name": "rootfs", "sha256": "rL0Y20zC...base64...", "size_bytes": 12582912}],
    [
        {
            "name": "rootfs",
            "sha256": "kZ8s1f9q...base64...",
            "size_bytes": 402653184,
            "encoding": GZIP_ENCODING,
            "uncompressed_size": 6442450944,
        }
    ],
]


def _upload_ttl() -> timedelta:
    return timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))


def _max_upload_bytes() -> int:
    return config.require(MAX_UPLOAD_BYTES)


def _presign_ttl_seconds() -> int:
    return min(3600, int(_upload_ttl().total_seconds()))


def _iso_utc(when: datetime) -> str:
    """Render a (tz-aware) instant as an ISO-8601 UTC string (#1336).

    ``now()`` is a ``timestamptz`` psycopg renders in the DB session's timezone, which is
    not guaranteed UTC, so normalize to UTC before formatting rather than trusting the
    session offset. The upload deadline contract promises UTC.
    """
    return when.astimezone(UTC).isoformat()


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
    audit_object_kind: str  # the owner table for the audit_log row ("runs" / "systems")
    project: Callable[[AsyncConnection, UUID], Awaitable[str | None]]
    accepts_upload: Callable[[AsyncConnection, UUID, ProviderResolver], Awaitable[bool]]
    allow_chunks: bool = True  # False when the owner's install path reads only a single-PUT object
    # ADR-0437: only an owner with a registered decompressing consumer accepts a non-identity
    # transport ``encoding``; others reject it at declaration (no accept-then-ignore).
    accepts_encoding: bool = False
    # The canonical-object (decompressed) size ceiling this owner enforces at declaration when a
    # transport encoding is declared. Both owner caps live here so a consumer never edits the
    # validator; the compressed single-PUT size is bound separately by ``SINGLE_PUT_MAX_BYTES``.
    uncompressed_cap: int = SINGLE_PUT_MAX_BYTES


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


def _validate_encoding(
    object_id: str,
    declaration: ArtifactDeclaration,
    *,
    accepts_encoding: bool,
    uncompressed_cap: int,
    has_chunks: bool,
) -> tuple[str | None, int | None] | ToolResponse:
    """Validate the optional transport ``encoding``/``uncompressed_size`` fields (ADR-0437).

    Returns the effective ``(encoding, uncompressed_size)`` — ``(None, None)`` for an identity
    declaration (absent or ``"identity"``) — or a self-correcting rejection (ADR-0166) when the
    encoding is unknown, unsupported for the owner, combined with chunks, missing a required
    ``uncompressed_size``, over the owner's canonical-object cap, or carries a stray
    ``uncompressed_size`` with no encoding.
    """
    raw_encoding = declaration.get("encoding")
    raw_uncompressed = declaration.get("uncompressed_size")
    if raw_encoding is not None and (
        not isinstance(raw_encoding, str) or raw_encoding not in KNOWN_ENCODINGS
    ):
        return _config_error(
            object_id,
            detail=(
                "unknown transport encoding: only 'gzip' (or 'identity'/omitted for no encoding) "
                "is supported; re-declare without encoding or with encoding='gzip'"
            ),
            data={"reason": "unknown_encoding"},
        )
    encoding = normalize_encoding(raw_encoding) if isinstance(raw_encoding, str) else None
    if encoding is None:
        if raw_uncompressed is not None:
            return _config_error(
                object_id,
                detail=(
                    "uncompressed_size is only meaningful with a transport encoding: omit it, or "
                    "declare encoding='gzip' with the canonical object's size"
                ),
                data={"reason": "uncompressed_size_without_encoding"},
            )
        return None, None
    if not accepts_encoding:
        return _config_error(
            object_id,
            detail=(
                "this upload does not accept a transport encoding: re-declare without encoding and "
                "upload the canonical object directly"
            ),
            data={"reason": "encoding_not_supported"},
        )
    if has_chunks:
        return _config_error(
            object_id,
            detail=(
                "a transport-encoded upload must be a single PUT: encoding cannot be combined with "
                "chunks (omit chunks and declare a single-PUT encoded upload)"
            ),
            data={"reason": "encoding_with_chunks"},
        )
    if not isinstance(raw_uncompressed, int) or raw_uncompressed <= 0:
        return _config_error(
            object_id,
            detail=(
                "a transport encoding requires a positive integer uncompressed_size (the canonical "
                "object's size in bytes); re-declare with uncompressed_size"
            ),
            data={"reason": "uncompressed_size_required"},
        )
    if raw_uncompressed > uncompressed_cap:
        return _config_error(
            object_id,
            detail=(
                f"uncompressed_size exceeds the {uncompressed_cap}-byte canonical-object cap for "
                "this upload; the decompressed object is too large"
            ),
            data={"reason": "uncompressed_size_over_cap"},
        )
    return encoding, raw_uncompressed


def _validate_artifact_declarations(
    object_id: str,
    artifacts: Sequence[ArtifactDeclaration],
    allowed: frozenset[str],
    cap: int,
    *,
    allow_chunks: bool = True,
    accepts_encoding: bool = False,
    uncompressed_cap: int = SINGLE_PUT_MAX_BYTES,
) -> list[ManifestEntry] | ToolResponse:
    entries: list[ManifestEntry] = []
    for declaration in artifacts:
        validated_declaration = _validate_one_declaration(object_id, declaration, allowed)
        if isinstance(validated_declaration, ToolResponse):
            return validated_declaration
        name, sha256, size = validated_declaration
        artifact_cap = EFFECTIVE_CONFIG_MAX_BYTES if name == "effective_config" else cap
        raw_chunks = declaration.get("chunks")
        encoding_result = _validate_encoding(
            object_id,
            declaration,
            accepts_encoding=accepts_encoding,
            uncompressed_cap=uncompressed_cap,
            has_chunks=raw_chunks is not None,
        )
        if isinstance(encoding_result, ToolResponse):
            return encoding_result
        encoding, uncompressed_size = encoding_result
        if raw_chunks is None:
            if size <= 0 or size > min(SINGLE_PUT_MAX_BYTES, artifact_cap):
                return _config_error(object_id, data={"reason": "size_out_of_range"})
            entries.append(
                ManifestEntry(
                    name=name,
                    sha256=sha256,
                    size_bytes=size,
                    encoding=encoding,
                    uncompressed_size=uncompressed_size,
                )
            )
            continue
        if not allow_chunks:
            # ADR-0436: a chunked (multipart) upload reassembles into an object whose only stored
            # checksum is the composite "<base64>-<N>", but the System rootfs install path verifies
            # sha256(body) == head.checksum_sha256 (plain SHA-256, ADR-0434), which a composite can
            # never satisfy. Reject at declaration with an actionable message rather than mint part
            # URLs that dead-end at provision ("upload-kind rootfs was never uploaded").
            return _config_error(
                object_id,
                detail=(
                    "System rootfs must be a single PUT <= 5 GiB; chunked/multipart upload is not "
                    "supported (omit chunks and declare a single-PUT upload)"
                ),
                data={"reason": "chunking_not_supported"},
            )
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
    if len(raw_chunks) > 1 and declared_total <= SINGLE_PUT_MAX_BYTES:
        # Chunking is a *size* mechanism for objects above the 5 GiB single-object cap, not a
        # remedy for a short presign window: a single PUT always succeeds below the cap, so a
        # multi-part declaration here is pure added failure surface (the parts upload but the
        # manifest reassembly then fails) and almost always a client mistake. Reject it loudly
        # with an actionable nudge to the single-PUT path rather than mint part URLs that dead-end.
        return _config_error(
            object_id,
            detail=(
                "chunked upload declared for an object that fits a single PUT: a single PUT "
                "always succeeds below the 5 GiB single-object cap, so omit chunks and declare "
                "a single-PUT upload instead of a multi-part upload"
            ),
            data={"reason": "chunking_not_needed"},
        )
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
    return run is not None and run.state is RunState.CREATED


async def _system_accepts_upload(
    conn: AsyncConnection, owner_id: UUID, resolver: ProviderResolver
) -> bool:
    system = await SYSTEMS.get(conn, owner_id)
    if system is None or system.state is not SystemState.DEFINED:
        return False
    parsed = ProvisioningProfile.parse(system.provisioning_profile)
    runtime = await resolver.runtime_for_system(conn, owner_id)
    return rootfs_upload_window_allowed(runtime.profile_policy, parsed)


# Canonical-object (decompressed) size ceilings, enforced at declaration when a transport encoding
# is declared (ADR-0437). Systems (rootfs) is the only owner with a decompressing consumer (Sub 2,
# #1510); its cap matches the ``KDIVE_MAX_UPLOAD_BYTES`` per-artifact ceiling (50 GiB) — the
# canonical object is bound by the same ceiling whether it arrives raw or transport-gzipped. Runs
# has no decompressing consumer and rejects a non-identity encoding outright, so its cap (the 5 GiB
# single-PUT ceiling) only ever gates a future opt-in.
_SYSTEM_UNCOMPRESSED_CAP = 50 * 1024 * 1024 * 1024
_RUN_UNCOMPRESSED_CAP = SINGLE_PUT_MAX_BYTES

_RUN_UPLOAD = _UploadOwnerSpec(
    owner_kind=upload_manifest.RUN_UPLOAD_OWNER,
    required_role=Role.CONTRIBUTOR,
    lock_scope=LockScope.RUN,
    allowed_names=RUN_ARTIFACT_NAMES,
    next_action="runs.complete_build",
    audit_object_kind="runs",
    project=_run_project,
    accepts_upload=_run_accepts_upload,
    accepts_encoding=False,  # no decompressing consumer for build artifacts (ADR-0437)
    uncompressed_cap=_RUN_UNCOMPRESSED_CAP,
)
_SYSTEM_UPLOAD = _UploadOwnerSpec(
    owner_kind=upload_manifest.SYSTEM_UPLOAD_OWNER,
    required_role=Role.CONTRIBUTOR,
    lock_scope=LockScope.SYSTEM,
    allowed_names=SYSTEM_ARTIFACT_NAMES,
    next_action="systems.provision_defined",
    audit_object_kind="systems",
    project=_system_project,
    accepts_upload=_system_accepts_upload,
    allow_chunks=False,  # #743 install verifies plain SHA-256; a composite can't (ADR-0436)
    accepts_encoding=True,  # rootfs consumer strips gzip on download (Sub 2, #1510; ADR-0437)
    uncompressed_cap=_SYSTEM_UNCOMPRESSED_CAP,
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
                owner_id,
                artifacts,
                spec.allowed_names,
                _max_upload_bytes(),
                allow_chunks=spec.allow_chunks,
                accepts_encoding=spec.accepts_encoding,
                uncompressed_cap=spec.uncompressed_cap,
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
                    stamp = await upload_manifest.replace_manifest(
                        conn,
                        upload_manifest.UploadManifestReplaceRequest(
                            owner_kind=spec.owner_kind,
                            owner_id=uid,
                            prefix=prefix,
                            entries=entries,
                            ttl=_upload_ttl(),
                        ),
                    )
                    # Minting presigned PUTs replaces the durable manifest and grants write
                    # access to object-store keys; audit it inside the same transaction (ADR-0028)
                    # so the grant and its attribution row commit together, symmetric with
                    # artifacts.fetch_raw auditing its presigned GET.
                    await audit.record(
                        conn,
                        ctx,
                        audit.AuditEvent(
                            tool=_upload_tool_name(spec),
                            object_kind=spec.audit_object_kind,
                            object_id=uid,
                            transition="create_upload",
                            args={"owner_id": owner_id, "artifacts": [e.name for e in entries]},
                            project=project,
                        ),
                    )
            except CategorizedError as exc:
                _log.warning("create_upload failed for %s %s: %s", spec.owner_kind, owner_id, exc)
                return ToolResponse.failure_from_error(owner_id, exc)

    # The presigned URL expiry (per item) is the "begin the PUT by" wall; it is
    # server_time + presign_ttl, which clamps below the manifest deadline when
    # UPLOAD_TTL_SECONDS > 3600. The manifest deadline (collection) is the reaper's
    # reclaim window for the whole upload (#1336, ADR-0394).
    #
    # expires_at is rendered in the DB clock frame (server_time is the transaction's
    # now(), which precedes the boto3 signing instant), while the object store enforces
    # the URL on its own clock. Any DB-ahead-of-store skew or lock-wait between the two
    # only understates expires_at, so the failure direction is a needless re-mint, never
    # trusting a lapsed URL; manifest_deadline is the authoritative reaper-enforced wall.
    url_expires_at = _iso_utc(stamp.server_time + timedelta(seconds=_presign_ttl_seconds()))
    items = [
        _upload_response(upload, next_action=spec.next_action, expires_at=url_expires_at)
        for upload in uploads
    ]
    return ToolResponse.collection(
        owner_id,
        "upload_ready",
        items,
        suggested_next_actions=[spec.next_action],
        data={
            "owner_kind": spec.owner_kind,
            "manifest_mode": "replace",
            "replaces_prior_manifest": True,
            "server_time": _iso_utc(stamp.server_time),
            "manifest_deadline": _iso_utc(stamp.deadline),
            "upload_hint": _UPLOAD_HINT,
            "on_expiry": {
                "tool": _upload_tool_name(spec),
                "effect": "re-mint replaces the manifest and resets the deadline",
            },
        },
    )


def _upload_response(
    upload: _MaterializedUpload, *, next_action: str, expires_at: str
) -> ToolResponse:
    return ToolResponse.success(
        upload.key,
        "upload_ready",
        suggested_next_actions=[next_action],
        refs={"upload_url": upload.presigned.url},
        data={
            "name": upload.entry.name,
            "artifact_name": upload.entry.name,
            "expires_in": _presign_ttl_seconds(),
            "expires_at": expires_at,
            **({"part_number": upload.part_number} if upload.part_number is not None else {}),
            "required_headers": dict(upload.presigned.required_headers),
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
