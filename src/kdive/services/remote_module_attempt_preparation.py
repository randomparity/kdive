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
    ModuleAttemptObligationReceiptV1,
    ModuleAttemptPreparationRequestV1,
)


class ModuleAttemptObligationVerificationError(RuntimeError):
    """The durable obligation could not authorize this preparation attempt."""


def _verification_failed() -> ModuleAttemptObligationVerificationError:
    return ModuleAttemptObligationVerificationError("module-attempt obligation verification failed")


async def open_module_attempt_preparation(
    pool: AsyncConnectionPool,
    repository: RemoteModuleAttemptObligationRepository,
    attempt: ModuleAttempt,
) -> ModuleAttemptPreparationRequestV1:
    """Commit an open obligation before returning its dispatchable request."""
    try:
        async with pool.connection() as conn, conn.transaction():
            await repository.open_mutation_obligation(conn, attempt)
            if not await repository.mutation_obligation_is_open(conn, attempt):
                raise _verification_failed()
    except ModuleAttemptObligationVerificationError:
        raise
    except psycopg.Error as error:
        raise _verification_failed() from error

    return ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=attempt.system_id,
            run_id=attempt.run_id,
            operation_nonce=attempt.operation_nonce,
        )
    )


async def run_verified_module_attempt_preparation[ResultT](
    pool: AsyncConnectionPool,
    repository: RemoteModuleAttemptObligationRepository,
    request: ModuleAttemptPreparationRequestV1,
    expected_attempt: ModuleAttempt,
    consumer: Callable[[ModuleAttempt], Awaitable[ResultT]],
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
            await conn.execute("SET TRANSACTION READ ONLY")
            async with advisory_xact_lock(conn, LockScope.SYSTEM, expected_attempt.system_id):
                if not await repository.mutation_obligation_is_open(conn, expected_attempt):
                    raise _verification_failed()
                return await consumer(expected_attempt)
    except ModuleAttemptObligationVerificationError:
        raise
    except psycopg.Error as error:
        raise _verification_failed() from error
