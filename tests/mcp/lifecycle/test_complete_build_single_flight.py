"""runs.complete_build joins a Run's in-flight finalize instead of restarting it (ADR-0675).

The validator blocks on an event, which stands in for a scan longer than the client's request
timeout. Each join test waits for the handler's join log line before it releases the scan, so a
caller that returned through the recorded-result fast path cannot pass as a joiner.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, cast

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.runs.complete_build import _IN_FLIGHT, CompleteBuildHandlers
from kdive.services.runs.steps import BuildStepResult
from tests.mcp.complete_build_support import (
    FakeValidator,
    build_output,
    ctx,
    pool,
    seed_external_run_with_manifest,
)

_JOINED = "joined the in-flight finalize"
_WAIT_S = 10.0


class _BlockingValidator(FakeValidator):
    """Hold each scan until ``release`` is set; optionally fail the first call."""

    def __init__(self, run_id: Any, *, first_error: Exception | None = None) -> None:
        super().__init__(build_output(run_id))
        self.started = threading.Event()
        self.release = threading.Event()
        self._first_error = first_error

    def __call__(self, manifest, keys, declared_build_id, *, arch: str = "x86_64"):
        self.started.set()
        if not self.release.wait(_WAIT_S):
            raise AssertionError("the test never released the scan")
        if self._first_error is not None and self.calls == 0:
            self.calls += 1
            raise self._first_error
        return super().__call__(manifest, keys, declared_build_id, arch=arch)


async def _started(validator: _BlockingValidator) -> None:
    assert await asyncio.to_thread(validator.started.wait, _WAIT_S), "the scan never started"


async def _joined(caplog: pytest.LogCaptureFixture) -> None:
    """Wait until a caller has parked on the in-flight finalize."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_S
    while not any(_JOINED in record.getMessage() for record in caplog.records):
        assert loop.time() < deadline, "no caller joined the in-flight finalize"
        await asyncio.sleep(0.01)


def _joined_count(caplog: pytest.LogCaptureFixture) -> int:
    return sum(_JOINED in record.getMessage() for record in caplog.records)


def _call(handlers: CompleteBuildHandlers, conn_pool: Any, run_id: Any) -> asyncio.Task[Any]:
    return asyncio.create_task(
        handlers.complete_build(conn_pool, ctx(), str(run_id), build_id=None, cmdline="c")
    )


async def _cancelled(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _build_rows(conn_pool: Any, run_id: Any) -> int:
    async with conn_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM run_steps WHERE run_id = %s AND step = 'build'", (run_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _run_state(conn_pool: Any, run_id: Any) -> RunState:
    async with conn_pool.connection() as conn:
        run = await RUNS.get(conn, run_id)
    assert run is not None
    return run.state


def _reason(response: ToolResponse) -> Any:
    return (response.data or {}).get("reason")


def test_retry_after_cancel_joins_running_finalize(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            validator = _BlockingValidator(run_id)
            handlers = CompleteBuildHandlers(validate_complete_build=validator)

            await _cancelled_after_start(handlers, conn_pool, run_id, validator)
            retry = _call(handlers, conn_pool, run_id)
            await _joined(caplog)
            validator.release.set()
            result = await retry

            assert result.status == "succeeded"
            assert validator.calls == 1
            assert _joined_count(caplog) == 1
            assert await _build_rows(conn_pool, run_id) == 1

    asyncio.run(_run())


async def _cancelled_after_start(
    handlers: CompleteBuildHandlers, conn_pool: Any, run_id: Any, validator: _BlockingValidator
) -> None:
    first = _call(handlers, conn_pool, run_id)
    await _started(validator)
    await _cancelled(first)


def test_cancelled_only_caller_still_commits(migrated_url: str) -> None:
    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            validator = _BlockingValidator(run_id)
            handlers = CompleteBuildHandlers(validate_complete_build=validator)

            await _cancelled_after_start(handlers, conn_pool, run_id, validator)
            task = _IN_FLIGHT.get(run_id)
            validator.release.set()
            if task is not None:
                await asyncio.wait({task}, timeout=_WAIT_S)

            assert await _run_state(conn_pool, run_id) is RunState.SUCCEEDED
            assert run_id not in _IN_FLIGHT

    asyncio.run(_run())


def test_failure_reaches_joiners_and_next_call_retries(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            error = CategorizedError("bad bundle", category=ErrorCategory.CONFIGURATION_ERROR)
            validator = _BlockingValidator(run_id, first_error=error)
            handlers = CompleteBuildHandlers(validate_complete_build=validator)

            first = _call(handlers, conn_pool, run_id)
            await _started(validator)
            joiner = _call(handlers, conn_pool, run_id)
            await _joined(caplog)
            validator.release.set()
            responses = await asyncio.gather(first, joiner)

            assert [r.status for r in responses] == ["error", "error"]
            assert [r.error_category for r in responses] == ["configuration_error"] * 2
            retry = await _call(handlers, conn_pool, run_id)
            assert retry.status == "succeeded"
            assert validator.calls == 2

    asyncio.run(_run())


def _remint(url: str, run_id: Any) -> None:
    """Reap and re-mint the window with a new deadline, as a concurrent agent would."""
    with psycopg.connect(url, autocommit=True) as other:
        other.execute(
            "DELETE FROM upload_manifests WHERE owner_kind = 'runs' AND owner_id = %s", (run_id,)
        )
        other.execute(
            "INSERT INTO upload_manifests (owner_kind, owner_id, prefix, manifest, deadline) "
            "VALUES ('runs', %s, %s, %s, now() + interval '1 hour')",
            (
                run_id,
                f"local/runs/{run_id}/",
                Jsonb([{"name": "kernel", "sha256": "c", "size_bytes": 1}]),
            ),
        )


def test_remint_during_joined_finalize_rejects_both(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            validator = _BlockingValidator(run_id)
            handlers = CompleteBuildHandlers(validate_complete_build=validator)

            first = _call(handlers, conn_pool, run_id)
            await _started(validator)
            await asyncio.to_thread(_remint, migrated_url, run_id)
            joiner = _call(handlers, conn_pool, run_id)
            await _joined(caplog)
            validator.release.set()
            responses = await asyncio.gather(first, joiner)

            assert [_reason(r) for r in responses] == ["upload_window_replaced"] * 2
            retry = await _call(handlers, conn_pool, run_id)
            assert retry.status == "succeeded"
            assert validator.calls == 2

    asyncio.run(_run())


async def _record_build(conn_pool: Any, run_id: Any) -> None:
    """Commit a finalize the way the publisher does: a build step and a succeeded Run."""
    async with conn_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s)",
            (run_id, Jsonb(BuildStepResult("recorded/kernel", None, "recorded-build").dump())),
        )
        await conn.execute("UPDATE runs SET state = 'succeeded' WHERE id = %s", (run_id,))


def _start(handlers: CompleteBuildHandlers, conn_pool: Any, run_id: Any) -> Any:
    """Enter the join step directly, as a caller whose authorize read preceded a commit."""
    return handlers._join_or_start(
        conn_pool, ctx(), run_id, str(run_id), build_id=None, cmdline="c", source_provenance=None
    )


def test_finalize_answers_a_commit_that_landed_after_authorize(migrated_url: str) -> None:
    """A finalize started after another one committed returns the recorded result."""

    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            validator = _BlockingValidator(run_id)
            await _record_build(conn_pool, run_id)

            result = await _start(
                CompleteBuildHandlers(validate_complete_build=validator), conn_pool, run_id
            )

            assert result.status == "succeeded"
            assert validator.calls == 0

    asyncio.run(_run())


def test_finished_task_in_the_map_starts_a_new_finalize(migrated_url: str) -> None:
    """A done task not yet dropped by its callback counts as absent (criterion 4)."""

    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            validator = _BlockingValidator(run_id)
            validator.release.set()
            stale: asyncio.Future[ToolResponse] = asyncio.get_running_loop().create_future()
            stale.set_result(
                ToolResponse.failure(str(run_id), ErrorCategory.CONFIGURATION_ERROR, detail="stale")
            )
            _IN_FLIGHT[run_id] = cast("asyncio.Task[ToolResponse]", stale)
            try:
                result = await _start(
                    CompleteBuildHandlers(validate_complete_build=validator), conn_pool, run_id
                )
            finally:
                _IN_FLIGHT.pop(run_id, None)

            assert result.status == "succeeded"
            assert validator.calls == 1

    asyncio.run(_run())
