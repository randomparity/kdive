"""Provider-owned console reads and boot snapshots (ADR-0235, ADR-0429, ADR-0864).

Local files and remote object-store parts use explicit collaborators. Missing ports never imply
a storage backend. Snapshot registration is best-effort at the boot caller, except for a typed
fatal source-read failure. Control callers redact buffers before persistence or response, retaining
local raw byte offsets; remote readers additionally redact at their own seam (ADR-0429).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol
from uuid import UUID

from kdive.domain.errors import CategorizedError

if TYPE_CHECKING:
    from psycopg import AsyncConnection


class ConsoleSnapshotReadError(CategorizedError):
    """Fatal console-source read failure, distinct from best-effort snapshot registration."""


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
        :meth:`snapshot` as ``start_index`` so only this boot's parts are assembled. A part-store
        failure propagates; the boot handler logs it and uses mark ``0`` (cumulative — the
        pre-slicing behavior).
        """
        ...

    async def snapshot(
        self, conn: AsyncConnection, system_id: UUID, run_id: UUID, start_index: int = 0
    ) -> ConsoleSnapshot | None:
        """Assemble this boot window's console and write a per-Run ``console-<run>`` artifact.

        ``start_index`` (the mark from :meth:`mark_boot_window`) slices to one boot window: only
        parts with index ``>= start_index`` are assembled (ADR-0241). Default ``0`` is the whole
        history. The artifact row is written on ``conn`` so it commits atomically with the boot
        step. Returns ``None`` when no console bytes are available for the window yet. A part-store
        or database failure propagates to the boot handler, which owns the best-effort boundary so
        registration cannot fail the boot. ConsoleSnapshotReadError identifies a fatal source
        read and preserves its categorized operator remediation instead of degrading to no data.
        """
        ...


class ConsoleWindowRead(NamedTuple):
    """Internal console buffer and provider-specific cursor/freshness, never a safe-output type.

    Control consumers must redact ``data`` before publishing or persisting it. Local data remains
    raw so capture byte offsets retain their meaning; remote data is also redacted by its reader.
    Remote ``next_index`` is the highest observed part index plus one, or the requested index for
    an empty window. Local reads are cumulative: start/next index is always zero. ``pumped`` is
    true after a successful local read, or when the remote console-hosting leader is alive. False
    means an un-pumped source, rather than a silent console; each control caller owns that policy.
    """

    data: bytes
    next_index: int
    pumped: bool


class ConsoleReader(Protocol):
    """Read a running System's console through its provider (ADR-0429, ADR-0864).

    Propagate source/database failures; do not return an empty successful result for failed I/O.
    A missing local log retains the existing empty-file behavior. Control consumers own final
    redaction and freshness policy; remote readers also re-redact assembled parts at their seam.
    """

    async def read_window(
        self, conn: AsyncConnection, system_id: UUID, start_index: int = 0
    ) -> ConsoleWindowRead:
        """Read console bytes and freshness; consumers must redact before output.

        Remote reads assemble parts at indices >= ``start_index`` and report a next-part cursor.
        Local reads accept only zero and return the cumulative raw file with next index zero;
        a nonzero local index raises ValueError, recoverable by requesting zero. Read failures
        propagate. Neither errors nor an un-pumped source imply a silent successful console.
        """
        ...
