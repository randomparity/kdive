"""Remote external-boot volume ownership and orphan reaping (ADR-0599)."""

from __future__ import annotations

import hashlib
from typing import cast
from uuid import UUID

import libvirt

from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_name import (
    BootArtifactKind,
    BootArtifactName,
    render_boot_artifact_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_volumes import (
    render_boot_artifact_volume_xml,
)
from kdive.providers.remote_libvirt.reaping.boot_artifacts import (
    BootArtifactReaperConn,
    list_owned_boot_artifacts,
    reap_orphaned_boot_artifacts,
)
from tests.providers.remote_libvirt.fakes import (
    FakeStorageConn,
    FakeStoragePool,
    FakeStorageVolume,
)

SYSTEM = UUID("00000000-0000-0000-0000-000000000003")
RUN = UUID("00000000-0000-0000-0000-000000000002")
ATTEMPT = UUID("00000000-0000-0000-0000-000000000004")
POOL = "boot-artifacts"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _name(kind: BootArtifactKind, data: bytes, *, partial: bool = False) -> str:
    return render_boot_artifact_name(
        kind,
        SYSTEM,
        RUN,
        _digest(data),
        attempt_id=ATTEMPT if partial else None,
    )


def _put(conn: FakeStorageConn, pool: FakeStoragePool, name: str, data: bytes) -> FakeStorageVolume:
    volume = pool.createXML(render_boot_artifact_volume_xml(name, capacity_bytes=len(data)))
    stream = conn.newStream(0)
    volume.upload(stream, 0, len(data), 0)
    position = 0

    def send(_stream: object, bound: int, _opaque: object) -> bytes:
        nonlocal position
        chunk = data[position : position + bound]
        position += len(chunk)
        return chunk

    stream.sendAll(send, None)
    stream.finish()
    return volume


def test_listing_accepts_metadata_free_name_and_matching_complete_bytes() -> None:
    pool = FakeStoragePool(name=POOL)
    conn = FakeStorageConn(pool)
    name = _name("kernel", b"kernel")
    final = _put(conn, pool, name, b"kernel")
    _put(conn, pool, "kdive-boot-v1-foreign", b"kernel")
    mismatch_name = _name("initrd", b"expected")
    _put(conn, pool, mismatch_name, b"different")

    result = list_owned_boot_artifacts(cast("BootArtifactReaperConn", conn), POOL)

    assert "metadata" not in final.XMLDesc(0)
    assert result == [
        BootArtifactName(
            name=name,
            kind="kernel",
            system_id=SYSTEM,
            run_id=RUN,
            digest=_digest(b"kernel"),
            partial=False,
            attempt_id=None,
        )
    ]


def test_reap_removes_matching_orphaned_final_and_partial_only() -> None:
    pool = FakeStoragePool(name=POOL)
    conn = FakeStorageConn(pool)
    orphan_final = _put(conn, pool, _name("kernel", b"kernel"), b"kernel")
    orphan_partial = _put(conn, pool, _name("initrd", b"initrd", partial=True), b"initrd")
    mismatch = _put(conn, pool, _name("kernel", b"expected"), b"different")
    foreign = _put(conn, pool, "operator-volume", b"operator")

    removed = reap_orphaned_boot_artifacts(
        cast("BootArtifactReaperConn", conn), POOL, live_owners=set()
    )

    assert removed == 2
    remaining = pool.listAllVolumes(0)
    assert orphan_final not in remaining and orphan_partial not in remaining
    assert mismatch in remaining and foreign in remaining


def test_reap_keeps_a_live_owner() -> None:
    pool = FakeStoragePool(name=POOL)
    conn = FakeStorageConn(pool)
    volume = _put(conn, pool, _name("kernel", b"kernel"), b"kernel")
    owner: tuple[BootArtifactKind, UUID, UUID, str] = (
        "kernel",
        SYSTEM,
        RUN,
        _digest(b"kernel"),
    )

    assert (
        reap_orphaned_boot_artifacts(
            cast("BootArtifactReaperConn", conn), POOL, live_owners={owner}
        )
        == 0
    )
    assert volume in pool.listAllVolumes(0)


class _UnreadableVolume:
    def __init__(self, volume: FakeStorageVolume) -> None:
        self._volume = volume
        self.deleted = False

    def name(self) -> str:
        return self._volume.name()

    def delete(self, flags: int = 0) -> int:
        del flags
        self.deleted = True
        return 0

    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int:
        del stream, offset, length, flags
        raise libvirt.libvirtError("unreadable")


class _UnreadablePool:
    def __init__(self, volume: _UnreadableVolume) -> None:
        self._volume = volume

    def refresh(self, flags: int = 0) -> int:
        del flags
        return 0

    def listAllVolumes(self, flags: int = 0) -> list[_UnreadableVolume]:  # noqa: N802
        del flags
        return [self._volume]


class _UnreadableConn:
    def __init__(self, pool: _UnreadablePool, streams: FakeStorageConn) -> None:
        self._pool = pool
        self._streams = streams

    def storagePoolLookupByName(self, name: str) -> _UnreadablePool:  # noqa: N802
        assert name == POOL
        return self._pool

    def newStream(self, flags: int = 0) -> object:  # noqa: N802
        return self._streams.newStream(flags)


def test_reap_keeps_a_canonical_but_unreadable_volume() -> None:
    backing_pool = FakeStoragePool(name=POOL)
    stream_conn = FakeStorageConn(backing_pool)
    volume = _UnreadableVolume(
        _put(stream_conn, backing_pool, _name("kernel", b"kernel"), b"kernel")
    )
    conn = _UnreadableConn(_UnreadablePool(volume), stream_conn)

    assert (
        reap_orphaned_boot_artifacts(cast("BootArtifactReaperConn", conn), POOL, live_owners=set())
        == 0
    )
    assert not volume.deleted
