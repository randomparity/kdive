"""Envelope-replay idempotency helpers shared across mutating tools (ADR-0193).

The mutating MCP surface reuses the ``idempotency_keys`` table (PK ``(principal, key)``,
``kind`` discriminator, ``result jsonb``) that already backs ``allocations.{request,renew}``
(ADR-0040 §3). Where the allocation path stores only an ``allocation_id`` and re-reads the
live row, this path stores the returned :class:`ToolResponse` **envelope itself**, so a
repeated key replays a byte-identical envelope for any object kind — Run, System,
Investigation, or job handle.

Contract (see ADR-0193 / the design spec):

- :func:`resolve_envelope_replay` is the up-front read: a hit short-circuits the mutation.
- :func:`record_envelope` writes the success envelope **inside the mutation's own
  transaction**, so "object committed but key unrecorded" is impossible. It lets a
  ``UniqueViolation`` on the PK propagate (the transaction aborts); the caller catches it
  *after* exiting the transaction and calls :func:`resolve_conflict`.
- :func:`resolve_conflict` is the read-after-conflict step: it re-resolves the replay and
  returns the winner's envelope (the self-race case), or raises ``CONFLICT`` when the
  colliding row belongs to a different tool (genuine cross-operation key reuse).
- :func:`validate_idempotency_key` bounds the client-controlled key before any DB work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse

# The client key is a `text` component of the `(principal, key)` primary key; bound it so an
# unbounded client value cannot bloat the index / table.
_MAX_KEY_LEN = 200


def validate_idempotency_key(key: str) -> None:
    """Reject an empty or over-long idempotency key before any DB work.

    Args:
        key: The client-supplied idempotency key.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the key is empty or longer than
            200 characters.
    """
    if not key or len(key) > _MAX_KEY_LEN:
        raise CategorizedError(
            f"idempotency_key must be 1-{_MAX_KEY_LEN} characters",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "idempotency_key_invalid"},
        )


async def resolve_envelope_replay(
    conn: AsyncConnection, *, principal: str, key: str, kind: str
) -> ToolResponse | None:
    """Return the envelope stored for ``(principal, key)`` under ``kind``, or ``None``."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT result FROM idempotency_keys WHERE principal = %s AND key = %s AND kind = %s",
            (principal, key, kind),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return ToolResponse.model_validate(row[0]["envelope"])


async def record_envelope(
    conn: AsyncConnection,
    *,
    principal: str,
    key: str,
    project: str,
    kind: str,
    envelope: ToolResponse,
) -> None:
    """Persist ``envelope`` for ``(principal, key)`` in the caller's open transaction.

    Lets :class:`psycopg.errors.UniqueViolation` propagate on the ``(principal, key)`` PK so
    the caller can roll back and re-resolve (read-after-conflict); the category mapping lives
    in :func:`resolve_conflict`, not here.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
            "VALUES (%s, %s, %s, %s, %s)",
            (key, principal, project, kind, Jsonb({"envelope": envelope.model_dump(mode="json")})),
        )


async def resolve_conflict(
    conn: AsyncConnection, *, principal: str, key: str, kind: str
) -> ToolResponse:
    """Re-resolve after a record collision; return the winner's envelope or raise ``CONFLICT``.

    Call this from an ``except UniqueViolation`` block *after* the aborted transaction has
    exited (a Postgres transaction cannot run further queries until rollback). A row under the
    same ``kind`` is the self-race case — return its envelope. A miss means the colliding
    ``(principal, key)`` belongs to a different tool (the PK is ``(principal, key)``, not
    ``(principal, key, kind)``): genuine cross-operation reuse, surfaced as ``CONFLICT``.
    """
    replay = await resolve_envelope_replay(conn, principal=principal, key=key, kind=kind)
    if replay is not None:
        return replay
    raise CategorizedError(
        f"idempotency_key ({principal}, {key}) is already in use by another operation",
        category=ErrorCategory.CONFLICT,
        details={"reason": "idempotency_key_in_use"},
    )


async def keyed_mutation(
    conn: AsyncConnection,
    *,
    idempotency_key: str | None,
    principal: str,
    project: str,
    kind: str,
    do_work: Callable[[], Awaitable[ToolResponse]],
) -> ToolResponse:
    """Run a job-enqueuing mutation under optional replay-idempotency on one connection.

    The handler-owned-connection (topology 1) orchestration: when ``idempotency_key`` is
    ``None`` this just calls ``do_work`` (today's behavior, unchanged). When supplied it:
    validates the key; resolves a replay up-front (a hit short-circuits with no work); else
    runs ``do_work`` and records its success envelope **in the same transaction**; on a PK
    collision it rolls back and re-resolves (the winner's envelope, or ``CONFLICT`` for a
    cross-tool reuse).

    ``do_work`` must perform the durable effect (e.g. ``queue.enqueue``) and return the
    success envelope; it is invoked inside a fresh ``conn.transaction()`` so the effect and
    the key record commit atomically. A failure envelope is **not** recorded (a denial is not
    cached); ``do_work`` should raise or short-circuit *before* returning rather than
    returning a failure it wants recorded.

    Always returns a :class:`ToolResponse` (never raises for key handling): an invalid key or
    a genuine cross-tool collision is mapped to a failure envelope so the tool boundary stays
    "errors are envelopes".
    """
    if idempotency_key is None:
        return await do_work()
    try:
        validate_idempotency_key(idempotency_key)
    except CategorizedError as exc:
        # Do not echo the (rejected, unbounded) key as object_id.
        return ToolResponse.failure_from_error("idempotency_key", exc)
    replay = await resolve_envelope_replay(
        conn, principal=principal, key=idempotency_key, kind=kind
    )
    if replay is not None:
        return replay
    try:
        async with conn.transaction():
            envelope = await do_work()
            if envelope.error_category is not None:
                # A denial/validation failure is not cached (ADR-0193): leave the key unused
                # so the caller can correct the input and retry it. Returning here lets the
                # transaction commit any side effects do_work intended for the failure path
                # (today none of the wired tools mutate on the failure branch).
                return envelope
            await record_envelope(
                conn,
                principal=principal,
                key=idempotency_key,
                project=project,
                kind=kind,
                envelope=envelope,
            )
    except UniqueViolation:
        try:
            return await resolve_conflict(conn, principal=principal, key=idempotency_key, kind=kind)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(idempotency_key, exc)
    return envelope
