"""Completion ownership for verified remote-module preparation."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.services.remote_module_volume_preparation import prepare_verified_remote_module_attempt


@pytest.fixture(autouse=True)
def _identity_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kdive.services.remote_module_volume_preparation.build_remote_device_identity_port",
        lambda authority, _deadline: authority,
    )


@pytest.mark.anyio
async def test_capacity_is_retained_until_underlying_calls_complete() -> None:
    executor = RemoteModulePreparationExecutor()
    release = threading.Event()
    started = threading.Barrier(5)

    def blocked() -> None:
        started.wait()
        release.wait()

    tasks = [asyncio.create_task(executor.run(blocked)) for _ in range(4)]
    await asyncio.to_thread(started.wait)
    with pytest.raises(CategorizedError) as caught:
        await executor.run(lambda: None)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE

    release.set()
    await asyncio.gather(*tasks)
    await executor.run(lambda: None)
    executor.shutdown()


@pytest.mark.anyio
async def test_repeated_cancellation_drains_completion_and_restores_state() -> None:
    executor = RemoteModulePreparationExecutor()
    started = threading.Event()
    release = threading.Event()

    def blocked() -> None:
        started.set()
        release.wait()

    task = asyncio.create_task(executor.run(blocked))
    await asyncio.to_thread(started.wait)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelling() == 2
    executor.shutdown()


@pytest.mark.anyio
async def test_cancellation_wins_when_underlying_operation_fails() -> None:
    executor = RemoteModulePreparationExecutor()
    started = threading.Event()
    release = threading.Event()

    def blocked_failure() -> None:
        started.set()
        release.wait()
        raise TimeoutError("provider deadline")

    task = asyncio.create_task(executor.run(blocked_failure))
    await asyncio.to_thread(started.wait)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelling() == 2
    assert task.cancelled()
    executor.shutdown()


@pytest.mark.anyio
async def test_cancellation_does_not_interrupt_callers_async_cleanup() -> None:
    executor = RemoteModulePreparationExecutor()
    started = threading.Event()
    release = threading.Event()
    cleanup_finished = False

    async def caller() -> None:
        nonlocal cleanup_finished
        try:
            await executor.run(lambda: (started.set(), release.wait()))
        finally:
            await asyncio.sleep(0)
            cleanup_finished = True

    task = asyncio.create_task(caller())
    await asyncio.to_thread(started.wait)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished
    executor.shutdown()


@pytest.mark.anyio
async def test_shutdown_is_nonwaiting_and_rejects_new_work() -> None:
    executor = RemoteModulePreparationExecutor()
    started = threading.Event()
    release = threading.Event()
    task = asyncio.create_task(executor.run(lambda: (started.set(), release.wait())))
    await asyncio.to_thread(started.wait)

    executor.shutdown()
    assert not task.done()
    with pytest.raises(CategorizedError):
        await executor.run(lambda: None)

    release.set()
    await task


@pytest.mark.anyio
async def test_verified_consumer_runs_inline_with_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = RemoteModulePreparationExecutor()
    observed: list[tuple[object, object]] = []
    expected_attempt = object()
    identity = object()

    async def verify(
        _pool: object,
        _repository: object,
        _request: object,
        attempt: object,
        consumer: Any,
    ) -> object:
        assert attempt is expected_attempt
        return await consumer(attempt)

    monkeypatch.setattr(
        "kdive.services.remote_module_volume_preparation.run_verified_module_attempt_preparation",
        verify,
    )
    result = await prepare_verified_remote_module_attempt(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, expected_attempt),
        executor,
        cast(Any, identity),
        10.0,
        lambda attempt, identity, check_deadline: (
            check_deadline(),
            observed.append((attempt, identity)),
            "done",
        )[-1],
        clock=lambda: 9.0,
    )

    assert result == "done"
    assert observed == [(expected_attempt, identity)]
    executor.shutdown()


@pytest.mark.anyio
async def test_cancellation_retains_verified_consumer_until_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = RemoteModulePreparationExecutor()
    started = threading.Event()
    release = threading.Event()
    verifier_active = False

    async def verify(
        _pool: object,
        _repository: object,
        _request: object,
        attempt: object,
        consumer: Any,
    ) -> object:
        nonlocal verifier_active
        verifier_active = True
        try:
            return await consumer(attempt)
        finally:
            verifier_active = False

    monkeypatch.setattr(
        "kdive.services.remote_module_volume_preparation.run_verified_module_attempt_preparation",
        verify,
    )

    def blocked(_attempt: object, _identity: object, check_deadline: Any) -> None:
        check_deadline()
        started.set()
        release.wait()

    task = asyncio.create_task(
        prepare_verified_remote_module_attempt(
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
            executor,
            cast(Any, object()),
            10.0,
            cast(Any, blocked),
            clock=lambda: 9.0,
        )
    )
    await asyncio.to_thread(started.wait)
    task.cancel()
    await asyncio.sleep(0)
    assert verifier_active
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not verifier_active
    executor.shutdown()


@pytest.mark.anyio
async def test_expired_deadline_reaches_no_provider_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = RemoteModulePreparationExecutor()

    async def verify(
        _pool: object,
        _repository: object,
        _request: object,
        attempt: object,
        consumer: Any,
    ) -> object:
        return await consumer(attempt)

    monkeypatch.setattr(
        "kdive.services.remote_module_volume_preparation.run_verified_module_attempt_preparation",
        verify,
    )
    reached = False

    def operation(_attempt: object, _identity: object, check_deadline: Any) -> None:
        nonlocal reached
        check_deadline()
        reached = True

    with pytest.raises(TimeoutError):
        await prepare_verified_remote_module_attempt(
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
            executor,
            cast(Any, object()),
            10.0,
            cast(Any, operation),
            clock=lambda: 10.0,
        )
    assert not reached
    executor.shutdown()


@pytest.mark.anyio
async def test_missing_resource_bound_authority_reaches_no_verifier() -> None:
    executor = RemoteModulePreparationExecutor()
    with pytest.raises(CategorizedError) as caught:
        await prepare_verified_remote_module_attempt(
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
            executor,
            None,
            10.0,
            cast(Any, lambda: None),
            clock=lambda: 9.0,
        )
    assert caught.value.category is ErrorCategory.CONFLICT
    executor.shutdown()
