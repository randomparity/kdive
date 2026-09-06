"""Completion-owned remote-module preparation offload (ADRs 0604, 0605)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
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
from kdive.services.remote_module_attempt_preparation import (
    run_verified_module_attempt_preparation,
)

_CAPACITY = 4


class SynchronousPreparation[ResultT](Protocol):
    def __call__(
        self,
        attempt: ModuleAttempt,
        identity: RemoteDeviceIdentityPort,
        check_deadline: Callable[[], None],
    ) -> ResultT: ...


def _unavailable() -> CategorizedError:
    return CategorizedError(
        "remote module preparation capacity is unavailable",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
    )


class RemoteModulePreparationExecutor:
    """Run at most four synchronous preparations without releasing early."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=_CAPACITY,
            thread_name_prefix="kdive-remote-module-prepare",
        )
        self._admission = threading.BoundedSemaphore(_CAPACITY)
        self._state_lock = threading.Lock()
        self._closed = False

    async def run[ResultT](self, operation: Callable[[], ResultT]) -> ResultT:
        with self._state_lock:
            if self._closed or not self._admission.acquire(blocking=False):
                raise _unavailable()
            try:
                future = self._executor.submit(operation)
            except RuntimeError:
                self._admission.release()
                raise _unavailable() from None
        future.add_done_callback(lambda _future: self._admission.release())
        return await self._await_completion(future)

    def shutdown(self) -> None:
        """Reject new work and cancel queued calls without waiting for running calls."""
        with self._state_lock:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    async def _await_completion[ResultT](future: Future[ResultT]) -> ResultT:
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            assert task is not None
            cancellations = 1
            task.uncancel()
            while not wrapped.done():
                try:
                    await asyncio.shield(wrapped)
                except asyncio.CancelledError:
                    cancellations += 1
                    task.uncancel()
            for _ in range(cancellations):
                task.cancel()
            raise


async def prepare_verified_remote_module_attempt[ResultT](
    pool: AsyncConnectionPool,
    repository: RemoteModuleAttemptObligationRepository,
    request: ModuleAttemptPreparationRequestV1,
    expected_attempt: ModuleAttempt,
    executor: RemoteModulePreparationExecutor,
    authority: AuthorityRequestSender | None,
    preparation_deadline: float,
    operation: SynchronousPreparation[ResultT],
    *,
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

    async def consume(attempt: ModuleAttempt) -> ResultT:
        return await executor.run(lambda: operation(attempt, identity, check_deadline))

    return await run_verified_module_attempt_preparation(
        pool,
        repository,
        request,
        expected_attempt,
        consume,
    )
