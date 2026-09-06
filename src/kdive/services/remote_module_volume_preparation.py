"""Compose verified intent with remote-module provider preparation (ADRs 0604, 0605)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Protocol

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
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as cancelled:
                while not task.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.shield(task)
                if not task.cancelled():
                    task.exception()
                raise cancelled from None
        assert operation is not None
        return await executor.run(lambda: operation(attempt, identity, check_deadline))

    return await run_verified_module_attempt_preparation(
        pool, repository, request, expected_attempt, consume
    )
