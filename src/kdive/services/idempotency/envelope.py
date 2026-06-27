"""Stored-result idempotency helpers shared across mutating operations (ADR-0193).

Mutating surfaces reuse the ``idempotency_keys`` table (PK ``(principal, key)``, ``kind``
discriminator, ``result jsonb``) that already backs ``allocations.{request,renew}``
(ADR-0040 §3). Where the allocation path stores only an ``allocation_id`` and re-reads the
live row, this path stores a serialized success result so a repeated key can replay the
same boundary response for any object kind: Run, System, Investigation, or job handle.

Contract (see ADR-0193 / the design spec):

- :func:`resolve_replay` is the up-front read: a hit short-circuits the mutation.
- :func:`record_result` writes the success result **inside the mutation's own
  transaction**, so "object committed but key unrecorded" is impossible. It lets a
  ``UniqueViolation`` on the PK propagate (the transaction aborts); the caller catches it
  *after* exiting the transaction and calls :func:`resolve_conflict`.
- :func:`resolve_conflict` is the read-after-conflict step: it re-resolves the replay and
  returns the winner's result (the self-race case), or raises ``CONFLICT`` when the
  colliding row belongs to a different tool (genuine cross-operation key reuse).
- :func:`validate_idempotency_key` bounds the client-controlled key before any DB work.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.serialization import JsonValue

# The client key is a `text` component of the `(principal, key)` primary key; bound it so an
# unbounded client value cannot bloat the index / table.
_MAX_KEY_LEN = 200


@dataclass(frozen=True, slots=True)
class StoredResult:
    """Transport-neutral serialized result stored for idempotent replay."""

    document: dict[str, JsonValue]


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


async def resolve_replay(
    conn: AsyncConnection, *, principal: str, key: str, kind: str
) -> StoredResult | None:
    """Return the result stored for ``(principal, key)`` under ``kind``, or ``None``."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT result FROM idempotency_keys WHERE principal = %s AND key = %s AND kind = %s",
            (principal, key, kind),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    result = row[0]
    if isinstance(result, dict) and isinstance(result.get("envelope"), dict):
        return StoredResult(document=result["envelope"])
    if isinstance(result, dict):
        return StoredResult(document=result)
    raise CategorizedError(
        "stored idempotency result is not a JSON object",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"reason": "invalid_idempotency_result"},
    )


async def record_result(
    conn: AsyncConnection,
    *,
    principal: str,
    key: str,
    project: str,
    kind: str,
    result: StoredResult,
) -> None:
    """Persist ``result`` for ``(principal, key)`` in the caller's open transaction.

    Lets :class:`psycopg.errors.UniqueViolation` propagate on the ``(principal, key)`` PK so
    the caller can roll back and re-resolve (read-after-conflict); the category mapping lives
    in :func:`resolve_conflict`, not here.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
            "VALUES (%s, %s, %s, %s, %s)",
            (key, principal, project, kind, Jsonb({"envelope": result.document})),
        )


async def resolve_conflict(
    conn: AsyncConnection, *, principal: str, key: str, kind: str
) -> StoredResult:
    """Re-resolve after a record collision; return the winner's result or raise ``CONFLICT``.

    Call this from an ``except UniqueViolation`` block *after* the aborted transaction has
    exited (a Postgres transaction cannot run further queries until rollback). A row under the
    same ``kind`` is the self-race case — return its result. A miss means the colliding
    ``(principal, key)`` belongs to a different tool (the PK is ``(principal, key)``, not
    ``(principal, key, kind)``): genuine cross-operation reuse, surfaced as ``CONFLICT``.
    """
    replay = await resolve_replay(conn, principal=principal, key=key, kind=kind)
    if replay is not None:
        return replay
    raise CategorizedError(
        f"idempotency_key ({principal}, {key}) is already in use by another operation",
        category=ErrorCategory.CONFLICT,
        details={"reason": "idempotency_key_in_use"},
    )
