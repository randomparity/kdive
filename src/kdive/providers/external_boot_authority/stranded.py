"""The authority's own facts about a stranded activation (ADR-0816, #3574)."""

from __future__ import annotations

import hashlib
import json
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row


class _Connections(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


@dataclass(frozen=True, slots=True)
class StrandedTarget:
    """What the database says the authority must prove absent, and the head it read."""

    system_id: UUID
    run_id: UUID
    domain_name: str
    object_refs: tuple[str, ...]
    journal_sequence: int | None
    journal_digest: str | None


@dataclass(frozen=True, slots=True)
class StrandedAuthorityFacts:
    """What the authority's own daemon and store hold for one activation."""

    domain_absent: bool
    store_absent: bool
    recovery_ref: str


def stranded_composite_state(activation_id: UUID, target: StrandedTarget) -> str:
    """Name what the authority observed as absent; settlement copies it to terminal evidence."""
    document = json.dumps(
        {
            "activation_id": str(activation_id),
            "domain_name": target.domain_name,
            "journal_digest": target.journal_digest,
            "journal_sequence": target.journal_sequence,
            "objects": sorted(target.object_refs),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(b"kdive-stranded-absence-v1\0" + document).hexdigest()


class StrandedObservationRepository:
    """The authority role's two statements about one instance's stranded activations."""

    def __init__(self, connections: _Connections, authority_instance: str) -> None:
        self._connections = connections
        self._authority_instance = authority_instance

    async def read_target(self, activation_id: UUID) -> StrandedTarget | None:
        async with (
            self._connections() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                "SELECT * FROM public.read_stranded_activation_target(%s,%s)",
                (activation_id, self._authority_instance),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return StrandedTarget(
            system_id=row["system_id"],
            run_id=row["run_id"],
            domain_name=row["domain_name"],
            object_refs=tuple(row["object_refs"]),
            journal_sequence=row["journal_sequence"],
            journal_digest=row["journal_digest"],
        )

    async def record_absence(
        self, activation_id: UUID, target: StrandedTarget, composite_state: str
    ) -> bool:
        async with self._connections() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT public.record_stranded_activation_authority_absence"
                    "(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        activation_id,
                        self._authority_instance,
                        target.domain_name,
                        target.journal_sequence,
                        target.journal_digest,
                        composite_state,
                        list(target.object_refs),
                    ),
                )
            ).fetchone()
        return row is not None and row[0] == "applied"
