"""Durable worker-authority supervision for capture child operations (ADR-0558)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.capture_operations.launcher import (
    GatedCaptureLauncher,
    LaunchAbortEvidence,
    LaunchedCapture,
)
from kdive.jobs.capture_operations.linux_identity import LinuxIdentity, scan_launch_token
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.jobs.capture_operations.publication import (
    CapturePublicationIdentityConflict,
    recover_publication,
)
from kdive.jobs.capture_operations.repository import (
    CaptureOperation,
    CaptureOperationIdentity,
    CaptureOperationSnapshot,
    CaptureProviderKind,
    CaptureRecoveryCandidate,
    RecoveryEvidence,
    acknowledge_exit,
    claim_publication_recovery,
    create_launching,
    list_recovery_candidates,
    mark_running,
    record_identity,
    record_spool_disposed,
    recover_operation,
    request_cancel,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.traffic import TrafficCaptureQuiescence, capture_qom_id
from kdive.store.objectstore import ObjectStore

LOCK_PROBE_INTERVAL_SECONDS = 0.25
LOCK_PROBE_TIMEOUT_SECONDS = 1.0
_STATEMENT_TIMEOUT_MILLISECONDS = 1000
_SIGNAL_WAIT_SECONDS = 5.0
_log = logging.getLogger(__name__)
_CAPTURE_AUTHORITY_LOST: ContextVar[asyncio.Event | None] = ContextVar(
    "capture_authority_lost", default=None
)


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


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    """Startup recovery counts; any pending operation bars readiness and claims."""

    scanned: int
    recovered: int
    pending: int


@asynccontextmanager
async def _capture_job_fence(conn: AsyncConnection, job_id: UUID) -> AsyncIterator[None]:
    await conn.execute(
        "SELECT pg_advisory_lock(hashtextextended('kdive:job:' || %s::text, 1951))",
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
                "SELECT pg_advisory_unlock(hashtextextended('kdive:job:' || %s::text, 1951))",
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
        launched: LaunchedCapture | None = None
        operation: CaptureOperation | None = None
        configuration: bytes | None = None
        launch_abort: LaunchAbortEvidence | None = None
        acknowledged = False

        def record_launch_abort(evidence: LaunchAbortEvidence) -> None:
            nonlocal launch_abort
            launch_abort = evidence

        try:
            async with _capture_job_fence(conn, job.id):
                operation = await create_launching(
                    conn,
                    self._credential,
                    job.id,
                    job.attempt,
                    _repository_snapshot(snapshot, request),
                )
                launched = await self._launcher.launch(
                    request, operation, on_abort=record_launch_abort
                )
                await record_identity(conn, self._credential, operation.id, _identity(launched))
                configuration = snapshot.configuration()
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
                acknowledged = True
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
            if acknowledged:
                assert operation is not None and launched is not None
                await _finish_owned_cleanup(
                    asyncio.create_task(
                        self._cleanup_publication(conn, operation, launched, publication_recoverer)
                    )
                )
            else:
                await self._cleanup_started(
                    conn, operation, launched, launch_abort, snapshot, configuration
                )
            raise
        except CaptureAuthorityLost as error:
            if acknowledged:
                assert operation is not None and launched is not None
                await _finish_owned_cleanup(
                    asyncio.create_task(
                        self._cleanup_publication(conn, operation, launched, publication_recoverer)
                    )
                )
            else:
                await self._cleanup_started(
                    conn, operation, launched, launch_abort, snapshot, configuration
                )
            raise _authority_error() from error
        except Exception:
            if acknowledged:
                assert operation is not None and launched is not None
                await _finish_owned_cleanup(
                    asyncio.create_task(
                        self._cleanup_publication(conn, operation, launched, publication_recoverer)
                    )
                )
            else:
                await self._cleanup_started(
                    conn, operation, launched, launch_abort, snapshot, configuration
                )
            raise

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

    async def _cleanup_started(
        self,
        conn: AsyncConnection,
        operation: CaptureOperation | None,
        launched: LaunchedCapture | None,
        launch_abort: LaunchAbortEvidence | None,
        snapshot: CaptureSnapshot,
        configuration: bytes | None,
    ) -> None:
        if operation is None:
            return
        try:
            if launched is None:
                if launch_abort is None:
                    return
                async with self._transition_connection(conn) as transition:
                    await acknowledge_exit(
                        transition,
                        self._credential,
                        operation.id,
                        RecoveryEvidence(
                            process_absent=launch_abort.process_absent,
                            provider_quiescence=dict(launch_abort.provider_quiescence),
                            exit_outcome=launch_abort.exit_outcome,
                            exit_code=launch_abort.exit_code,
                        ),
                    )
                return
            await self._cancel_and_acknowledge(conn, operation, launched, snapshot, configuration)
        except Exception as error:
            _log.warning(
                "capture operation %s cleanup did not complete (%s)",
                operation.id,
                type(error).__name__,
            )

    async def _wait_for_exit(self, conn: AsyncConnection, launched: LaunchedCapture) -> int:
        process = asyncio.create_task(launched.wait_process())
        authority = asyncio.create_task(_monitor_lock_session(conn))
        try:
            done, _pending = await asyncio.wait(
                {process, authority}, return_when=asyncio.FIRST_COMPLETED
            )
            if authority in done:
                await authority
                raise AssertionError("lock monitor returned without losing authority")
            await asyncio.sleep(0)
            if authority.done():
                await authority
                raise AssertionError("lock monitor returned without losing authority")
            await require_capture_authority()
            return process.result()
        finally:
            for task in (process, authority):
                if not task.done():
                    task.cancel()
            await asyncio.gather(process, authority, return_exceptions=True)

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
        publication = asyncio.create_task(
            self._publish_and_dispose(conn, job, operation, snapshot, launched, data, publisher)
        )
        authority = asyncio.create_task(_monitor_lock_session(conn))
        try:
            done, _pending = await asyncio.wait(
                {publication, authority}, return_when=asyncio.FIRST_COMPLETED
            )
            if authority in done:
                await authority
                raise AssertionError("lock monitor returned without losing authority")
            await asyncio.sleep(0)
            if authority.done():
                await authority
                raise AssertionError("lock monitor returned without losing authority")
            await require_capture_authority()
            return publication.result()
        finally:
            for task in (publication, authority):
                if not task.done():
                    task.cancel()
            await asyncio.gather(publication, authority, return_exceptions=True)

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
        exit_outcome: str,
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


async def _pidfd_ready(pidfd: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def mark_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(pidfd, mark_ready)
    try:
        await ready
    finally:
        loop.remove_reader(pidfd)


async def _wait_identity(identity: LinuxIdentity, pidfd: int, seconds: float) -> bool:
    try:
        await asyncio.wait_for(_pidfd_ready(pidfd), timeout=seconds)
    except TimeoutError:
        return identity.is_absent(current_host_instance=identity.host_instance)
    return True


async def _terminate_identity(identity: LinuxIdentity) -> bool:
    try:
        pidfd = identity.open_pidfd(current_host_instance=identity.host_instance)
    except ProcessLookupError:
        return True
    try:
        with contextlib.suppress(ProcessLookupError):
            identity.signal(pidfd, signal.SIGTERM)
        if await _wait_identity(identity, pidfd, _SIGNAL_WAIT_SECONDS):
            return True
        with contextlib.suppress(ProcessLookupError):
            identity.signal(pidfd, signal.SIGKILL)
        return await _wait_identity(identity, pidfd, _SIGNAL_WAIT_SECONDS)
    finally:
        os.close(pidfd)


async def _launching_evidence(candidate: CaptureRecoveryCandidate) -> RecoveryEvidence | None:
    if candidate.launch_token is None:
        return None
    matches = await asyncio.to_thread(
        scan_launch_token,
        candidate.launch_token,
        interpreter=Path(os.path.realpath(sys.executable)),
        host_instance=candidate.host_instance,
    )
    for identity in matches:
        if not await _terminate_identity(identity):
            return None
    remaining = await asyncio.to_thread(
        scan_launch_token,
        candidate.launch_token,
        interpreter=Path(os.path.realpath(sys.executable)),
        host_instance=candidate.host_instance,
    )
    if remaining:
        return None
    return RecoveryEvidence(
        process_absent=True,
        provider_quiescence={
            "evidence_kind": "closed_gate_boundary_token_scan_v1",
            "gate_closed": True,
            "boundary_scan_complete": True,
            "boundary_processes_absent": True,
            "host_instance": candidate.host_instance,
            "launch_token": candidate.launch_token,
            "launch_token_absent": True,
        },
        exit_outcome="aborted_before_identity",
        exit_code=None,
    )


async def _recovery_quiescence(
    conn: AsyncConnection,
    resolver: ProviderResolver,
    candidate: CaptureRecoveryCandidate,
) -> TrafficCaptureQuiescence:
    binding = await resolver.binding_for_system(conn, candidate.system_id)
    if binding.kind.value != candidate.provider_kind:
        raise RuntimeError("capture recovery provider binding changed")
    operation_ports = binding.runtime.traffic_capture_operation
    if operation_ports is None:
        raise RuntimeError("capture recovery provider does not support quiescence")
    configuration = operation_ports.configuration(candidate.resource_id)
    return operation_ports.quiescence(configuration)


async def _recover_identified(
    conn: AsyncConnection,
    resolver: ProviderResolver,
    host_identity: str,
    credential: SecretStr,
    candidate: CaptureRecoveryCandidate,
) -> CaptureOperation | None:
    if candidate.host_instance != host_identity:
        return None
    if candidate.boot_id is None or candidate.pid is None or candidate.start_ticks is None:
        return None
    identity = LinuxIdentity(
        host_instance=candidate.host_instance,
        boot_id=candidate.boot_id,
        pid=candidate.pid,
        start_ticks=candidate.start_ticks,
    )
    absent = identity.is_absent(current_host_instance=host_identity)
    if not absent:
        absent = await _terminate_identity(identity)
    if not absent:
        return None
    probe = await _recovery_quiescence(conn, resolver, candidate)
    evidence = await asyncio.to_thread(
        probe.prove_absent,
        candidate.resource_id,
        candidate.domain_name,
        capture_qom_id(candidate.job_id),
    )
    return await recover_operation(
        conn,
        credential,
        candidate.id,
        RecoveryEvidence(
            process_absent=True,
            provider_quiescence=evidence.as_dict(),
            exit_outcome="recovered",
            exit_code=None,
        ),
    )


async def recover_capture_operations(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    store: ObjectStore,
    supervisor: CaptureOperationSupervisor,
    host_identity: str,
    credential: SecretStr,
) -> RecoverySummary:
    """Recover every authority-eligible operation before worker readiness or claiming."""
    async with pool.connection() as conn:
        candidates = await list_recovery_candidates(conn, credential)
        recovered = 0
        for candidate in candidates:
            try:
                operation: CaptureOperation | None = None
                if candidate.state == "launching":
                    evidence = await _launching_evidence(candidate)
                    if evidence is None:
                        continue
                    operation = await recover_operation(conn, credential, candidate.id, evidence)
                elif candidate.state == "exited":
                    operation = await claim_publication_recovery(conn, credential, candidate.id)
                else:
                    operation = await _recover_identified(
                        conn, resolver, host_identity, credential, candidate
                    )
                if operation is None:
                    continue
                operation = await recover_publication(conn, store, credential, operation)
                disposed = await asyncio.to_thread(supervisor.dispose_recovery_spool, operation.id)
                if not disposed:
                    continue
                await record_spool_disposed(conn, credential, operation.id)
                recovered += 1
            except CapturePublicationIdentityConflict as error:
                _log.error(
                    "capture_publication_object_identity_conflict operation_id=%s key=%s reason=%s",
                    error.operation_id,
                    error.key,
                    error.reason,
                )
                continue
            except Exception:
                _log.exception("capture operation %s recovery remains pending", candidate.id)
                continue
    return RecoverySummary(
        scanned=len(candidates),
        recovered=recovered,
        pending=len(candidates) - recovered,
    )
