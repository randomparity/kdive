"""Tests for the remote-libvirt qcow2 volume upload sequence (ADR-0336)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

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
        if self._volume_exists and not self._volume.deleted:
            return self._volume
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)

    def createXML(self, xml: str, flags: int = 0) -> _FakeVolume:  # noqa: N802
        if self._create_fails:
            raise libvirt.libvirtError("create failed")
        self.created_xml = xml
        self._volume_exists = True
        self._volume.deleted = False
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


# A minimal qcow2: the magic (bytes 51 46 49 fb) plus a body the fake stream drains verbatim.
_QCOW2_MAGIC = b"QFI\xfb"


def _qcow2(tmp_path: Path, data: bytes = _QCOW2_MAGIC + b"body") -> Path:
    path = tmp_path / "img.qcow2"
    path.write_bytes(data)
    return path


def test_upload_streams_bytes_and_finishes(tmp_path: Path) -> None:
    """The happy path creates the volume, streams the file, and finishes the stream."""
    volume, stream = _FakeVolume(), _FakeStream()
    pool = _FakePool(volume)
    conn = _FakeConn(pool, stream)
    body = _QCOW2_MAGIC + b"CONFIG-QCOW2"
    qcow2 = _qcow2(tmp_path, body)

    upload_qcow2_volume(conn, "images", "fedora-44.qcow2", qcow2)

    assert pool.created_xml is not None and "fedora-44.qcow2" in pool.created_xml
    assert volume.upload_args == (0, len(body), 0)
    assert stream.sent == body
    assert stream.finished
    assert not volume.deleted


def test_upload_rejects_non_qcow2_file(tmp_path: Path) -> None:
    """A file without the qcow2 magic is a configuration error; no volume is created."""
    volume, stream = _FakeVolume(), _FakeStream()
    pool = _FakePool(volume)
    conn = _FakeConn(pool, stream)
    not_qcow2 = tmp_path / "img.qcow2"
    not_qcow2.write_bytes(b"not-a-qcow2-header")

    with pytest.raises(CategorizedError) as exc:
        upload_qcow2_volume(conn, "images", "v", not_qcow2)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert pool.created_xml is None  # nothing created for a bad file
    assert not stream.finished


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


@pytest.mark.parametrize("existing,create_fails", [(True, False), (False, True)])
def test_fresh_target_never_reuses_or_deletes_existing_volume(
    tmp_path: Path, existing: bool, create_fails: bool
) -> None:
    volume, stream = _FakeVolume(), _FakeStream()
    pool = _FakePool(volume, volume_exists=existing, create_fails=create_fails)
    with pytest.raises(CategorizedError):
        upload_qcow2_volume(
            _FakeConn(pool, stream), "images", "base", _qcow2(tmp_path), require_new=True
        )
    assert not stream.sent and not stream.finished and not volume.deleted


@pytest.mark.parametrize("stage", ["setup", "reopen", "read", "send", "finish"])
def test_upload_failure_rolls_back_and_allows_same_name_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    source = _qcow2(tmp_path)
    volume, stream = _FakeVolume(), _FakeStream()
    pool = _FakePool(volume)
    conn = _FakeConn(pool, stream)
    error = (
        OSError("source I/O failed")
        if stage in {"reopen", "read"}
        else libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise error

    original_open = Path.open
    opens = 0
    with monkeypatch.context() as faults:

        def open_source(path: Path, *args: Any, **kwargs: Any) -> Any:
            nonlocal opens
            if path == source:
                opens += 1
                if opens == 2 and stage == "reopen":
                    raise error
            handle = original_open(path, *args, **kwargs)
            if path == source and opens == 2 and stage == "read":
                faults.setattr(handle, "read", fail)
            return handle

        if stage in {"reopen", "read"}:
            faults.setattr(Path, "open", open_source)
        elif stage == "setup":
            faults.setattr(conn, "newStream", fail)
        else:
            faults.setattr(stream, "sendAll" if stage == "send" else "finish", fail)
        with pytest.raises(CategorizedError) as caught:
            upload_qcow2_volume(
                conn, "image-stage-pool", "candidate-base.qcow2", source, require_new=True
            )
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert caught.value.details == {
            "pool": "image-stage-pool",
            "volume": "candidate-base.qcow2",
        }
        assert caught.value.__cause__ is error
        assert pool.created_xml is not None and volume.deleted
        assert stream.aborted is (stage != "setup")
    retry = _FakeStream()
    upload_qcow2_volume(
        _FakeConn(pool, retry), "image-stage-pool", "candidate-base.qcow2", source, require_new=True
    )
    assert retry.sent == source.read_bytes() and retry.finished
    assert not volume.deleted


@pytest.mark.parametrize("stage", ["setup", "send"])
@pytest.mark.parametrize("cleanup", ["abort-libvirt", "abort-os", "delete", "both"])
def test_upload_cleanup_failure_keeps_primary_and_attempts_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stage: str,
    cleanup: str,
) -> None:
    source = _qcow2(tmp_path)
    volume, stream = _FakeVolume(), _FakeStream()
    conn = _FakeConn(_FakePool(volume), stream)
    primary = libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)
    aborts: list[bool] = []
    deletions: list[bool] = []

    def fail(*args: object, **kwargs: object) -> None:
        raise primary

    def fail_abort() -> int:
        aborts.append(True)
        if cleanup == "abort-os":
            raise OSError("abort I/O failed")
        raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)

    def fail_delete(flags: int = 0) -> int:
        deletions.append(True)
        raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)

    if stage == "setup":
        monkeypatch.setattr(conn, "newStream", fail)
    else:
        monkeypatch.setattr(stream, "sendAll", fail)
    if cleanup in {"abort-libvirt", "abort-os", "both"}:
        monkeypatch.setattr(stream, "abort", fail_abort)
    if cleanup in {"delete", "both"}:
        monkeypatch.setattr(volume, "delete", fail_delete)
    with pytest.raises(CategorizedError) as caught:
        upload_qcow2_volume(conn, "image-stage-pool", "candidate-base.qcow2", source)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"pool": "image-stage-pool", "volume": "candidate-base.qcow2"}
    assert caught.value.__cause__ is primary
    if cleanup in {"delete", "both"}:
        assert deletions == [True] and not volume.deleted
        assert "remote volume rollback failed" in caplog.text
        assert "image-stage-pool" in caplog.text and "candidate-base.qcow2" in caplog.text
    else:
        assert volume.deleted
    assert bool(aborts) is (stage == "send" and cleanup != "delete")
