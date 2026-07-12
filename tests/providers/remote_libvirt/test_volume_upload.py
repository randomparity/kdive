"""Tests for the remote-libvirt qcow2 volume upload sequence (ADR-0336)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.volume_upload import (
    render_base_volume_xml,
    upload_qcow2_volume,
)
from tests.providers.remote_libvirt.conftest import libvirt_error


def test_render_base_volume_xml_is_standalone_qcow2() -> None:
    """A staged base image is a full qcow2 volume: format qcow2, capacity set, no backing store."""
    xml = render_base_volume_xml("fedora-44.qcow2", capacity_bytes=1234)
    assert "<name>fedora-44.qcow2</name>" in xml
    assert "<capacity>1234</capacity>" in xml
    assert '<format type="qcow2" />' in xml
    assert "backingStore" not in xml


class _FakeStream:
    def __init__(self) -> None:
        self.sent = b""
        self.finished = False
        self.aborted = False

    def sendAll(  # noqa: N802
        self, handler: Callable[[object, int, object], bytes], opaque: object
    ) -> None:
        # Drain the source callback the way libvirt would, in one chunk.
        self.sent += handler(self, 1 << 20, opaque)

    def finish(self) -> int:
        self.finished = True
        return 0

    def abort(self) -> int:
        self.aborted = True
        return 0


class _FakeVolume:
    def __init__(self) -> None:
        self.upload_args: tuple[int, int, int] | None = None
        self.deleted = False

    def upload(self, stream: object, offset: int, length: int, flags: int = 0) -> int:
        self.upload_args = (offset, length, flags)
        return 0

    def delete(self, flags: int = 0) -> int:
        self.deleted = True
        return 0


class _FakePool:
    def __init__(
        self, volume: _FakeVolume, *, create_fails: bool = False, volume_exists: bool = False
    ) -> None:
        self._volume = volume
        self._create_fails = create_fails
        self._volume_exists = volume_exists
        self.created_xml: str | None = None

    def storageVolLookupByName(self, name: str) -> _FakeVolume:  # noqa: N802
        if self._volume_exists:
            return self._volume
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)

    def createXML(self, xml: str, flags: int = 0) -> _FakeVolume:  # noqa: N802
        if self._create_fails:
            raise libvirt.libvirtError("create failed")
        self.created_xml = xml
        return self._volume


class _FakeConn:
    def __init__(
        self,
        pool: _FakePool | None,
        stream: _FakeStream,
        *,
        pool_missing: bool = False,
    ) -> None:
        self._pool = pool
        self._stream = stream
        self._pool_missing = pool_missing

    def storagePoolLookupByName(self, name: str) -> _FakePool:  # noqa: N802
        if self._pool_missing:
            raise libvirt.libvirtError("no pool")
        assert self._pool is not None
        return self._pool

    def newStream(self, flags: int = 0) -> _FakeStream:  # noqa: N802
        return self._stream


def _qcow2(tmp_path: Path, data: bytes = b"qcow2-bytes") -> Path:
    path = tmp_path / "img.qcow2"
    path.write_bytes(data)
    return path


def test_upload_streams_bytes_and_finishes(tmp_path: Path) -> None:
    """The happy path creates the volume, streams the file, and finishes the stream."""
    volume, stream = _FakeVolume(), _FakeStream()
    pool = _FakePool(volume)
    conn = _FakeConn(pool, stream)
    qcow2 = _qcow2(tmp_path, b"CONFIG-QCOW2")

    upload_qcow2_volume(conn, "images", "fedora-44.qcow2", qcow2)

    assert pool.created_xml is not None and "fedora-44.qcow2" in pool.created_xml
    assert volume.upload_args == (0, len(b"CONFIG-QCOW2"), 0)
    assert stream.sent == b"CONFIG-QCOW2"
    assert stream.finished
    assert not volume.deleted


def test_upload_skips_when_volume_already_staged(tmp_path: Path) -> None:
    """A re-run over an already-staged volume skips the create+stream (idempotent recovery)."""
    volume, stream = _FakeVolume(), _FakeStream()
    pool = _FakePool(volume, volume_exists=True)
    conn = _FakeConn(pool, stream)

    upload_qcow2_volume(conn, "images", "fedora-44.qcow2", _qcow2(tmp_path))

    assert pool.created_xml is None  # nothing created
    assert volume.upload_args is None  # nothing streamed
    assert not stream.finished


def test_upload_missing_pool_raises_infra(tmp_path: Path) -> None:
    conn = _FakeConn(None, _FakeStream(), pool_missing=True)
    with pytest.raises(CategorizedError) as exc:
        upload_qcow2_volume(conn, "images", "v", _qcow2(tmp_path))
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_upload_create_failure_raises_infra(tmp_path: Path) -> None:
    conn = _FakeConn(_FakePool(_FakeVolume(), create_fails=True), _FakeStream())
    with pytest.raises(CategorizedError) as exc:
        upload_qcow2_volume(conn, "images", "v", _qcow2(tmp_path))
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_upload_stream_failure_cleans_up_partial_volume(tmp_path: Path) -> None:
    """A stream fault aborts the stream, deletes the partial volume, and raises infra."""

    class _FailingStream(_FakeStream):
        def sendAll(  # noqa: N802
            self, handler: Callable[[object, int, object], bytes], opaque: object
        ) -> None:
            raise libvirt.libvirtError("stream broke")

    volume, stream = _FakeVolume(), _FailingStream()
    conn = _FakeConn(_FakePool(volume), stream)
    with pytest.raises(CategorizedError) as exc:
        upload_qcow2_volume(conn, "images", "v", _qcow2(tmp_path))
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert stream.aborted
    assert volume.deleted
