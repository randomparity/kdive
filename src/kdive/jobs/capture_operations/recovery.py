"""Worker-startup recovery for durable capture operations (ADR-0558)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.jobs.capture_operations.process.linux_identity import LinuxIdentity, scan_launch_token
from kdive.jobs.capture_operations.storage.publication import (
    CapturePublicationIdentityConflict,
    recover_publication,
)
from kdive.jobs.capture_operations.storage.repository import (
    CaptureOperation,
    CaptureRecoveryCandidate,
    RecoveryEvidence,
    claim_publication_recovery,
    list_recovery_candidates,
    record_spool_disposed,
    recover_operation,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.traffic import TrafficCaptureQuiescence, capture_qom_id
from kdive.store.objectstore import ObjectStore

_SIGNAL_WAIT_SECONDS = 5.0
_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    """Startup recovery counts; any pending operation bars readiness and claims."""

    scanned: int
    recovered: int
    pending: int


async def _wait_for_pidfd_ready(pidfd: int) -> None:
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
        await asyncio.wait_for(_wait_for_pidfd_ready(pidfd), timeout=seconds)
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
    dispose_spool: Callable[[UUID], bool],
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
                if not await asyncio.to_thread(dispose_spool, operation.id):
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
            except Exception:
                _log.exception("capture operation %s recovery remains pending", candidate.id)
    return RecoverySummary(
        scanned=len(candidates),
        recovered=recovered,
        pending=len(candidates) - recovered,
    )
