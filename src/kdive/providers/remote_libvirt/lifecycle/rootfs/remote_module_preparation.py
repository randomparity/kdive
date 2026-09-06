"""Completion-owned remote-module preparation offload (ADRs 0604, 0605)."""

from __future__ import annotations

import asyncio
import contextlib
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
        loop = asyncio.get_running_loop()
        completed = loop.create_future()

        def signal_completion(_future: Future[ResultT]) -> None:
            loop.call_soon_threadsafe(completed.set_result, None)

        future.add_done_callback(signal_completion)
        try:
            await asyncio.shield(completed)
        except asyncio.CancelledError as cancelled:
            while not completed.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(completed)
            if not future.cancelled():
                future.exception()
            raise cancelled from None
        return future.result()
