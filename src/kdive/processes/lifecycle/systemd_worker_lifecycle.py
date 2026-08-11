"""Injected authority ports for retained systemd worker lifecycle reconciliation."""

from __future__ import annotations

from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.processes.lifecycle.systemd_worker_state import SlotState, TerminationOutcome
from kdive.services.runs.worker_incarnations import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    register_worker_incarnation,
    terminate_worker_incarnation,
)


class EvidenceRejected(RuntimeError):
    """PostgreSQL rejected purported terminal evidence for an exact incarnation."""


class IncarnationAuthority(Protocol):
    """Register and terminate exact immutable worker-incarnation facts."""

    async def register(self, state: SlotState, credential_hash: bytes) -> None:
        """Register one exact local-systemd incarnation and binding."""
        ...

    async def terminate(self, state: SlotState, outcome: TerminationOutcome) -> None:
        """Commit terminal evidence only for the same exact registered binding."""
        ...


class PostgresAuthority:
    """Thin witness-role adapter over the existing worker-incarnation services."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def register(self, state: SlotState, credential_hash: bytes) -> None:
        """Register the slot's unchanged local authority binding and current protocol."""
        binding = state.authority_binding()
        async with self.pool.connection() as connection:
            await register_worker_incarnation(
                connection,
                state.incarnation,
                "local",
                binding,
                credential_hash,
                CURRENT_WORKER_FENCE_PROTOCOL,
            )

    async def terminate(self, state: SlotState, outcome: TerminationOutcome) -> None:
        """Require PostgreSQL to accept the exact binding before reporting evidence."""
        async with self.pool.connection() as connection:
            accepted = await terminate_worker_incarnation(
                connection,
                state.incarnation,
                "local",
                state.authority_binding(),
                outcome,
            )
        if not accepted:
            raise EvidenceRejected(f"database rejected termination evidence for slot {state.slot}")
