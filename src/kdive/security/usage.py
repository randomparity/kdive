"""Append-only per-call usage analytics writer (ADR-0148, #506).

`record_usage` writes one `tool_invocation` row recording a dispatched tool call's
dimensions. This is operational analytics, not an audit trail: no membership guard and no
``args_digest`` (distinct from :mod:`kdive.security.audit`). The recorder
(``UsageTrackingMiddleware``) calls it best-effort, so a write failure never affects the
tool call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from psycopg import AsyncConnection


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One dispatched tool call's recorded dimensions.

    ``project`` is nullable: a list-time or object-resolving call may carry no resolvable
    project at the dispatch boundary. ``outcome`` is one of ``ok`` / ``error`` / ``denied``
    (CHECK-constrained at the DB). ``actor`` reuses the operator-cli / agent / unknown
    classification (ADR-0089).
    """

    principal: str
    agent_session: str | None
    project: str | None
    tool: str
    outcome: str
    actor: str
    client_id: str | None


async def record_usage(conn: AsyncConnection, event: UsageEvent) -> UUID:
    """Append one ``tool_invocation`` row; return its id.

    Runs the INSERT on ``conn`` without opening a transaction, so the caller controls
    commit. ``outcome`` is CHECK-constrained at the DB to ``ok|error|denied``.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO tool_invocation "
            "(principal, agent_session, project, tool, outcome, actor, client_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                event.principal,
                event.agent_session,
                event.project,
                event.tool,
                event.outcome,
                event.actor,
                event.client_id,
            ),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into tool_invocation returned no row")
    return row[0]
