"""Shared helpers for Investigation MCP handlers."""

from __future__ import annotations

from typing import TypedDict
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import InvestigationState
from kdive.domain.lifecycle.records import ExternalRef, Investigation
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import ConfigErrorReason
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import not_found as _not_found
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

TERMINAL_INVESTIGATION = frozenset({InvestigationState.CLOSED, InvestigationState.ABANDONED})
TITLE_MAX = 200
DESCRIPTION_MAX = 4096


class ExternalRefInput(TypedDict):
    """Raw MCP input for a full external tracker reference."""

    tracker: str
    id: str
    url: str


class ExternalRefKey(TypedDict, total=False):
    """Raw MCP input identifying an external reference by natural key."""

    tracker: str
    id: str


def invalid_text_error(object_id: str) -> ToolResponse:
    """Return a ``configuration_error`` for an out-of-bounds title/description."""
    return _config_error_reason(
        object_id,
        ConfigErrorReason.INVALID_TEXT,
        detail=(
            f"title must be 1..{TITLE_MAX} chars and description at most {DESCRIPTION_MAX} chars"
        ),
    )


def natural_key(ref: ExternalRefKey) -> tuple[str, str] | None:
    """Return a ref's ``(tracker, id)`` identity, or ``None`` for malformed input."""
    try:
        tracker = ref["tracker"]
        rid = ref["id"]
    except KeyError:
        return None
    if not isinstance(tracker, str) or not tracker:
        return None
    if not isinstance(rid, str) or not rid:
        return None
    return (tracker, rid)


def parse_external_refs(raw: list[ExternalRefInput] | None) -> list[ExternalRef]:
    """Parse and deduplicate external refs by the ``(tracker, id)`` natural key."""
    if raw is None:
        return []
    by_key: dict[tuple[str, str], ExternalRef] = {}
    for entry in raw:
        ref = ExternalRef.model_validate(entry)
        by_key[(ref.tracker, ref.id)] = ref
    return list(by_key.values())


def refs_jsonb(refs: list[ExternalRef]) -> Jsonb:
    """Serialize external refs for the investigations row."""
    return Jsonb([r.model_dump() for r in refs])


def validate_text(title: str | None, description: str | None) -> bool:
    """Return whether supplied title/description are within their write-boundary bounds."""
    if title is not None and not (1 <= len(title) <= TITLE_MAX):
        return False
    return description is None or len(description) <= DESCRIPTION_MAX


def invalid_external_refs_error(object_id: str, *, key_only: bool = False) -> ToolResponse:
    """Return the shared invalid-external-ref response."""
    detail = (
        "ref key must carry a non-empty tracker and id"
        if key_only
        else "ref must carry a tracker, id, and url"
    )
    return _config_error_reason(
        object_id,
        ConfigErrorReason.INVALID_EXTERNAL_REF,
        detail=detail,
    )


async def get_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    """Fetch one Investigation row under ``FOR UPDATE``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def get_mutable_investigation_locked(
    conn: AsyncConnection, uid: UUID
) -> Investigation | ToolResponse:
    """Return a locked non-terminal Investigation, or the mutation config error."""
    current = await get_for_update(conn, uid)
    if current is None:
        return _config_error(str(uid), detail="Investigation no longer exists")
    if current.state in TERMINAL_INVESTIGATION:
        return _config_error(
            str(uid),
            detail=f"Investigation is {current.state.value}; it cannot be edited",
            data={"current_status": current.state.value},
        )
    return current


async def resolve_contributor_investigation(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, raw_id: str
) -> Investigation | ToolResponse:
    """Resolve a contributor-writable Investigation row or return not-found-shaped error."""
    inv = await INVESTIGATIONS.get(conn, uid)
    if inv is None or inv.project not in ctx.projects:
        return _not_found(raw_id)
    require_role(ctx, inv.project, Role.CONTRIBUTOR)
    return inv


def parse_external_ref_input(raw: ExternalRefInput, object_id: str) -> ExternalRef | ToolResponse:
    """Parse a single external ref, returning the standardized config error on failure."""
    try:
        return ExternalRef.model_validate(raw)
    except ValidationError:
        return invalid_external_refs_error(object_id)
