"""Attachment ownership and cancellation proofs without a database or GDB process."""

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.catalog.read_model import ArtifactReadRef
from kdive.domain.capacity.state import DebugSessionState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import DebugSession
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug.operations import runtime as ops
from kdive.mcp.tools.debug.operations.runtime import DebugEngineRuntime
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.debug import GdbController, GdbMiAttachment, GdbMiEngine
from kdive.providers.shared.debug_common.gdbmi.policy import debuginfo
from tests.mcp.debug.session_support import request_context


def test_reap_exits_before_removing_staged_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(debuginfo.tempfile, "tempdir", str(tmp_path))
    paths: list[Path] = []
    controller = Mock(spec=GdbController)
    controller.exit.side_effect = lambda: paths[0].read_bytes()
    attachment = GdbMiAttachment(controller, "127.0.0.1", 1, tmp_path / "transcript")

    def attach(path: Path) -> GdbMiAttachment:
        paths.append(path)
        return attachment

    resolver = debuginfo.DebuginfoResolver(
        read_debuginfo_ref=lambda _: ArtifactReadRef("key", None), fetch_object=lambda _: b"symbols"
    )
    debuginfo.stage_and_attach(run_id="r", attach=attach, resolver=resolver)
    runtime = DebugEngineRuntime(
        engine=cast(GdbMiEngine, Mock()), attach=Mock(), transcript_dir=tmp_path
    )
    runtime._registry.register("session", attachment)
    runtime.reap("session")
    controller.exit.assert_called_once()
    assert not paths[0].parent.exists()


def _owned(tmp_path: Path) -> tuple[DebugEngineRuntime, GdbMiAttachment]:
    root = tmp_path / "symbols"
    root.mkdir()
    attachment = GdbMiAttachment(
        Mock(spec=GdbController), "127.0.0.1", 1, tmp_path / "transcript", staging_dir=root
    )
    runtime = DebugEngineRuntime(
        engine=cast(GdbMiEngine, Mock()),
        attach=Mock(return_value=attachment),
        transcript_dir=tmp_path,
    )
    return runtime, attachment


def _session() -> DebugSession:
    return DebugSession(
        id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        run_id=uuid4(),
        state=DebugSessionState.LIVE,
        transport="gdbstub",
        transport_handle="gdbstub://127.0.0.1:1",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        principal="test",
        project="test",
    )


def _owner(runtime: DebugEngineRuntime) -> ops.DebugRuntimeResolver:
    owner = ops.DebugRuntimeResolver(cast(ProviderResolver, Mock()))
    owner._runtimes[(ResourceKind.LOCAL_LIBVIRT, None)] = runtime
    return owner


def _pool(row: tuple[str] | None = None) -> AsyncConnectionPool:
    pool = MagicMock()
    connection, cursor = MagicMock(), MagicMock()
    pool.connection.return_value.__aenter__.return_value = connection
    connection.cursor.return_value.__aenter__.return_value = cursor
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=row)
    return cast(AsyncConnectionPool, pool)


@pytest.mark.parametrize("exit_fails", [False, True])
def test_reap_retains_owner_until_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_fails: bool
) -> None:
    runtime, attachment = _owned(tmp_path)
    runtime._registry.register("s", attachment)
    controller = cast(Mock, attachment.controller)
    if exit_fails:
        controller.exit.side_effect = [OSError("synthetic"), None]
    else:
        original = ops.shutil.rmtree
        failures = iter([True, False])

        def remove(path: Path) -> None:
            if next(failures):
                raise OSError("synthetic")
            original(path)

        monkeypatch.setattr(ops.shutil, "rmtree", remove)
    with pytest.raises(ops.ReapFailure) as failure:
        runtime.reap("s")
    assert isinstance(failure.value.__cause__, OSError) and failure.value.halted
    assert runtime._registry.get("s") is attachment
    assert attachment.staging_dir is not None and attachment.staging_dir.exists()
    runtime.reap("s")
    assert runtime._registry.get("s") is None and not attachment.staging_dir.exists()
    assert controller.exit.call_count == (2 if exit_fails else 1)


@pytest.mark.parametrize("row", [None, ("detached",), ("live",)])
def test_owner_sweep_observes_durable_state(tmp_path: Path, row: tuple[str] | None) -> None:
    runtime, attachment = _owned(tmp_path)
    sid = str(uuid4())
    runtime._registry.register(sid, attachment)
    asyncio.run(_owner(runtime).sweep_once(_pool(row)))
    assert attachment.staging_dir is not None
    assert attachment.staging_dir.exists() == (row == ("live",))


def test_owner_outage_and_pending_retry(tmp_path: Path) -> None:
    runtime, attachment = _owned(tmp_path)
    sid = str(uuid4())
    runtime._registry.register(sid, attachment)
    pool = _pool()
    cast(MagicMock, pool).connection.side_effect = OSError("synthetic")
    owner = _owner(runtime)
    asyncio.run(owner.sweep_once(pool))
    assert runtime._registry.get(sid) is attachment
    attachment.cleanup_pending = True
    asyncio.run(owner.sweep_once(pool))
    assert runtime._registry.get(sid) is None


@pytest.mark.parametrize("variant", ["upper", "hex"])
def test_variant_identity_cancellation_and_waiters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    async def check() -> None:
        runtime, attachment = _owned(tmp_path)
        session = _session()
        key = str(session.id)
        monkeypatch.setattr(ops, "_live_session", AsyncMock(return_value=session))
        entered, release = threading.Event(), threading.Event()

        def work(_engine: GdbMiEngine, _attachment: GdbMiAttachment) -> ToolResponse:
            entered.set()
            assert release.wait(30)
            return ToolResponse.success(key, "live")

        raw = key.upper() if variant == "upper" else session.id.hex
        task = asyncio.create_task(
            ops.run_engine_op_with_runtime(_pool(), request_context(), raw, runtime, work)
        )
        waiter = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            lock = runtime.lock_for(key)
            assert lock.locked()

            async def reap() -> None:
                async with lock:
                    runtime.reap(key)

            waiter = asyncio.create_task(reap())
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not waiter.done() and lock.locked()
            assert runtime.lock_for(key) is lock
        finally:
            release.set()
            if not task.done() and not task.cancelling():
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            if waiter is not None:
                await waiter
        assert attachment.staging_dir is not None and not attachment.staging_dir.exists()

    asyncio.run(check())


def test_shutdown_drains_unpublished_first_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        runtime, attachment = _owned(tmp_path)
        entered, release = threading.Event(), threading.Event()

        def attach(**_kwargs: object) -> GdbMiAttachment:
            entered.set()
            assert release.wait(30)
            return attachment

        runtime._attach = attach
        session = _session()
        owner = _owner(runtime)
        swept = threading.Event()
        original_sweep = owner.sweep_once

        async def sweep_once(pool: AsyncConnectionPool, *, shutdown: bool = False) -> None:
            if shutdown:
                swept.set()
            await original_sweep(pool, shutdown=shutdown)

        monkeypatch.setattr(owner, "sweep_once", sweep_once)
        context = owner.lifespan(_pool(("live",)))
        await context.__aenter__()
        running = asyncio.create_task(
            runtime.run(session, lambda _e, _a: ToolResponse.success("s", "live"))
        )
        closing = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not owner.owned()
            closing = asyncio.create_task(context.__aexit__(None, None, None))
            # Without the drain, shutdown's final sweep would run (and find nothing) at once.
            assert not await asyncio.to_thread(swept.wait, 0.5)
            assert not closing.done()
        finally:
            release.set()
            await running
            if closing is not None:
                await closing
            else:
                await context.__aexit__(None, None, None)
        assert attachment.staging_dir is not None and not attachment.staging_dir.exists()
        with pytest.raises(CategorizedError):
            await runtime.run(session, lambda _e, _a: ToolResponse.success("s", "live"))

    asyncio.run(check())


def test_repeated_live_cleanup_failure_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, attachment = _owned(tmp_path)
    session = _session()
    assert attachment.staging_dir is not None
    runtime._pending_roots[str(session.id)] = attachment.staging_dir
    monkeypatch.setattr(ops.shutil, "rmtree", Mock(side_effect=OSError("synthetic")))
    for _ in range(2):
        with pytest.raises(CategorizedError) as failure:
            runtime.attach_or_reuse(session)
        response = ops._op_failure(str(session.id), failure.value)
        assert response.error_category == "infrastructure_failure"
        assert response.data["reason"] == "debug_cleanup_pending"
        assert response.suggested_next_actions == ["debug.end_session", "debug.get_session"]
    assert attachment.staging_dir.exists() and runtime._pending_roots


def test_failed_halt_survives_a_failed_cleanup(tmp_path: Path) -> None:
    runtime, attachment = _owned(tmp_path)
    attachment.execution = "running"
    cast(Mock, runtime.engine).interrupt.side_effect = OSError("synthetic halt")
    cast(Mock, attachment.controller).exit.side_effect = OSError("synthetic exit")
    runtime._registry.register("session", attachment)
    with pytest.raises(ops.ReapFailure) as failure:
        runtime.reap("session")
    assert failure.value.halted is False
    assert attachment.staging_dir is not None and attachment.staging_dir.exists()
    assert runtime._registry.get("session") is attachment


def test_cancelled_caller_still_retrieves_the_task_error() -> None:
    async def check() -> None:
        started = asyncio.Event()

        async def work() -> None:
            started.set()
            await asyncio.sleep(0.05)
            raise OSError("synthetic")

        task = asyncio.ensure_future(work())
        waiter = asyncio.create_task(ops._finish_task(task))
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert task.done()
        assert task._log_traceback is False  # Retrieved, so no "never retrieved" log at GC.

    asyncio.run(check())
