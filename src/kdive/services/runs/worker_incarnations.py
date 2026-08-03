"""Role-gated registration and authentication for exact worker incarnations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from psycopg import AsyncConnection, errors
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.locks import require_top_level_transaction

type AuthorityKind = Literal["local", "docker", "kubernetes"]
type TerminationOutcome = Literal["succeeded", "failed", "killed"]

CURRENT_WORKER_FENCE_PROTOCOL = 2


class IncarnationConflict(RuntimeError):
    """An immutable incarnation was replayed with conflicting facts."""


class IncarnationAuthenticationError(RuntimeError):
    """A credential did not identify an active worker incarnation."""


@dataclass(frozen=True, slots=True)
class WorkerIncarnation:
    """Public immutable facts for one authority-registered incarnation."""

    incarnation: str
    authority_kind: AuthorityKind
    authority_binding: dict[str, Any]
    fence_protocol: int


def _record(row: tuple[Any, ...]) -> WorkerIncarnation:
    return WorkerIncarnation(
        incarnation=cast(str, row[0]),
        authority_kind=cast(AuthorityKind, row[1]),
        authority_binding=cast(dict[str, Any], row[2]),
        fence_protocol=cast(int, row[3]),
    )


async def register_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: AuthorityKind,
    binding: dict[str, Any],
    credential_hash: bytes,
    fence_protocol: int,
) -> WorkerIncarnation:
    """Ask the lifecycle-witness authority to register immutable incarnation facts."""
    require_top_level_transaction(conn, "register_worker_incarnation")
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT public.register_worker_incarnation(%s, %s, %s, %s, %s)",
                (
                    incarnation,
                    authority_kind,
                    Jsonb(binding),
                    credential_hash,
                    fence_protocol,
                ),
            )
    except errors.UniqueViolation as exc:
        raise IncarnationConflict(
            "worker incarnation registration conflicts with durable state"
        ) from exc
    return WorkerIncarnation(
        incarnation=incarnation,
        authority_kind=authority_kind,
        authority_binding=binding,
        fence_protocol=fence_protocol,
    )


async def authenticate_worker_incarnation(
    conn: AsyncConnection, credential: SecretStr
) -> WorkerIncarnation:
    """Authenticate an active identity without accepting caller-supplied holder facts."""
    require_top_level_transaction(conn, "authenticate_worker_incarnation")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT incarnation, authority_kind, authority_binding, fence_protocol "
                "FROM public.authenticate_worker_incarnation("
                "sha256(convert_to(%s, 'UTF8')))",
                (credential.get_secret_value(),),
            )
        ).fetchone()
    if row is None:
        raise IncarnationAuthenticationError(
            "worker incarnation credential does not identify an active incarnation"
        )
    return _record(row)


async def terminate_worker_incarnation(
    conn: AsyncConnection, incarnation: str, outcome: TerminationOutcome
) -> bool:
    """Ask the lifecycle-witness authority to terminate one exact incarnation."""
    require_top_level_transaction(conn, "terminate_worker_incarnation")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT public.terminate_worker_incarnation(%s, %s)",
                (incarnation, outcome),
            )
        ).fetchone()
    assert row is not None
    return bool(row[0])
