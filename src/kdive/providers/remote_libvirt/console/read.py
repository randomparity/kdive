"""Worker-side strict console read for remote-libvirt (ADR-0429).

A remote System's console is streamed out-of-band by a reconciler-resident
:class:`~kdive.providers.remote_libvirt.console.collector.ConsoleCollector` into rotating S3 parts
under a single-leader lock (``CONSOLE_HOSTING_LEADER``). The boot worker cannot reach that
in-process collector, so — like the boot-window snapshotter — this reader assembles the System's
already-uploaded parts itself. It exists because the boot snapshotter's best-effort contract is
wrong for a tool whose whole output is the console it just read (#1431, consumed by #1435):

- It reports ``pumped`` — whether a console-hosting leader is alive — so an un-pumped or
  unreachable console is distinguishable from a genuinely silent one. A worker read of the S3
  parts alone cannot tell the two apart; the leader-liveness probe (``pg_locks``) can.
- It does **not** swallow a part-store read failure the way the snapshotter does; an unreachable
  store propagates so an empty result never masquerades as a successful read of a silent console.
- It re-redacts the assembled bytes at the seam so the mandatory-redaction invariant holds here
  regardless of how the parts were produced (and catches a secret registered after a part sealed).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from kdive.db.locks import CONSOLE_HOSTING_LEADER, session_advisory_lock_held
from kdive.providers.ports.console import ConsoleWindowRead
from kdive.providers.remote_libvirt.console.wiring import RemoteConsolePartStore
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

if TYPE_CHECKING:
    from psycopg import AsyncConnection

# The leader-liveness probe: reports whether any backend holds the named session advisory lock.
# Injected so the reader is unit-testable without a Postgres backend.
LeaderProbe = Callable[["AsyncConnection", str], Awaitable[bool]]


class _PartReader(Protocol):
    """The read-only slice of :class:`RemoteConsolePartStore` this reader needs."""

    def list_part_indices(self, system_id: UUID) -> list[int]: ...
    def assemble(self, system_id: UUID, start_index: int = 0) -> bytes: ...


class RemoteLibvirtConsoleReader:
    """Strict worker-side reader over a running remote System's S3 console parts (ADR-0429).

    Every host/store seam is injected so the reader is unit-testable without a libvirt host, an
    object store, or a Postgres backend. Use :func:`build_remote_console_reader` for the
    production instance.
    """

    def __init__(
        self,
        *,
        parts: _PartReader,
        secret_registry: SecretRegistry,
        leader_name: str = CONSOLE_HOSTING_LEADER,
        leader_probe: LeaderProbe = session_advisory_lock_held,
    ) -> None:
        self._parts = parts
        self._secret_registry = secret_registry
        self._leader_name = leader_name
        self._leader_probe = leader_probe

    async def read_window(
        self, conn: AsyncConnection, system_id: UUID, start_index: int = 0
    ) -> ConsoleWindowRead:
        """Read redacted console bytes for ``system_id`` over parts with index ``>= start_index``.

        The blocking store I/O runs in a worker thread. A store failure propagates (it is **not**
        swallowed): an empty return therefore always means the console was reachable but silent
        over the window, never that it could not be read. The leader-liveness probe runs after the
        read so ``pumped`` reflects a leader alive through the moment the window was assembled.
        """
        data, next_index = await asyncio.to_thread(self._read_parts, system_id, start_index)
        pumped = await self._leader_probe(conn, self._leader_name)
        return ConsoleWindowRead(self._redact(data), next_index, pumped)

    def _read_parts(self, system_id: UUID, start_index: int) -> tuple[bytes, int]:
        """List the window's part indices and assemble their bytes (blocking store I/O).

        ``next_index`` is the highest observed part index ``+ 1``, or the requested ``start_index``
        when the window is empty, so a poller's cursor never rewinds past where it asked to read.
        """
        indices = self._parts.list_part_indices(system_id)
        in_window = [index for index in indices if index >= start_index]
        next_index = (max(in_window) + 1) if in_window else start_index
        data = self._parts.assemble(system_id, start_index)
        return data, next_index

    def _redact(self, data: bytes) -> bytes:
        """Redact the assembled bytes before they leave the seam (ADR-0027, ADR-0429).

        The parts are already redacted at collection, but re-redacting here makes the
        mandatory-redaction invariant hold at the seam regardless of the parts' provenance and
        seeds from the current registry so a value registered after a part sealed is still caught.
        Non-UTF-8 console bytes decode with ``errors="replace"`` so a partial multibyte tail never
        raises.
        """
        redactor = Redactor(registry=self._secret_registry)
        return redactor.redact_text(data.decode("utf-8", "replace")).encode("utf-8")


def build_remote_console_reader(*, secret_registry: SecretRegistry) -> RemoteLibvirtConsoleReader:
    """Build the production remote console reader from the environment object store.

    ``conninfo`` is unused on the read path (``list_part_indices``/``assemble`` touch only the
    object store, never the database), so the part store is constructed with an empty one.
    """
    parts = RemoteConsolePartStore(object_store_from_env(), "")
    return RemoteLibvirtConsoleReader(parts=parts, secret_registry=secret_registry)
