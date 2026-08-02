"""Permanent registration and termination evidence for exact worker incarnations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from kdive.db.locks import LockScope, advisory_xact_lock, require_top_level_transaction

type AuthorityKind = Literal["local", "docker", "kubernetes"]
type TerminationOutcome = Literal["succeeded", "failed", "killed"]


class IncarnationConflict(RuntimeError):
    """An immutable incarnation was replayed with conflicting facts."""


@dataclass(frozen=True, slots=True)
class WorkerIncarnation:
    """One permanent exact-incarnation record."""

    incarnation: str
    authority_kind: AuthorityKind
    authority_binding: dict[str, Any]
    state: Literal["active", "terminated"]
    recorded_at: datetime
    terminated_at: datetime | None
    outcome: TerminationOutcome | None


def _record(row: tuple[Any, ...]) -> WorkerIncarnation:
    return WorkerIncarnation(
        incarnation=cast(str, row[0]),
        authority_kind=cast(AuthorityKind, row[1]),
        authority_binding=cast(dict[str, Any], row[2]),
        state=cast(Literal["active", "terminated"], row[3]),
        recorded_at=cast(datetime, row[4]),
        terminated_at=cast(datetime | None, row[5]),
        outcome=cast(TerminationOutcome | None, row[6]),
    )


async def _get_locked(conn: AsyncConnection, incarnation: str) -> WorkerIncarnation | None:
    row = await (
        await conn.execute(
            "SELECT incarnation, authority_kind, authority_binding, state, recorded_at, "
            "terminated_at, outcome FROM worker_incarnations WHERE incarnation = %s FOR UPDATE",
            (incarnation,),
        )
    ).fetchone()
    return None if row is None else _record(row)


async def register_worker_incarnation(
    conn: AsyncConnection,
    incarnation: str,
    authority_kind: AuthorityKind,
    authority_binding: dict[str, Any],
) -> WorkerIncarnation:
    """Register one immutable active incarnation; allow only an identical active replay."""
    if not incarnation or len(incarnation) > 512 or not authority_binding:
        raise ValueError("worker incarnation and authority binding must be non-empty and bounded")
    require_top_level_transaction(conn, "register_worker_incarnation")
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.WORKER_INCARNATION, incarnation),
    ):
        current = await _get_locked(conn, incarnation)
        if current is not None:
            if (
                current.state == "active"
                and current.authority_kind == authority_kind
                and current.authority_binding == authority_binding
            ):
                return current
            raise IncarnationConflict(
                "worker incarnation registration conflicts with durable state"
            )
        row = await (
            await conn.execute(
                "INSERT INTO worker_incarnations "
                "(incarnation, authority_kind, authority_binding) VALUES (%s, %s, %s) "
                "RETURNING incarnation, authority_kind, authority_binding, state, recorded_at, "
                "terminated_at, outcome",
                (incarnation, authority_kind, Jsonb(authority_binding)),
            )
        ).fetchone()
        assert row is not None
        return _record(row)


async def terminate_worker_incarnation(
    conn: AsyncConnection, incarnation: str, outcome: TerminationOutcome
) -> WorkerIncarnation:
    """Permanently terminate an exact registered incarnation; allow identical replay."""
    require_top_level_transaction(conn, "terminate_worker_incarnation")
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.WORKER_INCARNATION, incarnation),
    ):
        current = await _get_locked(conn, incarnation)
        if current is None:
            raise IncarnationConflict("worker incarnation was never registered")
        if current.state == "terminated":
            if current.outcome == outcome:
                return current
            raise IncarnationConflict("worker termination outcome conflicts with durable state")
        row = await (
            await conn.execute(
                "UPDATE worker_incarnations SET state = 'terminated', terminated_at = now(), "
                "outcome = %s WHERE incarnation = %s "
                "RETURNING incarnation, authority_kind, authority_binding, state, recorded_at, "
                "terminated_at, outcome",
                (outcome, incarnation),
            )
        ).fetchone()
        assert row is not None
        return _record(row)
