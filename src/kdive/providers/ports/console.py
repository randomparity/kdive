"""Console provider contracts (ADR-0235, ADR-0429).

A provider that captures its console out-of-band (e.g. remote-libvirt streams it to S3 parts from
a reconciler-resident collector) exposes a ``ConsoleSnapshotter`` so the boot worker can persist an
immutable per-Run console artifact at boot completion. Providers whose console is a worker-local
file (local-libvirt) leave the runtime's ``console_snapshotter`` unset and the boot handler
captures the file directly.

Separately, :class:`RemoteConsoleReader` is the strict-read counterpart used by a tool whose whole
output is the console it just read on a *running* System (post-SysRq capture, crash watch). Its
freshness and error contract deliberately differs from the best-effort boot-window
``ConsoleSnapshotter`` (ADR-0429): it reports whether the console is being pumped and does not
swallow a read failure, so an empty result is never mistaken for "the kernel printed nothing".
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


class ConsoleWindowRead(NamedTuple):
    """Redacted console bytes read over a part-index window on a running System, with freshness.

    Unlike :class:`ConsoleSnapshot` (a persisted per-Run artifact), this is a transient read for a
    live tool. The fields carry the freshness signal the boot snapshotter cannot:

    - ``data``: the redacted console bytes assembled from parts with index ``>= start_index``.
    - ``next_index``: the cursor a poller passes as ``start_index`` on its next read to receive
      only newer parts (the highest part index seen ``+ 1``, or the requested ``start_index`` when
      the window is empty, so a poll never rewinds).
    - ``pumped``: whether a console-hosting leader is currently pumping this System's console. When
      ``False`` the console source is un-pumped/unreachable, so empty ``data`` means "could not be
      read", **not** "the kernel printed nothing". When ``True``, empty ``data`` is a genuinely
      silent console.
    """

    data: bytes
    next_index: int
    pumped: bool


class RemoteConsoleReader(Protocol):
    """Read a running System's console over a part-index window (ADR-0429).

    The strict-read counterpart to :class:`ConsoleSnapshotter`'s best-effort boot-window contract,
    for a tool whose entire output is the console it just read. Its contract differs deliberately:

    - **Freshness.** Reads the object-store parts as of the call and reports ``pumped`` so the
      caller can tell an un-pumped/unreachable console from a genuinely silent one — a distinction
      the boot snapshotter cannot make (it returns ``None`` for both).
    - **Errors.** Does **not** swallow a part-store or database read failure the way the
      best-effort snapshotter does; an unreachable store propagates so an empty result never
      masquerades as a successful read of a silent console.
    - **Redaction.** The returned bytes pass the redactor at the seam, upholding the
      mandatory-redaction invariant regardless of how the underlying parts were produced.
    """

    async def read_window(
        self, conn: AsyncConnection, system_id: UUID, start_index: int = 0
    ) -> ConsoleWindowRead:
        """Read redacted console bytes for ``system_id`` over the part-index window.

        Assembles parts with index ``>= start_index`` into redacted bytes and reports whether a
        console-hosting leader is pumping the System (``pumped``) plus a ``next_index`` cursor for a
        subsequent poll. Propagates a store/database read failure rather than returning empty, so a
        caller can distinguish "could not read" from a silent console.
        """
        ...
