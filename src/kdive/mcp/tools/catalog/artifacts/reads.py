"""Redacted-only artifact list/get/search handlers."""

from __future__ import annotations

import asyncio
import gzip
import logging
import zlib
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
from kdive.security.artifacts.artifact_jump import JumpDirection, jump_find, resolve_anchor
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
from kdive.serialization import JsonValue
from kdive.services.artifacts.listing import RedactedArtifact, list_redacted_system_artifacts
from kdive.store.objectstore import (
    object_store_from_env,
)

_log = logging.getLogger(__name__)

_MAX_SEARCHABLE_ARTIFACT_BYTES = 1024 * 1024
# The largest object `artifacts.get` pulls whole into memory to slice a window from.
# Intentionally equal to `_MAX_SEARCHABLE_ARTIFACT_BYTES` (both byte-reading read tools
# share the 1 MiB in-memory ceiling), but a distinct constant so the two can diverge
# later without surprise (ADR-0247). Larger objects omit inline content; the
# always-present `refs.download_uri` serves them.
_MAX_WINDOWED_FETCH_BYTES = 1024 * 1024
# The default inline window `artifacts.get` returns when the caller names no `max_bytes`
# (ADR-0247): 16 KiB ≈ 4k–5k tokens, sized to the tool-result token budget rather than the
# 64 KiB `KDIVE_ARTIFACT_INLINE_MAX_BYTES` byte cap (which still bounds the per-call window).
ARTIFACT_GET_WINDOW_DEFAULT_BYTES = 16 * 1024
# Hard, non-configurable token-safe ceiling on a single `artifacts.get` window
# (ADR-0257, #835). The MCP client bounds a tool-result in tokens (~25k); the server
# bounds the window in bytes. The REDACTED artifacts served inline are line-oriented
# text (console, redacted dmesg, build-log), so JSON escaping stays near 1:1 and
# 24 KiB is <= ~8.3k tokens worst case (ADR-0247: 64 KiB ~ 16k–22k tokens, i.e.
# <= ~0.336 tokens/byte) — about a third of the ceiling, leaving room for the rest
# of the envelope. Unlike `KDIVE_ARTIFACT_INLINE_MAX_BYTES` this is not
# operator-tunable, so the token-safety bound holds regardless of the caller's
# `max_bytes` or the configured inline cap (which can only lower the window further).
ARTIFACT_GET_WINDOW_MAX_BYTES = 24 * 1024
_GET_SQL: LiteralString = (
    "SELECT id, object_key, owner_kind, owner_id FROM artifacts "
    "WHERE id = %s AND owner_kind IN ('systems', 'runs') AND sensitivity = %s"
)
# The owning row's project, keyed by the artifact's owner_kind. A System-owned artifact (console,
# vmcore) resolves through `systems`; a Run-owned build-log (ADR-0238) resolves through `runs`,
# which carries the build's project even when no System is bound yet (system_id nullable, ADR-0169).
_PROJECT_SQL_BY_OWNER_KIND: dict[str, LiteralString] = {
    "systems": "SELECT project FROM systems WHERE id = %s",
    "runs": "SELECT project FROM runs WHERE id = %s",
}


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
            project_sql = _PROJECT_SQL_BY_OWNER_KIND.get(str(row["owner_kind"]))
            if project_sql is None:
                return _not_found(artifact_id)
            await cur.execute(project_sql, (row["owner_id"],))
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
    byte_offset: int = 0,
    max_bytes: int = ARTIFACT_GET_WINDOW_DEFAULT_BYTES,
    find: str | None = None,
    direction: JumpDirection = "forward",
    store_factory: Callable[[], _SearchStore] = object_store_from_env,
) -> ToolResponse:
    """Return one `redacted` artifact's content window, or jump to a literal match.

    Without ``find`` this returns a byte window: ``data[byte_offset : byte_offset +
    effective_max]`` where ``effective_max = min(max_bytes, KDIVE_ARTIFACT_INLINE_MAX_BYTES,
    ARTIFACT_GET_WINDOW_MAX_BYTES)`` (the last a hard 24 KiB token-safe ceiling, ADR-0257);
    ``data["content_truncated"]``/``data["next_offset"]`` page the rest. ``direction`` pages
    ``forward`` (default; ``byte_offset`` from the start) or ``backward`` (``byte_offset`` from
    end-of-artifact, the tail), so a caller can read the end and walk up.

    With ``find`` the call jumps to the nearest literal ``|``-OR match in ``direction`` over the
    whole body (#939): ``data["match_found"]`` plus, on a hit, ``data["match_offset"]``,
    ``data["match_line"]``, the surrounding ``data["content"]`` window, and a direction-relative
    ``data["next_offset"]`` to continue. Matching is byte-space literal (no regex, no Unicode
    normalization). An artifact larger than ``_MAX_WINDOWED_FETCH_BYTES`` cannot be searched (its
    bytes are never fetched), so ``find`` rejects it with ``configuration_error``
    ``reason=artifact_too_large`` rather than a misleading ``match_found=false``.

    A negative ``byte_offset`` reads from the direction's natural edge and ``max_bytes <= 0``
    floors to a 1-byte window (clamped, never rejected). Objects larger than
    ``_MAX_WINDOWED_FETCH_BYTES`` omit inline content (``content_omitted``) on a plain read and
    are retrieved via ``refs["download_uri"]``. A store outage degrades to a
    ``data["content_unavailable"]`` reason; the metadata envelope still returns (ADR-0140,
    ADR-0247). Missing or unauthorized rows return ``not_found``. A visible redacted row whose
    object metadata or fetched object is no longer redacted is redaction drift and returns
    ``configuration_error``.
    """
    authorized = await _authorized_redacted_artifact(pool, ctx, artifact_id=artifact_id)
    if isinstance(authorized, ToolResponse):
        return authorized
    terms: tuple[str, ...] | None = None
    if find is not None:
        try:
            terms = parse_literal_terms(find)
        except ArtifactSearchInputError:
            return _config_error(artifact_id, data={"reason": "bad_search_input"})
    refs: dict[str, str] = {"object": authorized.key}
    loaded = await _load_redacted_plaintext(authorized.key, store_factory, refs)
    if loaded.drift:  # head/fetched object's sensitivity is not REDACTED (the redaction gate)
        return _config_error(artifact_id)
    if terms is not None:
        find_data = _find_response_data(
            loaded,
            terms=terms,
            direction=direction,
            byte_offset=byte_offset,
            max_bytes=max_bytes,
            artifact_id=artifact_id,
        )
        if isinstance(find_data, ToolResponse):
            return find_data
        data = find_data
    else:
        data = _window_response_data(
            loaded, byte_offset=byte_offset, max_bytes=max_bytes, direction=direction
        )
    return ToolResponse.success(
        artifact_id,
        "available",
        suggested_next_actions=["artifacts.get"],
        refs=refs,
        data=data,
    )


@dataclass(frozen=True, slots=True)
class _LoadedBody:
    """The redacted plaintext body, or a degraded/redaction-drift state, from one fetch."""

    body: bytes | None
    size_bytes: int | None
    degraded: dict[str, JsonValue] | None
    drift: bool


def _effective_max(max_bytes: int) -> int:
    """Clamp a requested window to the inline cap and the hard token-safe ceiling (ADR-0247)."""
    inline_cap = config.require(ARTIFACT_INLINE_MAX_BYTES)
    return min(max(max_bytes, 1), inline_cap, ARTIFACT_GET_WINDOW_MAX_BYTES)


async def _load_redacted_plaintext(
    key: str, store_factory: Callable[[], _SearchStore], refs: dict[str, str]
) -> _LoadedBody:
    """Fetch the redacted object, enrich ``refs`` with a download URI, return its plaintext.

    Shared by the plain-window and ``find`` paths so the redaction gate lives in one place.
    Best-effort: a store failure yields a ``content_unavailable`` degrade and leaves ``refs``
    without a ``download_uri``. ``drift=True`` marks a non-REDACTED head/fetched object (the
    caller maps it to ``configuration_error``). A ``gzip`` object is inflated so the body and
    all offsets describe the plaintext; a corrupt body degrades to ``decode_error``. Detection
    is strictly metadata-driven; the object key is never inspected. Inflation is bounded by
    construction (only the console-part path writes gzip, at <= 64 KiB plaintext per part), so a
    decompression bomb is out of the threat model.
    """
    try:
        store = store_factory()
    except CategorizedError:
        return _LoadedBody(None, None, {"content_unavailable": "store_unconfigured"}, False)
    ttl = config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS)
    try:
        head = await asyncio.to_thread(store.head, key)
        if head is None:
            return _LoadedBody(None, None, {"content_unavailable": "store_error"}, False)
        # The redaction gate, enforced before the URI is minted so it covers every size.
        if head.sensitivity is not Sensitivity.REDACTED:
            return _LoadedBody(None, None, None, True)
        refs["download_uri"] = await asyncio.to_thread(store.presign_get, key, expires_in=ttl)
        if head.size_bytes > _MAX_WINDOWED_FETCH_BYTES:
            return _LoadedBody(
                None,
                head.size_bytes,
                {"size_bytes": head.size_bytes, "content_omitted": "artifact_too_large"},
                False,
            )
        fetched = await asyncio.to_thread(store.get_artifact, key, head.etag)
    except CategorizedError:
        refs.pop("download_uri", None)
        return _LoadedBody(None, None, {"content_unavailable": "store_error"}, False)
    if fetched.sensitivity is not Sensitivity.REDACTED:
        return _LoadedBody(None, None, None, True)
    if head.content_encoding == "gzip":
        try:
            body = gzip.decompress(fetched.data)
        except gzip.BadGzipFile, EOFError, zlib.error:
            return _LoadedBody(None, None, {"content_unavailable": "decode_error"}, False)
    else:
        body = fetched.data
    return _LoadedBody(body, len(body), None, False)


def _window_response_data(
    loaded: _LoadedBody, *, byte_offset: int, max_bytes: int, direction: JumpDirection
) -> dict[str, JsonValue]:
    """Build the plain windowed-read ``data`` (forward = today's behavior; backward = tail)."""
    if loaded.body is None:
        return loaded.degraded or {}
    body = loaded.body
    size_bytes = len(body)
    effective_max = _effective_max(max_bytes)
    if direction == "backward":
        end = resolve_anchor(size_bytes, direction="backward", byte_offset=byte_offset)
        window_start = max(0, end - effective_max)
        window = body[window_start:end]
        data: dict[str, JsonValue] = {
            "size_bytes": size_bytes,
            "content": window.decode("utf-8", errors="replace"),
            "content_truncated": window_start > 0,
        }
        if window_start > 0:
            data["next_offset"] = window_start
        return data
    byte_offset = max(byte_offset, 0)
    window = body[byte_offset : byte_offset + effective_max]
    next_offset = byte_offset + len(window)
    # Truncation requires forward progress so a paging caller never loops on a stuck cursor.
    truncated = len(window) > 0 and next_offset < size_bytes
    data = {
        "size_bytes": size_bytes,
        "content": window.decode("utf-8", errors="replace"),
        "content_truncated": truncated,
    }
    if truncated:
        data["next_offset"] = next_offset
    return data


def _find_response_data(
    loaded: _LoadedBody,
    *,
    terms: tuple[str, ...],
    direction: JumpDirection,
    byte_offset: int,
    max_bytes: int,
    artifact_id: str,
) -> dict[str, JsonValue] | ToolResponse:
    """Build the ``find`` ``data``, or reject an oversized artifact that cannot be searched."""
    if loaded.body is None:
        if loaded.degraded is not None and (
            loaded.degraded.get("content_omitted") == "artifact_too_large"
        ):
            return _config_error(
                artifact_id,
                data={"reason": "artifact_too_large", "size_bytes": loaded.size_bytes},
            )
        # Store outage: degrade honestly rather than claim "no match".
        return {**(loaded.degraded or {}), "match_found": False}
    hit = jump_find(
        loaded.body,
        terms=terms,
        direction=direction,
        byte_offset=byte_offset,
        max_bytes=_effective_max(max_bytes),
    )
    if hit is None:
        return {"size_bytes": len(loaded.body), "match_found": False}
    data: dict[str, JsonValue] = {
        "size_bytes": len(loaded.body),
        "match_found": True,
        "match_offset": hit.match_offset,
        "match_line": hit.match_line,
        "content": hit.content.decode("utf-8", errors="replace"),
    }
    if hit.next_offset is not None:
        data["next_offset"] = hit.next_offset
    return data


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
            data={"reason": "artifact_too_large", "size_bytes": head.size_bytes},
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
            "match_count": result.match_count,
            "truncated": result.truncated,
            "matches_json": result.matches_json(),
        },
    )
