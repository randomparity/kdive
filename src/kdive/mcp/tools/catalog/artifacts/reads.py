"""Redacted-only artifact list/get/search handlers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, LiteralString, NamedTuple, Protocol

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

import kdive.config as config
from kdive.artifacts.storage import FetchedArtifact, HeadResult
from kdive.config.core_settings import (
    ARTIFACT_DOWNLOAD_TTL_SECONDS,
    ARTIFACT_INLINE_MAX_BYTES,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.security.artifacts.artifact_search import (
    AFTER_LINES_RANGE,
    BEFORE_LINES_RANGE,
    MAX_MATCHES_RANGE,
    ArtifactSearchInputError,
    parse_literal_terms,
    search_text,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.artifacts.listing import RedactedArtifact, list_redacted_system_artifacts
from kdive.store.objectstore import (
    object_store_from_env,
)

_log = logging.getLogger(__name__)

_MAX_SEARCHABLE_ARTIFACT_BYTES = 1024 * 1024
_GET_SQL: LiteralString = (
    "SELECT id, object_key, owner_id FROM artifacts "
    "WHERE id = %s AND owner_kind = 'systems' AND sensitivity = %s"
)
_PROJECT_SQL: LiteralString = "SELECT project FROM systems WHERE id = %s"


class _SearchStore(Protocol):
    def head(self, key: str) -> HeadResult | None: ...
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...
    def presign_get(self, key: str, *, expires_in: int) -> str: ...


class _AuthorizedArtifact(NamedTuple):
    key: str


class ArtifactSearchRequest(BaseModel):
    """Request fields for bounded literal search in one redacted artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: Annotated[str, Field(description="The redacted System artifact id.")]
    pattern: Annotated[
        str,
        Field(
            description=(
                "Literal alternation pattern; '|' separates terms (grep-style), "
                "e.g. '__d_lookup|panic'. The word 'OR' is not special."
            )
        ),
    ]
    before_lines: Annotated[
        int,
        Field(
            ge=BEFORE_LINES_RANGE[0],
            le=BEFORE_LINES_RANGE[1],
            description=(
                f"Context lines before each match "
                f"({BEFORE_LINES_RANGE[0]}–{BEFORE_LINES_RANGE[1]})."
            ),
        ),
    ] = 2
    after_lines: Annotated[
        int,
        Field(
            ge=AFTER_LINES_RANGE[0],
            le=AFTER_LINES_RANGE[1],
            description=(
                f"Context lines after each match ({AFTER_LINES_RANGE[0]}–{AFTER_LINES_RANGE[1]})."
            ),
        ),
    ] = 4
    max_matches: Annotated[
        int,
        Field(
            ge=MAX_MATCHES_RANGE[0],
            le=MAX_MATCHES_RANGE[1],
            description=(
                f"Maximum match windows to return ({MAX_MATCHES_RANGE[0]}–{MAX_MATCHES_RANGE[1]})."
            ),
        ),
    ] = 20


@dataclass(frozen=True, slots=True)
class ArtifactReadHandlers:
    """Artifact read handlers with the object-store search seam bound at construction."""

    search_store_factory: Callable[[], _SearchStore] = object_store_from_env

    async def artifacts_search_text(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        request: ArtifactSearchRequest,
    ) -> ToolResponse:
        try:
            store = self.search_store_factory()
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                request.artifact_id, exc, suggested_next_actions=["artifacts.search_text"]
            )
        return await _artifacts_search_text(
            pool,
            ctx,
            request=request,
            store=store,
        )


async def _authorized_redacted_artifact(
    pool: AsyncConnectionPool, ctx: RequestContext, *, artifact_id: str
) -> _AuthorizedArtifact | ToolResponse:
    uid = _as_uuid(artifact_id)
    if uid is None:
        return _config_error(artifact_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_GET_SQL, (uid, Sensitivity.REDACTED.value))
            row = await cur.fetchone()
            if row is None:
                return _not_found(artifact_id)
            await cur.execute(_PROJECT_SQL, (row["owner_id"],))
            owner = await cur.fetchone()
        if owner is None or owner["project"] not in ctx.projects:
            return _not_found(artifact_id)
        require_role(ctx, owner["project"], Role.VIEWER)
        return _AuthorizedArtifact(key=str(row["object_key"]))


async def artifacts_list(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> ToolResponse:
    """Return the System's `redacted` artifacts in one collection envelope.

    A System's artifact set is naturally bounded, so this carries the uniform pagination
    keys (ADR-0192) — ``data.total`` (the cheap row count) and ``data.truncated`` (always
    ``false``: the whole set is returned) — but takes no ``cursor`` and runs no keyset query.
    """
    items = _artifact_list_items(
        await list_redacted_system_artifacts(pool, ctx, system_id=system_id)
    )
    return ToolResponse.collection(
        system_id,
        "ok",
        items,
        suggested_next_actions=["artifacts.get"],
        data={"truncated": False, "total": len(items)},
    )


def _artifact_list_items(artifacts: list[RedactedArtifact]) -> list[ToolResponse]:
    """Return redacted artifact item envelopes."""
    responses: list[ToolResponse] = []
    for artifact in artifacts:
        try:
            responses.append(
                ToolResponse.success(
                    artifact.id,
                    "available",
                    suggested_next_actions=["artifacts.get"],
                    refs={"object": artifact.object_key},
                )
            )
        except ValueError:
            _log.warning("artifact %s violates the envelope invariant; degraded", artifact.id)
    return responses


async def artifacts_get(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    artifact_id: str,
    store_factory: Callable[[], _SearchStore] = object_store_from_env,
) -> ToolResponse:
    """Return one `redacted` artifact's content, or a not-found-shaped config error.

    On success the envelope carries the object ref plus, best-effort, the redacted
    bytes inline (`data["content"]`, capped at ``KDIVE_ARTIFACT_INLINE_MAX_BYTES``)
    and a presigned download URL (`refs["download_uri"]`). A store outage degrades
    the content/URI enrichment to a ``data["content_unavailable"]`` reason; the
    metadata envelope still returns (ADR-0140).
    """
    authorized = await _authorized_redacted_artifact(pool, ctx, artifact_id=artifact_id)
    if isinstance(authorized, ToolResponse):
        return authorized
    refs: dict[str, str] = {"object": authorized.key}
    data = await _artifact_content(authorized.key, store_factory, refs)
    if data is None:  # fetched object's sensitivity is not REDACTED (the redaction gate)
        return _config_error(artifact_id)
    return ToolResponse.success(
        artifact_id,
        "available",
        suggested_next_actions=["artifacts.get"],
        refs=refs,
        data=data,
    )


async def _artifact_content(
    key: str,
    store_factory: Callable[[], _SearchStore],
    refs: dict[str, str],
) -> dict[str, str] | None:
    """Enrich ``refs`` with a download URI and return the inline-content data fields.

    Best-effort: any store failure yields a ``content_unavailable`` reason and leaves
    ``refs`` without a ``download_uri`` rather than failing the tool. Returns ``None``
    when the fetched object's sensitivity is not `REDACTED` (the caller maps that to a
    not-found-shaped config error — the same redaction gate `artifacts_search_text`
    applies).
    """
    try:
        store = store_factory()
    except CategorizedError:
        return {"content_unavailable": "store_unconfigured"}
    inline_cap = config.require(ARTIFACT_INLINE_MAX_BYTES)
    ttl = config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS)
    try:
        head = await asyncio.to_thread(store.head, key)
        if head is None:
            return {"content_unavailable": "store_error"}
        # The redaction gate, enforced before the URI is minted so it covers every size:
        # a sensitive object at a redacted row's key is not-found-shaped (DB/object drift).
        if head.sensitivity is not Sensitivity.REDACTED:
            return None
        refs["download_uri"] = await asyncio.to_thread(store.presign_get, key, expires_in=ttl)
        if head.size_bytes > inline_cap:
            return {"size_bytes": str(head.size_bytes), "content_omitted": "artifact_too_large"}
        fetched = await asyncio.to_thread(store.get_artifact, key, head.etag)
    except CategorizedError:
        refs.pop("download_uri", None)
        return {"content_unavailable": "store_error"}
    if fetched.sensitivity is not Sensitivity.REDACTED:
        return None
    return {
        "size_bytes": str(head.size_bytes),
        "content": fetched.data.decode("utf-8", errors="replace"),
        "content_truncated": "false",
    }


async def _artifacts_search_text(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    request: ArtifactSearchRequest,
    store: _SearchStore,
) -> ToolResponse:
    """Search one redacted System-owned text artifact with bounded literal context."""
    artifact_id = request.artifact_id
    authorized = await _authorized_redacted_artifact(pool, ctx, artifact_id=artifact_id)
    if isinstance(authorized, ToolResponse):
        return authorized
    try:
        parse_literal_terms(request.pattern)
    except ArtifactSearchInputError:
        return _config_error(artifact_id, data={"reason": "bad_search_input"})
    key = authorized.key
    try:
        head = await asyncio.to_thread(store.head, key)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(artifact_id, exc)
    if head is None:
        return _config_error(artifact_id)
    if head.size_bytes > _MAX_SEARCHABLE_ARTIFACT_BYTES:
        return _config_error(
            artifact_id,
            data={"reason": "artifact_too_large", "size_bytes": str(head.size_bytes)},
        )
    try:
        fetched = await asyncio.to_thread(store.get_artifact, key, head.etag)
        if fetched.sensitivity is not Sensitivity.REDACTED:
            return _config_error(artifact_id)
        result = search_text(
            fetched.data,
            pattern=request.pattern,
            before_lines=request.before_lines,
            after_lines=request.after_lines,
            max_matches=request.max_matches,
        )
    except ArtifactSearchInputError:
        return _config_error(artifact_id, data={"reason": "bad_search_input"})
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(artifact_id, exc)
    return ToolResponse.success(
        artifact_id,
        "searched",
        suggested_next_actions=["artifacts.search_text", "runs.get"],
        refs={"artifact": key},
        data={
            "match_count": str(result.match_count),
            "truncated": str(result.truncated).lower(),
            "matches_json": result.matches_json(),
        },
    )
