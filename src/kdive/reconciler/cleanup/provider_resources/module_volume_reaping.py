"""Queue the worker-owned remote module-volume maintenance lane (ADR-0588)."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, RemoteModuleVolumeReapPayload

_AUTHORIZE = Authorizing(principal="remote-libvirt", project="remote-libvirt")
_DEDUP_KEY = "remote-module-volume-reap:v1"
_PAYLOAD = RemoteModuleVolumeReapPayload(schema="remote-module-volume-reap-v1")


async def enqueue_remote_module_volume_reap(conn: AsyncConnection) -> bool:
    """Ensure the one platform-internal worker sweep is queued or recycled."""
    _, admitted = await queue.enqueue_with_status(
        conn,
        JobKind.REMOTE_MODULE_VOLUME_REAP,
        _PAYLOAD,
        _AUTHORIZE,
        _DEDUP_KEY,
        recycle=queue.JobRecyclePolicy.TERMINAL_OR_CANCELED,
    )
    return admitted
