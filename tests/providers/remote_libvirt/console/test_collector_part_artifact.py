"""Tests for the remote part store's observable compressed-part dual-write (issue #892).

``RemoteConsolePartStore.put_part`` writes the internal ``console-parts-<index>`` assembly object
unchanged (the per-Run evidence, ADR-0235) and additionally registers a separate
gzip-compressed ``console-part-0-<index>`` artifact so an agent observes a remote System's live
console through the same ``artifacts.{list,get,find}`` surface the local path uses.
"""

from __future__ import annotations

import asyncio
import gzip
from uuid import UUID, uuid4

import psycopg

from kdive.artifacts.storage import ArtifactWriteRequest, FetchedArtifact, StoredArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.providers.console_parts.rotation import part_object_name
from kdive.providers.remote_libvirt.console.wiring import RemoteConsolePartStore

_TENANT = "remote-libvirt"


class RecordingObjectStore:
    """In-memory object store that also records each object's ``content_encoding``."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.encodings: dict[str, str | None] = {}
        self._etag = 0

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        key = request.key()
        self.objects[key] = request.data
        self.encodings[key] = request.content_encoding
        self._etag += 1
        return StoredArtifact(
            key, f"etag-{self._etag}", request.sensitivity, request.retention_class
        )

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        return FetchedArtifact(self.objects[key], Sensitivity.REDACTED, "console")

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def _observable_key(system_id: UUID, index: int) -> str:
    return f"{_TENANT}/systems/{system_id}/{part_object_name(0, index)}"


def _internal_key(system_id: UUID, index: int) -> str:
    return f"{_TENANT}/systems/{system_id}/console-parts-{index}"


def _count_rows(url: str, system_id: UUID, object_key: str) -> int:
    async def _check() -> int:
        async with await psycopg.AsyncConnection.connect(url) as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM artifacts WHERE owner_id = %s AND object_key = %s",
                (system_id, object_key),
            )
            row = await cur.fetchone()
        return 0 if row is None else int(row[0])

    return asyncio.run(_check())


def _row_classes(url: str, system_id: UUID, object_key: str) -> tuple[str, str, str] | None:
    async def _check() -> tuple[str, str, str] | None:
        async with await psycopg.AsyncConnection.connect(url) as conn:
            cur = await conn.execute(
                "SELECT owner_kind, sensitivity, retention_class FROM artifacts "
                "WHERE owner_id = %s AND object_key = %s",
                (system_id, object_key),
            )
            row = await cur.fetchone()
        return None if row is None else (row[0], row[1], row[2])

    return asyncio.run(_check())


def test_put_part_registers_compressed_observable_artifact(migrated_url: str) -> None:
    store = RecordingObjectStore()
    part_store = RemoteConsolePartStore(store, migrated_url)
    sid = uuid4()
    redacted = b"login: root\npassword: <redacted>\n"

    part_store.put_part(sid, 0, redacted)

    observable_key = _observable_key(sid, 0)
    assert store.encodings[observable_key] == "gzip"
    assert gzip.decompress(store.objects[observable_key]) == redacted
    assert _row_classes(migrated_url, sid, observable_key) == ("systems", "redacted", "console")


def test_internal_assembly_path_is_unchanged(migrated_url: str) -> None:
    # Regression guard: the internal console-parts-<index> objects stay raw redacted bytes and
    # finalize's assembly (read_part/assemble) concatenates them byte-for-byte (ADR-0235).
    store = RecordingObjectStore()
    part_store = RemoteConsolePartStore(store, migrated_url)
    sid = uuid4()
    parts = [b"boot ", b"... ", b"crash"]

    for index, blob in enumerate(parts):
        part_store.put_part(sid, index, blob)

    for index, blob in enumerate(parts):
        # The internal object is the raw redacted bytes (NOT gzip-compressed).
        assert store.objects[_internal_key(sid, index)] == blob
        assert store.encodings[_internal_key(sid, index)] is None
    assert part_store.list_part_indices(sid) == [0, 1, 2]
    assert part_store.assemble(sid) == b"".join(parts)


def test_put_part_is_idempotent_no_duplicate_row(migrated_url: str) -> None:
    store = RecordingObjectStore()
    part_store = RemoteConsolePartStore(store, migrated_url)
    sid = uuid4()
    redacted = b"replayed part\n"

    part_store.put_part(sid, 0, redacted)
    part_store.put_part(sid, 0, redacted)  # replay after a crash-before-sidecar advance

    assert _count_rows(migrated_url, sid, _observable_key(sid, 0)) == 1


class _FailingObservableStore(RecordingObjectStore):
    """Records the internal part write but fails the gzip observable dual-write."""

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        if request.content_encoding == "gzip":
            raise RuntimeError("object store unavailable for the observable part")
        return super().put_artifact(request)


def test_observable_dual_write_failure_does_not_disrupt_evidence() -> None:
    # R7 best-effort: a failing observable dual-write must not raise out of put_part, and the
    # internal console-parts-<index> evidence object must still be written. No DB needed: the
    # observable object write fails before any row registration, so conninfo is never used.
    store = _FailingObservableStore()
    part_store = RemoteConsolePartStore(store, "unused")
    sid = uuid4()
    redacted = b"boot ... crash\n"

    part_store.put_part(sid, 0, redacted)  # must not raise

    assert store.objects[_internal_key(sid, 0)] == redacted
    assert _observable_key(sid, 0) not in store.objects
