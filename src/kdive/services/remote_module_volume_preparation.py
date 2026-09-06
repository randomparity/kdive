"""Compose verified intent with remote-module provider preparation (ADRs 0604, 0605)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.device_identity import (
    build_remote_device_identity_port,
)
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.services.remote_module_attempt_preparation import (
    run_verified_module_attempt_preparation,
)


class SynchronousPreparation[ResultT](Protocol):
    def __call__(
        self,
        attempt: ModuleAttempt,
        identity: RemoteDeviceIdentityPort,
        check_deadline: Callable[[], None],
    ) -> ResultT: ...


class AwaitedPreparation[ResultT](Protocol):
    async def __call__(
        self,
        attempt: ModuleAttempt,
        identity: RemoteDeviceIdentityPort,
        check_deadline: Callable[[], None],
    ) -> ResultT: ...


async def prepare_verified_remote_module_attempt[ResultT](
    pool: AsyncConnectionPool,
    repository: RemoteModuleAttemptObligationRepository,
    request: ModuleAttemptPreparationRequestV1,
    expected_attempt: ModuleAttempt,
    executor: RemoteModulePreparationExecutor,
    authority: AuthorityRequestSender | None,
    preparation_deadline: float,
    operation: SynchronousPreparation[ResultT] | None,
    *,
    awaited_operation: AwaitedPreparation[ResultT] | None = None,
    commit_result: Callable[[AsyncConnection, ModuleAttempt, ResultT], Awaitable[None]]
    | None = None,
    allow_terminal_replay: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> ResultT:
    """Verify durable intent and retain its lock through synchronous completion."""
    identity = build_remote_device_identity_port(authority, preparation_deadline)
    if identity is None:
        raise CategorizedError(
            "remote module provider authority is unavailable",
            category=ErrorCategory.CONFLICT,
        )

    def check_deadline() -> None:
        if clock() >= preparation_deadline:
            raise TimeoutError("remote module preparation deadline expired")

    if (operation is None) == (awaited_operation is None):
        raise ValueError("exactly one remote module preparation consumer is required")

    async def consume(attempt: ModuleAttempt) -> ResultT:
        if awaited_operation is not None:
            task = asyncio.create_task(awaited_operation(attempt, identity, check_deadline))
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
        assert operation is not None
        return await executor.run(lambda: operation(attempt, identity, check_deadline))

    return await run_verified_module_attempt_preparation(
        pool,
        repository,
        request,
        expected_attempt,
        consume,
        commit_result=commit_result,
        allow_terminal_replay=allow_terminal_replay,
    )
