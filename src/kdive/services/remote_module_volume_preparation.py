"""Compose verified intent with remote-module provider preparation (ADRs 0604, 0605)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.services.remote_module_attempt_preparation import (
    run_verified_module_attempt_preparation,
)


async def prepare_verified_remote_module_attempt[ResultT](
    pool: AsyncConnectionPool,
    repository: RemoteModuleAttemptObligationRepository,
    request: ModuleAttemptPreparationRequestV1,
    expected_attempt: ModuleAttempt,
    preparation_deadline: float,
    operation: Callable[[ModuleAttempt, Callable[[], None]], Awaitable[ResultT]],
    *,
    commit_result: Callable[[AsyncConnection, ModuleAttempt, ResultT], Awaitable[None]]
    | None = None,
    allow_terminal_replay: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> ResultT:
    """Verify durable intent and retain its lock through asynchronous completion."""

    def check_deadline() -> None:
        if clock() >= preparation_deadline:
            raise TimeoutError("remote module preparation deadline expired")

    async def consume(attempt: ModuleAttempt) -> ResultT:
        task = asyncio.ensure_future(operation(attempt, check_deadline))
        caller = asyncio.current_task()
        assert caller is not None
        completed = asyncio.Event()
        task.add_done_callback(lambda _task: completed.set())
        cancelled: asyncio.CancelledError | None = None
        consumed_cancellations = 0
        while not completed.is_set():
            try:
                await completed.wait()
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                consumed_cancellations += 1
                caller.uncancel()
        if cancelled is None and caller.cancelling() != 0:
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError as error:
                cancelled = error
                consumed_cancellations += 1
                caller.uncancel()
        if cancelled is not None:
            if not task.cancelled():
                task.exception()
            message = cancelled.args[0] if cancelled.args else None
            for _ in range(consumed_cancellations):
                caller.cancel(message)
            raise cancelled from None
        return task.result()

    return await run_verified_module_attempt_preparation(
        pool,
        repository,
        request,
        expected_attempt,
        consume,
        commit_result=commit_result,
        allow_terminal_replay=allow_terminal_replay,
    )
