"""Test identities for credential-gated worker claims."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import timedelta

import psycopg
from pydantic import SecretStr

from kdive.domain.operations.jobs import Job
from kdive.jobs import queue
from kdive.services.runs.worker_incarnations import CURRENT_WORKER_FENCE_PROTOCOL


def incarnation_credential(worker_id: str) -> SecretStr:
    """Return a deterministic current-test credential for one worker identity."""
    return SecretStr(hashlib.sha256(f"kdive-test:{worker_id}".encode()).hexdigest())


async def register_worker(
    conn: psycopg.AsyncConnection,
    worker_id: str,
    *,
    credential: SecretStr | None = None,
) -> SecretStr:
    """Register an active current-protocol test worker and return its credential."""
    credential = credential or incarnation_credential(worker_id)
    await conn.execute(
        "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
        "fence_protocol, credential_hash) VALUES "
        "(%s, 'local', '{}'::jsonb, %s, sha256(convert_to(%s, 'UTF8'))) "
        "ON CONFLICT (incarnation) DO NOTHING",
        (worker_id, CURRENT_WORKER_FENCE_PROTOCOL, credential.get_secret_value()),
    )
    return credential


async def dequeue_as_current_worker(
    conn: psycopg.AsyncConnection,
    worker_id: str,
    *,
    lease: timedelta = queue.DEFAULT_LEASE,
    accepted_lanes: Sequence[str] = queue.DEFAULT_DISPATCH_LANES,
) -> Job | None:
    """Register and claim as one active current-protocol test worker."""
    credential = await register_worker(conn, worker_id)
    return await queue.dequeue(
        conn,
        worker_id,
        incarnation_credential=credential,
        lease=lease,
        accepted_lanes=accepted_lanes,
    )
