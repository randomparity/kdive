"""Console provider contracts (ADR-0235, ADR-0429).

A provider that captures its console out-of-band (e.g. remote-libvirt streams it to S3 parts from
a reconciler-resident collector) exposes a ``ConsoleSnapshotter`` so the boot worker can persist an
immutable per-Run console artifact at boot completion. Providers whose console is a worker-local
file (local-libvirt) leave the runtime's ``console_snapshotter`` unset and the boot handler
captures the file directly.

Separately, :class:`RemoteConsoleReader` is the strict-read counterpart used by a tool whose whole
output is the console it just read on a *running* System (post-SysRq capture, crash watch). Its
freshness contract differs from the boot-window ``ConsoleSnapshotter`` (ADR-0429): it reports
whether the console is being pumped, while the boot handler owns the snapshotter's best-effort
failure boundary.
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
        console capture cannot fail the boot.
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

    The strict-read counterpart to :class:`ConsoleSnapshotter`, for a tool whose entire output is
    the console it just read. Its contract differs deliberately:

    - **Freshness.** Reads the object-store parts as of the call and reports ``pumped`` so the
      caller can tell an un-pumped/unreachable console from a genuinely silent one — a distinction
      the boot snapshotter cannot make (it returns ``None`` for both).
    - **Errors.** Propagates a part-store or database read failure. Unlike a boot snapshot, the
      caller has no best-effort boundary because the read itself is the tool's result; an empty
      result must never masquerade as a successful read of a silent console.
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
