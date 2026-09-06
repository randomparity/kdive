"""Completion-owned remote-module preparation offload (ADRs 0604, 0605)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from kdive.domain.errors import CategorizedError, ErrorCategory

_CAPACITY = 4


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
