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

    async def mark_boot_window(self, system_id: UUID) -> int:
        """Return the boot-window mark to record before the boot starts (ADR-0241).

        For a part-based collector this is the next part index (parts produced from now on belong
        to this boot). The boot handler reads it before ``booter.boot`` and passes it back to
        :meth:`snapshot` as ``start_index`` so only this boot's parts are assembled. Never raises:
        the handler treats a failure as mark ``0`` (cumulative — the pre-slicing behavior).
        """
        ...

    async def snapshot(
        self, conn: AsyncConnection, system_id: UUID, run_id: UUID, start_index: int = 0
    ) -> ConsoleSnapshot | None:
        """Assemble this boot window's console and write a per-Run ``console-<run>`` artifact.

        ``start_index`` (the mark from :meth:`mark_boot_window`) slices to one boot window: only
        parts with index ``>= start_index`` are assembled (ADR-0241). Default ``0`` is the whole
        history. The artifact row is written on ``conn`` so it commits atomically with the boot
        step. Returns ``None`` when no console bytes are available for the window yet. Never raises
        for an absent or partial console — capture is best-effort and must not fail the boot.
        """
        ...
