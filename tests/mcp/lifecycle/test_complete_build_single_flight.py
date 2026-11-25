"""runs.complete_build joins a Run's in-flight finalize instead of restarting it (ADR-0675).

The validator blocks on an event, which stands in for a scan longer than the client's request
timeout. Each join test waits for the handler's join log line before it releases the scan, so a
caller that returned through the recorded-result fast path cannot pass as a joiner.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import AsyncExitStack
from typing import Any, cast
from uuid import UUID

import psycopg
import pytest
from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from psycopg.types.json import Jsonb

import kdive.mcp.tools.lifecycle.runs.registrar as run_registrar
import kdive.services.runs.complete_build as build_service
from kdive.db import migrate
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.runs.complete_build import CompleteBuildHandlers
from kdive.services.runs.steps import BuildStepResult

# Reuse the existing fixture owner for the fresh peer database lifecycle.
from tests.db.conftest import (
    _cluster_global_role_lock,  # noqa: PLC2701
    _drop_worker_db,  # noqa: PLC2701
    _provision_worker_db,  # noqa: PLC2701
    _server_url_without_db,  # noqa: PLC2701
)
from tests.mcp.complete_build_support import (
    FakeValidator,
    build_output,
    ctx,
    pool,
    seed_external_run_with_manifest,
)
from tests.mcp.systems_support import provider_resolver

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
            task = handlers._in_flight.get(run_id)
            validator.release.set()
            if task is not None:
                await asyncio.wait({task}, timeout=_WAIT_S)

            assert await _run_state(conn_pool, run_id) is RunState.SUCCEEDED
            assert run_id not in handlers._in_flight

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


def test_uncategorized_failure_reaches_joiners_and_next_call_retries(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            validator = _BlockingValidator(run_id, first_error=RuntimeError("scan crashed"))
            handlers = CompleteBuildHandlers(validate_complete_build=validator)

            first = _call(handlers, conn_pool, run_id)
            await _started(validator)
            joiner = _call(handlers, conn_pool, run_id)
            await _joined(caplog)
            validator.release.set()
            outcomes = await asyncio.gather(first, joiner, return_exceptions=True)

            assert [str(o) for o in outcomes] == ["scan crashed"] * 2
            assert all(isinstance(o, RuntimeError) for o in outcomes)
            assert run_id not in handlers._in_flight
            retry = await _call(handlers, conn_pool, run_id)
            assert retry.status == "succeeded"
            assert validator.calls == 2

    asyncio.run(_run())


def test_failure_with_every_caller_cancelled_logs_one_error(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The finalize's own failure log is the only ERROR record, even with no caller awaiting."""
    caplog.set_level(logging.INFO)

    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            validator = _BlockingValidator(run_id, first_error=RuntimeError("scan crashed"))
            handlers = CompleteBuildHandlers(validate_complete_build=validator)

            first = _call(handlers, conn_pool, run_id)
            await _started(validator)
            joiner = _call(handlers, conn_pool, run_id)
            await _joined(caplog)
            task = handlers._in_flight[run_id]
            await _cancelled(first)
            await _cancelled(joiner)
            validator.release.set()
            await asyncio.wait({task}, timeout=_WAIT_S)
            await asyncio.sleep(0)

            assert task.done()
            errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert [r.getMessage() for r in errors] == [
                f"runs.complete_build finalize for run {run_id} failed"
            ]

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
            handlers = CompleteBuildHandlers(validate_complete_build=validator)
            stale: asyncio.Future[ToolResponse] = asyncio.get_running_loop().create_future()
            stale.set_result(
                ToolResponse.failure(str(run_id), ErrorCategory.CONFIGURATION_ERROR, detail="stale")
            )
            handlers._in_flight[run_id] = cast("asyncio.Task[ToolResponse]", stale)
            try:
                result = await _start(handlers, conn_pool, run_id)
            finally:
                handlers._in_flight.pop(run_id, None)

            assert result.status == "succeeded"
            assert validator.calls == 1

    asyncio.run(_run())


@pytest.fixture(params=["shared", "separate"])
def peer_build_database(
    request: pytest.FixtureRequest, migrated_url: str, postgres_url: str
) -> Iterator[str]:
    if request.param == "shared":
        yield migrated_url
        return
    server = _server_url_without_db(postgres_url)
    peer_url, name = _provision_worker_db(server)
    try:
        with (
            _cluster_global_role_lock(postgres_url),
            psycopg.connect(peer_url, autocommit=True) as conn,
        ):
            migrate.apply_migrations(conn)
        yield peer_url
    finally:
        _drop_worker_db(server, name)


async def _registered_build_call(app: FastMCP, run_id: UUID) -> ToolResponse:
    tools = await app.list_tools()
    tool = next(tool for tool in tools if tool.name == "runs.complete_build")
    assert isinstance(tool, FunctionTool)
    response = await tool.fn(run_id=str(run_id), build_id=None, cmdline="c")
    assert isinstance(response, ToolResponse)
    return response


def test_registered_apps_isolate_finalization_and_join_retries(
    migrated_url: str,
    peer_build_database: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    async def run() -> None:
        monkeypatch.setattr(build_service, "_EXTERNAL_BUILD_VALIDATION_SLOTS", asyncio.Semaphore(2))
        async with AsyncExitStack() as stack:
            first_pool = await stack.enter_async_context(pool(migrated_url))
            peer_pool = (
                first_pool
                if peer_build_database == migrated_url
                else await stack.enter_async_context(pool(peer_build_database))
            )
            run_id = await seed_external_run_with_manifest(first_pool)
            if peer_pool is not first_pool:
                assert await seed_external_run_with_manifest(peer_pool, run_id=run_id) == run_id
            first_validator = _BlockingValidator(run_id)
            peer_validator = _BlockingValidator(run_id)
            created: list[CompleteBuildHandlers] = []

            def handlers_factory() -> CompleteBuildHandlers:
                validator = peer_validator if len(created) == 1 else first_validator
                handlers = CompleteBuildHandlers(validate_complete_build=validator)
                created.append(handlers)
                return handlers

            monkeypatch.setattr(run_registrar, "_complete_build_handlers", handlers_factory)
            monkeypatch.setattr(run_registrar, "current_context", ctx)
            first_app, peer_app = FastMCP("first-app"), FastMCP("peer-app")
            run_registrar._register_runs_complete_build(first_app, first_pool, provider_resolver())
            run_registrar._register_runs_complete_build(peer_app, peer_pool, provider_resolver())
            calls: list[asyncio.Task[ToolResponse]] = []
            try:
                calls.append(asyncio.create_task(_registered_build_call(first_app, run_id)))
                await _started(first_validator)
                calls.append(asyncio.create_task(_registered_build_call(peer_app, run_id)))
                calls.append(asyncio.create_task(_registered_build_call(first_app, run_id)))
                loop = asyncio.get_running_loop()
                deadline = loop.time() + _WAIT_S
                while not (peer_validator.started.is_set() and _joined_count(caplog)):
                    if not peer_validator.started.is_set():
                        assert _joined_count(caplog) < 2, "another app joined the first app's task"
                    else:
                        assert len(created) == 2, "a retry constructed a request-local handler"
                    assert loop.time() < deadline, (
                        "independent validation or local join never started"
                    )
                    await asyncio.sleep(0.01)
                assert _joined_count(caplog) == 1
                assert len(created) == 2
            finally:
                first_validator.release.set()
                peer_validator.release.set()
                outcomes = await asyncio.gather(*calls, return_exceptions=True)
            assert len(outcomes) == 3
            assert all(
                isinstance(item, ToolResponse) and item.status == "succeeded" for item in outcomes
            )
            assert outcomes[0] == outcomes[2]
            assert first_validator.calls == peer_validator.calls == 1
            assert all(run_id not in item._in_flight for item in created)
            assert await _build_rows(first_pool, run_id) == 1
            assert await _build_rows(peer_pool, run_id) == 1
            assert await _run_state(first_pool, run_id) is RunState.SUCCEEDED
            assert await _run_state(peer_pool, run_id) is RunState.SUCCEEDED

    asyncio.run(run())


def test_stale_callback_retains_a_newer_finalize() -> None:
    async def run() -> None:
        handlers = CompleteBuildHandlers()
        uid = UUID(int=1)
        response = ToolResponse.success(str(uid), "succeeded")
        old = asyncio.create_task(asyncio.sleep(0, result=response))
        newer = asyncio.create_task(asyncio.sleep(0, result=response))
        try:
            await old
            handlers._in_flight[uid] = newer
            handlers._forget(uid, old)
            assert handlers._in_flight[uid] is newer
            await newer
            handlers._forget(uid, newer)
            assert uid not in handlers._in_flight
        finally:
            await asyncio.gather(old, newer)
            handlers._in_flight.pop(uid, None)

    asyncio.run(run())
