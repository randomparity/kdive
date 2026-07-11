"""Redacted-only artifact list/get handlers."""

from __future__ import annotations

import asyncio
import gzip
import logging
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import LiteralString, NamedTuple, Protocol

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

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
from kdive.mcp.schema.tool_payloads import ToolPayload
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.security.artifacts.artifact_jump import JumpDirection, jump_find, resolve_anchor
from kdive.security.artifacts.artifact_search import (
    ArtifactSearchInputError,
    parse_literal_terms,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.serialization import JsonValue
from kdive.services.artifacts.listing import RedactedArtifact, list_redacted_system_artifacts
from kdive.store.objectstore import (
    object_store_from_env,
)

_log = logging.getLogger(__name__)

# The largest object pulled whole into memory for `artifacts.get` windows or `artifacts.find`
# searches (ADR-0247, ADR-0283). Larger objects omit inline content from `artifacts.get` and
# reject `artifacts.find` (`artifact_too_large`); the always-present `refs.download_uri` serves
# them.
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

_ARTIFACT_ID_DESCRIPTION = "The redacted artifact to fetch (sensitive ids are not-found)."
_BYTE_OFFSET_DESCRIPTION = (
    "Start byte of the window (0-based). With direction=forward (default) a negative value "
    "reads from the start; with direction=backward the window runs up from this byte and an "
    "omitted (0) or negative value means end-of-artifact. Page with the returned "
    "data.next_offset until data.content_truncated is false."
)
_MAX_BYTES_DESCRIPTION = (
    f"Maximum inline window bytes; default {ARTIFACT_GET_WINDOW_DEFAULT_BYTES}, sized to the "
    "tool-result token budget. The server caps the window at the smaller of a hard "
    f"{ARTIFACT_GET_WINDOW_MAX_BYTES}-byte token-safe ceiling and "
    "KDIVE_ARTIFACT_INLINE_MAX_BYTES, so a larger value still returns at most "
    f"{ARTIFACT_GET_WINDOW_MAX_BYTES} bytes with data.next_offset to page the rest; an "
    "artifact above the fetch ceiling omits inline content — use refs.download_uri for the "
    "whole object when present. Store or redaction failures set data.content_unavailable and "
    "omit download_uri."
)
_GET_DIRECTION_DESCRIPTION = (
    "Cursor direction for paging. forward starts at byte_offset (the artifact start when "
    "omitted). backward starts at end-of-artifact when byte_offset is omitted (read the tail "
    "and page up); a positive byte_offset bounds it."
)
_QUERY_DESCRIPTION = (
    "Literal search terms. '|' separates alternatives (e.g. 'BUG: KASAN|Call Trace'); the "
    "nearest term in direction is returned with data.match_offset, data.match_line, the "
    "surrounding data.content, and data.next_offset to continue (data.match_found is false "
    "when none remain). Per-line literal substring, case-sensitive, no regex and no Unicode "
    "normalization — match the artifact's exact bytes (kernel signatures are ASCII)."
)
_FIND_MAX_BYTES_DESCRIPTION = (
    f"Maximum surrounding content bytes on a match; default {ARTIFACT_GET_WINDOW_DEFAULT_BYTES}. "
    "The server caps this at the smaller of a hard "
    f"{ARTIFACT_GET_WINDOW_MAX_BYTES}-byte token-safe ceiling and "
    "KDIVE_ARTIFACT_INLINE_MAX_BYTES."
)
_FIND_DIRECTION_DESCRIPTION = (
    "Search direction. forward starts at byte_offset (the artifact start when omitted). "
    "backward starts at end-of-artifact when byte_offset is omitted; a positive byte_offset "
    "bounds it."
)


class ArtifactsGetRequest(ToolPayload):
    """Request payload for ``artifacts.get`` byte-window reads."""

    artifact_id: str = Field(description=_ARTIFACT_ID_DESCRIPTION)
    byte_offset: int = Field(default=0, description=_BYTE_OFFSET_DESCRIPTION)
    max_bytes: int = Field(
        default=ARTIFACT_GET_WINDOW_DEFAULT_BYTES,
        description=_MAX_BYTES_DESCRIPTION,
    )
    direction: JumpDirection = Field(default="forward", description=_GET_DIRECTION_DESCRIPTION)


class ArtifactsFindRequest(ToolPayload):
    """Request payload for ``artifacts.find`` literal searches."""

    artifact_id: str = Field(
        description="The redacted artifact to search (sensitive ids are not-found)."
    )
    query: str = Field(description=_QUERY_DESCRIPTION)
    byte_offset: int = Field(
        default=0,
        description=(
            "Start byte for the search cursor. With direction=forward (default), search starts "
            "at this byte and a negative value starts from the beginning. With "
            "direction=backward, search runs up from this byte and an omitted (0) or negative "
            "value means end-of-artifact."
        ),
    )
    max_bytes: int = Field(
        default=ARTIFACT_GET_WINDOW_DEFAULT_BYTES,
        description=_FIND_MAX_BYTES_DESCRIPTION,
    )
    direction: JumpDirection = Field(default="forward", description=_FIND_DIRECTION_DESCRIPTION)


class _SearchStore(Protocol):
    def head(self, key: str) -> HeadResult | None: ...
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...
    def presign_get(self, key: str, *, expires_in: int) -> str: ...


class _AuthorizedArtifact(NamedTuple):
    key: str


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
    request: ArtifactsGetRequest,
    store_factory: Callable[[], _SearchStore] = object_store_from_env,
) -> ToolResponse:
    """Return one `redacted` artifact's content window.

    This returns a byte window: ``data[byte_offset : byte_offset + effective_max]`` where
    ``effective_max = min(max_bytes, KDIVE_ARTIFACT_INLINE_MAX_BYTES,
    ARTIFACT_GET_WINDOW_MAX_BYTES)`` (the last a hard 24 KiB token-safe ceiling, ADR-0257);
    ``data["content_truncated"]``/``data["next_offset"]`` page the rest. ``direction`` pages
    ``forward`` (default; ``byte_offset`` from the start) or ``backward`` (``byte_offset`` from
    end-of-artifact, the tail), so a caller can read the end and walk up.

    A negative ``byte_offset`` reads from the direction's natural edge and ``max_bytes <= 0``
    floors to a 1-byte window (clamped, never rejected). Objects larger than
    ``_MAX_WINDOWED_FETCH_BYTES`` omit inline content (``content_omitted``) and are retrieved via
    ``refs["download_uri"]``. A store outage degrades to a ``data["content_unavailable"]`` reason;
    the metadata envelope still returns (ADR-0140, ADR-0247). Missing or unauthorized rows return
    ``not_found``. A visible redacted row whose object metadata or fetched object is no longer
    redacted is redaction drift and returns ``configuration_error``.
    """
    authorized = await _authorized_redacted_artifact(pool, ctx, artifact_id=request.artifact_id)
    if isinstance(authorized, ToolResponse):
        return authorized
    refs: dict[str, str] = {"object": authorized.key}
    loaded = await _load_redacted_plaintext(authorized.key, store_factory, refs)
    if loaded.drift:  # head/fetched object's sensitivity is not REDACTED (the redaction gate)
        return _config_error(request.artifact_id)
    data = _window_response_data(
        loaded,
        byte_offset=request.byte_offset,
        max_bytes=request.max_bytes,
        direction=request.direction,
    )
    return ToolResponse.success(
        request.artifact_id,
        "available",
        suggested_next_actions=["artifacts.get"],
        refs=refs,
        data=data,
    )


async def artifacts_find(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    request: ArtifactsFindRequest,
    store_factory: Callable[[], _SearchStore] = object_store_from_env,
) -> ToolResponse:
    """Jump to a literal match in one `redacted` artifact.

    The call jumps to the nearest literal ``|``-OR match in ``direction`` over the whole body
    (#939): ``data["match_found"]`` plus, on a hit, ``data["match_offset"]``,
    ``data["match_line"]``, the surrounding ``data["content"]`` window, and a direction-relative
    ``data["next_offset"]`` to continue. Matching is byte-space literal (no regex, no Unicode
    normalization). An artifact larger than ``_MAX_WINDOWED_FETCH_BYTES`` cannot be searched (its
    bytes are never fetched), so this rejects it with ``configuration_error``
    ``reason=artifact_too_large`` rather than a misleading ``match_found=false``.
    """
    authorized = await _authorized_redacted_artifact(pool, ctx, artifact_id=request.artifact_id)
    if isinstance(authorized, ToolResponse):
        return authorized
    try:
        terms = parse_literal_terms(request.query)
    except ArtifactSearchInputError:
        return _config_error(request.artifact_id, data={"reason": "bad_search_input"})
    refs: dict[str, str] = {"object": authorized.key}
    loaded = await _load_redacted_plaintext(authorized.key, store_factory, refs)
    if loaded.drift:
        return _config_error(request.artifact_id)
    data = _find_response_data(
        loaded,
        terms=terms,
        direction=request.direction,
        byte_offset=request.byte_offset,
        max_bytes=request.max_bytes,
        artifact_id=request.artifact_id,
    )
    if isinstance(data, ToolResponse):
        return data
    return ToolResponse.success(
        request.artifact_id,
        "available",
        suggested_next_actions=["artifacts.find"],
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
        # Store outage: surface only the content_unavailable reason (like `artifacts.get`).
        # `match_found` is omitted because no search ran — emitting `match_found=false` would
        # read as "searched, no such signature" when the truth is "could not read the log".
        return loaded.degraded or {}
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
