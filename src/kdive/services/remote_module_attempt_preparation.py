"""Commit and verify remote-module attempt preparation obligations (ADR-0605)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.remote_module_attempt_preparation import (
    ModuleAttemptPreparationRequestV1,
)


class ModuleAttemptObligationVerificationError(RuntimeError):
    """The durable obligation could not authorize this preparation attempt."""


def _verification_failed() -> ModuleAttemptObligationVerificationError:
    return ModuleAttemptObligationVerificationError("module-attempt obligation verification failed")


async def run_verified_module_attempt_preparation[ResultT](
    pool: AsyncConnectionPool,
    repository: RemoteModuleAttemptObligationRepository,
    request: ModuleAttemptPreparationRequestV1,
    expected_attempt: ModuleAttempt,
    consumer: Callable[[ModuleAttempt], Awaitable[ResultT]],
    *,
    commit_result: Callable[[psycopg.AsyncConnection, ModuleAttempt, ResultT], Awaitable[None]]
    | None = None,
    allow_terminal_replay: bool = False,
) -> ResultT:
    """Verify committed intent, then directly await one inline two-volume consumer.

    The consumer must finish both operations before returning. It must not detach or schedule
    background work because the transaction-scoped System lock is released when this call exits.
    """
    receipt = request.module_attempt_obligation
    if receipt.system_id != expected_attempt.system_id or receipt.run_id != expected_attempt.run_id:
        raise _verification_failed()
    if receipt.operation_nonce != expected_attempt.operation_nonce:
        raise _verification_failed()

    try:
        async with pool.connection() as conn, conn.transaction():
            if commit_result is None:
                await conn.execute("SET TRANSACTION READ ONLY")
            async with advisory_xact_lock(conn, LockScope.SYSTEM, expected_attempt.system_id):
                preparable = await repository.attempt_is_preparable(conn, expected_attempt)
                if not preparable:
                    replayable = (
                        allow_terminal_replay
                        and await repository.mutation_obligation_is_open(conn, expected_attempt)
                        and await repository.read_terminal_evidence(conn, expected_attempt)
                        is not None
                    )
                    if not replayable:
                        raise _verification_failed()
                result = await consumer(expected_attempt)
                if commit_result is not None:
                    await commit_result(conn, expected_attempt, result)
                return result
    except ModuleAttemptObligationVerificationError:
        raise
    except psycopg.Error:
        raise _verification_failed() from None
