"""Append-only per-call usage analytics writer (ADR-0148, #506).

`record_usage` writes one `tool_invocation` row recording a dispatched tool call's
dimensions. This is operational analytics, not an audit trail: no membership guard. The
recorder (``UsageTrackingMiddleware``) calls it best-effort, so a write failure never
affects the tool call.

``args_digest`` (ADR-0304, #1010) is a stable SHA-256 hex over the call's *redacted*
arguments — a secret-free correlation key, not recoverable args, so the table stays
analytics rather than an audit trail. :func:`digest_args` computes it over the same
:class:`~kdive.security.secrets.redaction.Redactor` the log/telemetry boundaries use.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from kdive.security.secrets.redaction import Redactor


def digest_args(redactor: Redactor, arguments: Mapping[str, object] | None) -> str:
    """Return a stable SHA-256 hex digest over ``arguments`` after redaction (ADR-0304).

    The mapping is redacted through ``redactor`` (dropping registered secret values and
    ``key=value`` secret patterns) and serialized canonically (sorted keys, compact
    separators) so identical redacted args always yield the same digest and no secret
    value reaches the hash. A call with no arguments digests the empty mapping, so the
    result is always a non-empty hex string.
    """
    redacted = redactor.redact_mapping(arguments or {})
    canonical = json.dumps(redacted, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One dispatched tool call's recorded dimensions.

    ``project`` is nullable: a list-time or object-resolving call may carry no resolvable
    project at the dispatch boundary. ``outcome`` is one of ``ok`` / ``error`` / ``denied``
    (CHECK-constrained at the DB). ``actor`` reuses the operator-cli / agent / unknown
    classification (ADR-0089). ``args_digest`` is the redacted-args digest (ADR-0304);
    ``None`` only for a caller that records no arguments dimension.
    """

    principal: str
    agent_session: str | None
    project: str | None
    tool: str
    outcome: str
    actor: str
    client_id: str | None
    args_digest: str | None = None


async def record_usage(conn: AsyncConnection, event: UsageEvent) -> UUID:
    """Append one ``tool_invocation`` row; return its id.

    Runs the INSERT on ``conn`` without opening a transaction, so the caller controls
    commit. ``outcome`` is CHECK-constrained at the DB to ``ok|error|denied``.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO tool_invocation "
            "(principal, agent_session, project, tool, outcome, actor, client_id, args_digest) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                event.principal,
                event.agent_session,
                event.project,
                event.tool,
                event.outcome,
                event.actor,
                event.client_id,
                event.args_digest,
            ),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into tool_invocation returned no row")
    return row[0]
