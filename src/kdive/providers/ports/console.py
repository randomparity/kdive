"""Per-Run console-snapshot provider contract (ADR-0235).

A provider that captures its console out-of-band (e.g. remote-libvirt streams it to S3 parts from
a reconciler-resident collector) exposes a ``ConsoleSnapshotter`` so the boot worker can persist an
immutable per-Run console artifact at boot completion. Providers whose console is a worker-local
file (local-libvirt) leave the runtime's ``console_snapshotter`` unset and the boot handler
captures the file directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from psycopg import AsyncConnection


class ConsoleSnapshot(NamedTuple):
    """A persisted per-Run console artifact: its row id, object key, and redacted bytes.

    ``data`` is returned so the boot handler can run crash-signature detection on the same bytes
    it persisted (ADR-0233 gates), without a second fetch.
    """

    id: UUID
    object_key: str
    data: bytes


class ConsoleSnapshotter(Protocol):
    """Persist an immutable per-Run console snapshot for a System's current boot."""

    async def snapshot(
        self, conn: AsyncConnection, system_id: UUID, run_id: UUID
    ) -> ConsoleSnapshot | None:
        """Assemble the console captured so far and write a per-Run ``console-<run>`` artifact.

        The artifact row is written on ``conn`` so it commits atomically with the boot step.
        Returns ``None`` when no console bytes are available yet. Never raises for an absent or
        partial console — console capture is best-effort and must not fail the boot.
        """
        ...
