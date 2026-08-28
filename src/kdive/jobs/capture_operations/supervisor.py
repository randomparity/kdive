"""Durable worker-authority supervision for capture child operations (ADR-0558)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.locks import CAPTURE_JOB_FENCE_KEY_SQL
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.capture_operations.launcher import (
    GatedCaptureLauncher,
    LaunchAbortEvidence,
    LaunchedCapture,
)
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.jobs.capture_operations.storage.repository import (
    CaptureExitOutcome,
    CaptureOperation,
    CaptureOperationIdentity,
    CaptureOperationSnapshot,
    CaptureProviderKind,
    RecoveryEvidence,
    acknowledge_exit,
    create_launching,
    mark_running,
    record_identity,
    record_spool_disposed,
    request_cancel,
)
from kdive.providers.ports.traffic import TrafficCaptureQuiescence, capture_qom_id

LOCK_PROBE_INTERVAL_SECONDS = 0.25
LOCK_PROBE_TIMEOUT_SECONDS = 1.0
_STATEMENT_TIMEOUT_MILLISECONDS = 1000
_log = logging.getLogger(__name__)
_CAPTURE_AUTHORITY_LOST: ContextVar[asyncio.Event | None] = ContextVar(
    "capture_authority_lost", default=None
)


async def _wait_with_capture_authority[T](conn: AsyncConnection, operation: Awaitable[T]) -> T:
    """Return an operation result only while the lock session still holds authority."""
    operation_task = asyncio.ensure_future(operation)
    authority_task = asyncio.create_task(_monitor_lock_session(conn))
    try:
        done, _pending = await asyncio.wait(
            {operation_task, authority_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if authority_task in done:
            await authority_task
            raise AssertionError("lock monitor returned without losing authority")
        await asyncio.sleep(0)
        if authority_task.done():
            await authority_task
            raise AssertionError("lock monitor returned without losing authority")
        await require_capture_authority()
        return operation_task.result()
    finally:
        for task in (operation_task, authority_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation_task, authority_task, return_exceptions=True)


@dataclass(frozen=True, slots=True)
class CaptureSnapshot:
    """Provider and publication facts frozen under the handler's Run lock."""

    provider_kind: CaptureProviderKind
    resource_id: UUID
    system_id: UUID
    domain_name: str
    project: str
    write_remediation: str
    configuration: Callable[[], bytes]
    quiescence: Callable[[bytes], TrafficCaptureQuiescence]


@dataclass(frozen=True, slots=True)
class _PreLaunch:
    operation: CaptureOperation | None = None


@dataclass(frozen=True, slots=True)
class _LaunchAborted:
    operation: CaptureOperation
    evidence: LaunchAbortEvidence


@dataclass(frozen=True, slots=True)
class _Launched:
    operation: CaptureOperation
    capture: LaunchedCapture
    configuration: bytes | None = None


@dataclass(frozen=True, slots=True)
class _Acknowledged:
    operation: CaptureOperation
    capture: LaunchedCapture


type _ExecutionState = _PreLaunch | _LaunchAborted | _Launched | _Acknowledged


class CaptureAuthorityLost(RuntimeError):
    """The heartbeat or lock-owning session stopped authorizing provider work."""


class CapturePublisher(Protocol):
    """Publish bytes for the exact exited capture operation."""

    async def __call__(
        self,
        conn: AsyncConnection,
        job: Job,
        operation: CaptureOperation,
        snapshot: CaptureSnapshot,
        data: bytes,
    ) -> UUID: ...


class CapturePublicationRecoverer(Protocol):
    """Close publication for an exited attempt before its failure is propagated."""

    async def __call__(
        self,
        conn: AsyncConnection,
        operation: CaptureOperation,
    ) -> CaptureOperation: ...


@contextmanager
def capture_authority_scope(lost: asyncio.Event) -> Iterator[None]:
    """Bind worker heartbeat/stop authority to one capture handler task."""
    token = _CAPTURE_AUTHORITY_LOST.set(lost)
    try:
        yield
    finally:
        _CAPTURE_AUTHORITY_LOST.reset(token)


async def require_capture_authority() -> None:
    """Yield once so tied authority callbacks run, then reject lost worker authority."""
    lost = _CAPTURE_AUTHORITY_LOST.get()
    if lost is None:
        return
    await asyncio.sleep(0)
    if lost.is_set():
        raise CaptureAuthorityLost("capture worker authority ended")


async def _finish_owned_cleanup(task: asyncio.Task[None]) -> None:
    """Drain mandatory closure without letting repeated cancellation orphan it."""
    current = asyncio.current_task()
    assert current is not None
    completed = asyncio.Event()
    task.add_done_callback(lambda _task: completed.set())
    while not completed.is_set():
        try:
            await completed.wait()
        except asyncio.CancelledError:
            current.uncancel()
    task.result()


@asynccontextmanager
async def _capture_job_fence(conn: AsyncConnection, job_id: UUID) -> AsyncIterator[None]:
    await conn.execute(
        f"SELECT pg_advisory_lock({CAPTURE_JOB_FENCE_KEY_SQL})",  # noqa: S608
        (job_id,),
    )
    try:
        await conn.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MILLISECONDS}")
        yield
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("SET statement_timeout = 0")
        with contextlib.suppress(Exception):
            await conn.execute(
                f"SELECT pg_advisory_unlock({CAPTURE_JOB_FENCE_KEY_SQL})",  # noqa: S608
                (job_id,),
            )


async def _probe_lock_session(conn: AsyncConnection) -> None:
    try:
        async with asyncio.timeout(LOCK_PROBE_TIMEOUT_SECONDS):
            await conn.execute("SELECT 1")
    except Exception as error:
        raise CaptureAuthorityLost("capture operation lock session lost") from error


async def _monitor_lock_session(conn: AsyncConnection) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOCK_PROBE_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(max(0.0, deadline - loop.time()))
        await _probe_lock_session(conn)
        deadline += LOCK_PROBE_INTERVAL_SECONDS


def _identity(launched: LaunchedCapture) -> CaptureOperationIdentity:
    observed = launched.identity
    return CaptureOperationIdentity(
        host_instance=observed.host_instance,
        boot_id=observed.boot_id,
        pid=observed.pid,
        start_ticks=observed.start_ticks,
    )


def _repository_snapshot(
    snapshot: CaptureSnapshot, request: CaptureRequest
) -> CaptureOperationSnapshot:
    return CaptureOperationSnapshot(
        provider_kind=request.provider_kind,
        resource_id=snapshot.resource_id,
        system_id=snapshot.system_id,
        domain_name=snapshot.domain_name,
        request_digest=request.digest,
    )


def _authority_error() -> CategorizedError:
    return CategorizedError(
        "capture operation authority was lost",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"reason": "capture_authority_lost"},
    )


class CaptureOperationSupervisor:
    """Launch, fence, cancel, and acknowledge one exact capture provider process."""

    def __init__(
        self,
        *,
        launcher: GatedCaptureLauncher,
        credential: SecretStr,
        pool: AsyncConnectionPool | None = None,
    ) -> None:
        self._launcher = launcher
        self._credential = credential
        self._pool = pool

    @property
    def credential(self) -> SecretStr:
        """Expose the same secret reference to the co-assembled publication coordinator."""
        return self._credential

    def dispose_recovery_spool(self, operation_id: UUID) -> bool:
        """Remove and verify the exact operation-derived spool during startup recovery."""
        return self._launcher.dispose_operation_spool(operation_id)

    @asynccontextmanager
    async def _transition_connection(
        self, fallback: AsyncConnection
    ) -> AsyncIterator[AsyncConnection]:
        if self._pool is None:
            yield fallback
            return
        async with self._pool.connection() as connection:
            yield connection

    async def execute(
        self,
        conn: AsyncConnection,
        job: Job,
        snapshot: CaptureSnapshot,
        request: CaptureRequest,
        *,
        publisher: CapturePublisher,
        publication_recoverer: CapturePublicationRecoverer,
    ) -> UUID | None:
        """Execute and publish only while the exact job lock session remains responsive."""
        state: _ExecutionState = _PreLaunch()

        def record_launch_abort(evidence: LaunchAbortEvidence) -> None:
            nonlocal state
            assert isinstance(state, _PreLaunch) and state.operation is not None
            state = _LaunchAborted(state.operation, evidence)

        try:
            async with _capture_job_fence(conn, job.id):
                operation = await create_launching(
                    conn,
                    self._credential,
                    job.id,
                    job.attempt,
                    _repository_snapshot(snapshot, request),
                )
                state = _PreLaunch(operation)
                launched = await self._launcher.launch(
                    request, operation, on_abort=record_launch_abort
                )
                state = _Launched(operation, launched)
                await record_identity(conn, self._credential, operation.id, _identity(launched))
                configuration = snapshot.configuration()
                state = _Launched(operation, launched, configuration)
                launched.stage_configuration(configuration)
                await _probe_lock_session(conn)
                launched.release()
                await mark_running(conn, self._credential, operation.id)
                returncode = await self._wait_for_exit(conn, launched)
                await self._acknowledge(
                    conn,
                    operation,
                    snapshot,
                    configuration,
                    exit_outcome="completed",
                    exit_code=returncode,
                )
                state = _Acknowledged(operation, launched)
                data = self._consume_result(launched, request)
                return await self._wait_for_publication(
                    conn,
                    job,
                    operation,
                    snapshot,
                    launched,
                    data,
                    publisher,
                )
        except asyncio.CancelledError:
            await self._cleanup_failed_execution(
                conn,
                state,
                snapshot,
                publication_recoverer=publication_recoverer,
            )
            raise
        except CaptureAuthorityLost as error:
            await self._cleanup_failed_execution(
                conn,
                state,
                snapshot,
                publication_recoverer=publication_recoverer,
            )
            raise _authority_error() from error
        except Exception:
            await self._cleanup_failed_execution(
                conn,
                state,
                snapshot,
                publication_recoverer=publication_recoverer,
            )
            raise

    async def _cleanup_failed_execution(
        self,
        conn: AsyncConnection,
        state: _ExecutionState,
        snapshot: CaptureSnapshot,
        *,
        publication_recoverer: CapturePublicationRecoverer,
    ) -> None:
        if isinstance(state, _Acknowledged):
            cleanup = self._cleanup_publication(
                conn, state.operation, state.capture, publication_recoverer
            )
        elif isinstance(state, _Launched):
            cleanup = self._cleanup_launched(conn, state, snapshot)
        elif isinstance(state, _LaunchAborted):
            cleanup = self._cleanup_launch_abort(conn, state)
        else:
            return
        await _finish_owned_cleanup(asyncio.create_task(cleanup))

    async def _cleanup_publication(
        self,
        conn: AsyncConnection,
        operation: CaptureOperation,
        launched: LaunchedCapture,
        recoverer: CapturePublicationRecoverer,
    ) -> None:
        async with self._transition_connection(conn) as transition:
            recovered = await recoverer(transition, operation)
            disposed = await asyncio.to_thread(launched.dispose_spool)
            if not disposed:
                raise CategorizedError(
                    "capture publication recovery could not dispose its private spool",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={
                        "reason": "capture_spool_disposal_unverified",
                        "operation_id": str(operation.id),
                    },
                )
            await record_spool_disposed(transition, self._credential, recovered.id)

    async def _cleanup_launch_abort(
        self,
        conn: AsyncConnection,
        state: _LaunchAborted,
    ) -> None:
        try:
            evidence = state.evidence
            async with self._transition_connection(conn) as transition:
                await acknowledge_exit(
                    transition,
                    self._credential,
                    state.operation.id,
                    RecoveryEvidence(
                        process_absent=evidence.process_absent,
                        provider_quiescence=dict(evidence.provider_quiescence),
                        exit_outcome=evidence.exit_outcome,
                        exit_code=evidence.exit_code,
                    ),
                )
        except Exception as error:
            _log.warning(
                "capture operation %s cleanup did not complete (%s)",
                state.operation.id,
                type(error).__name__,
            )

    async def _cleanup_launched(
        self,
        conn: AsyncConnection,
        state: _Launched,
        snapshot: CaptureSnapshot,
    ) -> None:
        try:
            await self._cancel_and_acknowledge(
                conn,
                state.operation,
                state.capture,
                snapshot,
                state.configuration,
            )
        except Exception as error:
            _log.warning(
                "capture operation %s cleanup did not complete (%s)",
                state.operation.id,
                type(error).__name__,
            )

    async def _wait_for_exit(self, conn: AsyncConnection, launched: LaunchedCapture) -> int:
        return await _wait_with_capture_authority(conn, launched.wait_process())

    async def _wait_for_publication(
        self,
        conn: AsyncConnection,
        job: Job,
        operation: CaptureOperation,
        snapshot: CaptureSnapshot,
        launched: LaunchedCapture,
        data: bytes,
        publisher: CapturePublisher,
    ) -> UUID:
        return await _wait_with_capture_authority(
            conn,
            self._publish_and_dispose(conn, job, operation, snapshot, launched, data, publisher),
        )

    async def _publish_and_dispose(
        self,
        conn: AsyncConnection,
        job: Job,
        operation: CaptureOperation,
        snapshot: CaptureSnapshot,
        launched: LaunchedCapture,
        data: bytes,
        publisher: CapturePublisher,
    ) -> UUID:
        artifact_id = await publisher(conn, job, operation, snapshot, data)
        disposed = await asyncio.to_thread(launched.dispose_spool)
        if not disposed:
            raise CategorizedError(
                "capture publication committed but its private spool remains",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={
                    "reason": "capture_spool_disposal_unverified",
                    "operation_id": str(operation.id),
                },
            )
        await record_spool_disposed(conn, self._credential, operation.id)
        return artifact_id

    async def _cancel_and_acknowledge(
        self,
        conn: AsyncConnection,
        operation: CaptureOperation,
        launched: LaunchedCapture,
        snapshot: CaptureSnapshot,
        configuration: bytes | None,
    ) -> None:
        async with self._transition_connection(conn) as transition:
            transition_error: Exception | None = None
            try:
                await request_cancel(transition, self._credential, operation.id)
            except Exception as error:
                transition_error = error
            absent = await launched.cancel()
            if transition_error is not None:
                raise transition_error
            if not absent or configuration is None:
                return
            await self._acknowledge(
                transition,
                operation,
                snapshot,
                configuration,
                exit_outcome="canceled",
                exit_code=launched.returncode,
            )

    async def _acknowledge(
        self,
        conn: AsyncConnection,
        operation: CaptureOperation,
        snapshot: CaptureSnapshot,
        configuration: bytes,
        *,
        exit_outcome: CaptureExitOutcome,
        exit_code: int | None,
    ) -> None:
        probe = snapshot.quiescence(configuration)
        evidence = await asyncio.to_thread(
            probe.prove_absent,
            snapshot.resource_id,
            snapshot.domain_name,
            capture_qom_id(operation.job_id),
        )
        await acknowledge_exit(
            conn,
            self._credential,
            operation.id,
            RecoveryEvidence(
                process_absent=True,
                provider_quiescence=evidence.as_dict(),
                exit_outcome=exit_outcome,
                exit_code=exit_code,
            ),
        )

    @staticmethod
    def _consume_result(launched: LaunchedCapture, request: CaptureRequest) -> bytes:
        result = launched.read_result()
        if result.outcome == "failure":
            assert result.error_category is not None and result.terminal is not None
            raise CategorizedError(
                result.reason or "capture provider execution failed",
                category=result.error_category,
                terminal=result.terminal,
                details=cast(dict[str, object], result.details),
            )
        data = launched.read_capture(request.max_bytes)
        if len(data) != result.size_bytes:
            raise CategorizedError(
                "capture child result size does not match its private pcap",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"reason": "capture_result_size_mismatch"},
            )
        return data
