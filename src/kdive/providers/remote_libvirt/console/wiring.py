"""Production wiring for the remote console collector (ADR-0095).

Binds the injectable :class:`ConsoleCollector` seams to the real libvirt console stream and the
object store: :class:`RemoteConsolePartStore` rotates numbered redacted parts under the
System's console prefix and, on finalize, assembles the single ``…/console`` artifact in the
shape `classify_console`/`read_console_log` expect (with its `artifacts` row), and
:func:`open_remote_console` opens a `virDomainOpenConsole` stream over the existing mutual-TLS
connection. All of this is provider-specific (outside the M2 portability core) and exercised on
the live remote spine — the injected-seam unit tests cover the collector logic itself.
"""

from __future__ import annotations

import gzip
import logging
from typing import Protocol
from uuid import UUID

import libvirt
import psycopg

from kdive.artifacts.storage import (
    ArtifactWriteRequest,
    FetchedArtifact,
    StoredArtifact,
    artifact_key,
    owner_prefix,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.providers.console_parts.rotation import part_object_name
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
from kdive.providers.remote_libvirt.connection.transport import remote_connection
from kdive.providers.remote_libvirt.console.collector import ConsoleStream
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secrets import SecretBackend

_log = logging.getLogger(__name__)

_TENANT = "remote-libvirt"
_OWNER_KIND = "systems"
_CONSOLE_NAME = "console"
_RETENTION = "console"
# Numbered parts are named …/console-parts-<n> (one key component — `artifact_key` forbids a
# `/` in the name) so they never collide with the single …/console artifact the assembly writes.
_PARTS_PREFIX = "console-parts-"
# The remote console stream is never truncated mid-collection (no power-cycle re-truncation like
# the local `<log append=off>` path), so every observable part artifact lives in generation 0;
# `part_object_name(0, index)` keys the same `console-part-0-<index>` shape `artifacts.get` reads.
_OBSERVABLE_PART_GEN = 0


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...

    def list_prefix(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


class RemoteConsolePartStore:
    """Object-store-backed console part store + single-artifact assembler (ADR-0095).

    Parts are small redacted objects under ``…/console-parts/<n>``; finalize concatenates them
    into one ``…/console`` artifact and registers/refreshes its `artifacts` row, so downstream
    consumers (`classify_console`, artifact search) read the same shape local produces.
    """

    def __init__(self, store: _StorePort, conninfo: str) -> None:
        self._store = store
        self._conninfo = conninfo

    def _part_name(self, index: int) -> str:
        return f"{_PARTS_PREFIX}{index}"

    def _part_key(self, system_id: UUID, index: int) -> str:
        return artifact_key(_TENANT, _OWNER_KIND, str(system_id), self._part_name(index))

    def _parts_prefix(self, system_id: UUID) -> str:
        return owner_prefix(_TENANT, _OWNER_KIND, str(system_id)) + _PARTS_PREFIX

    def put_part(self, system_id: UUID, index: int, data: bytes) -> None:
        """Seal one redacted part: the internal assembly object plus the observable copy.

        The internal ``…/console-parts-<index>`` object is written unchanged — ``finalize()``
        concatenates those raw redacted bytes into the immutable per-Run ``…/console`` evidence
        (ADR-0235). Additionally, a separate gzip-compressed ``console-part-0-<index>`` artifact is
        registered so an agent observes the live console through the same ``artifacts`` read surface
        the local rotation path exposes; ``data`` is already redacted by the collector. Registration
        is insert-if-absent, so a replayed seal does not duplicate the part's row.
        """
        self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=str(system_id),
                name=self._part_name(index),
                data=data,
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION,
            )
        )
        try:
            self._register_observable_part(system_id, index, data)
        except Exception:  # noqa: BLE001 - observability dual-write must never disrupt evidence (R7)
            _log.warning(
                "registering observable console part %d for system %s failed; "
                "the per-Run evidence is unaffected",
                index,
                system_id,
                exc_info=True,
            )

    def _register_observable_part(self, system_id: UUID, index: int, data: bytes) -> None:
        """Store the gzip-compressed observable part copy and insert its row if absent.

        ``data`` is the already-redacted part bytes; the object is REDACTED-class with
        ``content_encoding="gzip"`` so ``artifacts.get`` inflates it on read. Write-before-commit
        (ADR-0005): the object is stored first, then the row is inserted only if no row already
        keys the same object — a re-sealed (replayed) part does not duplicate the row. The caller
        treats any failure here as best-effort (R7): the observable copy is for live observation,
        never the evidence path, so a store/DB outage must not stall the evidence pump.
        """
        stored = self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=str(system_id),
                name=part_object_name(_OBSERVABLE_PART_GEN, index),
                data=gzip.compress(data),
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION,
                content_encoding="gzip",
            )
        )
        self._insert_part_row_if_absent(system_id, stored)

    def _insert_part_row_if_absent(self, system_id: UUID, stored: StoredArtifact) -> None:
        with psycopg.connect(self._conninfo) as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM artifacts "
                "WHERE owner_kind = %s AND owner_id = %s AND object_key = %s",
                (_OWNER_KIND, system_id, stored.key),
            )
            if cur.fetchone() is not None:
                return
            cur.execute(
                "INSERT INTO artifacts "
                "(owner_kind, owner_id, object_key, etag, sensitivity, retention_class) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    _OWNER_KIND,
                    system_id,
                    stored.key,
                    stored.etag,
                    Sensitivity.REDACTED.value,
                    _RETENTION,
                ),
            )

    def list_part_indices(self, system_id: UUID) -> list[int]:
        prefix = self._parts_prefix(system_id)
        indices: list[int] = []
        for key in self._store.list_prefix(prefix):
            suffix = key[len(prefix) :]
            if suffix.isdigit():
                indices.append(int(suffix))
        return sorted(indices)

    def read_part(self, system_id: UUID, index: int) -> bytes:
        fetched = self._store.get_artifact(self._part_key(system_id, index), None)
        return fetched.data

    def delete_part(self, system_id: UUID, index: int) -> None:
        self._store.delete(self._part_key(system_id, index))

    def assemble(self, system_id: UUID, start_index: int = 0) -> bytes:
        """Concatenate the System's numbered console parts in index order (no DB access).

        ``start_index`` slices to one boot window (ADR-0241): only parts with index
        ``>= start_index`` are included. Default ``0`` is the whole history (the teardown
        ``finalize()`` assembly).
        """
        return b"".join(
            self.read_part(system_id, index)
            for index in self.list_part_indices(system_id)
            if index >= start_index
        )

    def put_run_console(self, system_id: UUID, run_id: UUID, data: bytes) -> StoredArtifact:
        """Store an immutable per-Run ``console-<run>`` object and return its handle (no row).

        The bytes are already redacted (assembled from redacted parts), so the object is
        REDACTED-class. The boot worker upserts the `artifacts` row on its own connection so the
        row commits atomically with the boot step (ADR-0235), unlike the teardown
        :meth:`write_console_artifact`, which writes the System-keyed assembly and its row together.
        """
        return self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=str(system_id),
                name=f"console-{run_id}",
                data=data,
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION,
            )
        )

    def write_console_artifact(self, system_id: UUID, data: bytes) -> None:
        """Store the assembled console object and register/refresh its `artifacts` row.

        Write-before-commit (ADR-0005): the object is stored first, then the row is upserted in
        one short transaction. The bytes are already redacted (every part was redacted before
        upload and assembled from those parts), so the artifact is REDACTED-class.
        """
        stored = self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=str(system_id),
                name=_CONSOLE_NAME,
                data=data,
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION,
            )
        )
        self._upsert_row(system_id, stored.key, stored.etag)

    def _upsert_row(self, system_id: UUID, object_key: str, etag: str) -> None:
        with psycopg.connect(self._conninfo) as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM artifacts "
                "WHERE owner_kind = %s AND owner_id = %s AND object_key = %s",
                (_OWNER_KIND, system_id, object_key),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO artifacts "
                    "(owner_kind, owner_id, object_key, etag, sensitivity, retention_class) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        _OWNER_KIND,
                        system_id,
                        object_key,
                        etag,
                        Sensitivity.REDACTED.value,
                        _RETENTION,
                    ),
                )
            else:
                cur.execute("UPDATE artifacts SET etag = %s WHERE id = %s", (etag, row[0]))


class _RemoteConsoleStream:
    """A :class:`ConsoleStream` over a libvirt console + its connection (closed together)."""

    def __init__(self, conn, stream, closer) -> None:  # noqa: ANN001 - libvirt duck-typed seams
        self._conn = conn
        self._stream = stream
        self._closer = closer

    def recv(self, nbytes: int) -> bytes | None:
        got = self._stream.recv(nbytes)
        if got is None or got == -1:
            raise ConnectionError("console stream recv failed")
        if got == -2:  # would-block on a non-blocking stream: no data this read (ADR-0182)
            return None
        return got

    def close(self) -> None:
        try:
            self._stream.abort()
        except libvirt.libvirtError:
            _log.debug("aborting console stream failed; closing connection anyway")
        self._closer()


def open_remote_console(
    config: RemoteLibvirtConfig, secret_backend: SecretBackend, system_id: UUID
) -> ConsoleStream:
    """Open a ``virDomainOpenConsole`` stream for ``system_id`` over the mutual-TLS connection.

    The connection is opened per stream (it cannot be shared across the per-System tasks) and is
    closed with the stream. The stream is non-blocking so a recv returns promptly even on an
    idle console; the collector treats an empty read as "no data yet", a raised error as a drop.
    """
    cm = remote_connection(config, secret_backend, open_connection=libvirt.open)
    conn = cm.__enter__()

    def _closer() -> None:
        cm.__exit__(None, None, None)

    try:
        domain = conn.lookupByName(domain_name_for(system_id))
        stream = conn.newStream(libvirt.VIR_STREAM_NONBLOCK)
        domain.openConsole(None, stream, libvirt.VIR_DOMAIN_CONSOLE_FORCE)
    except libvirt.libvirtError:
        _closer()
        raise
    return _RemoteConsoleStream(conn, stream, _closer)
