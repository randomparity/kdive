"""Tests for deterministic remote external-boot artifact volumes (ADR-0583, ADR-0599)."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_name import (
    parse_boot_artifact_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_volumes import (
    MaterializedBootArtifacts,
    artifact_partial_volume_name,
    materialize_boot_artifacts,
    render_boot_artifact_volume_xml,
    require_boot_artifact_capacity,
)
from tests.providers.remote_libvirt.conftest import libvirt_error
from tests.providers.remote_libvirt.fakes import FakeStorageConn, FakeStoragePool, FakeStorageVolume

SYSTEM = UUID("00000000-0000-0000-0000-000000000003")
RUN = UUID("00000000-0000-0000-0000-000000000002")
ATTEMPT = UUID("00000000-0000-0000-0000-000000000004")


def _store_bytes(conn: FakeStorageConn, volume: FakeStorageVolume, payload: bytes) -> None:
    stream = conn.newStream(0)
    volume.upload(stream, 0, len(payload), 0)
    remaining = payload

    def send(_stream: object, nbytes: int, _opaque: object) -> bytes:
        nonlocal remaining
        chunk, remaining = remaining[:nbytes], remaining[nbytes:]
        return chunk

    stream.sendAll(send, None)
    stream.finish()


class _Stream:
    def __init__(self, data: bytes = b"", *, fail_send: bool = False) -> None:
        self.data = data
        self.sent = b""
        self.finished = False
        self.aborted = False
        self._fail_send = fail_send

    def sendAll(self, callback: Callable[[object, int, object], bytes], opaque: object) -> None:  # noqa: N802
        if self._fail_send:
            raise libvirt.libvirtError("stream failed")
        self.sent += callback(self, 1 << 20, opaque)

    def recvAll(self, callback: Callable[[object, bytes, object], None], opaque: object) -> None:  # noqa: N802
        callback(self, self.data, opaque)

    def finish(self) -> int:
        self.finished = True
        return 0

    def abort(self) -> int:
        self.aborted = True
        return 0


class _Volume:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data
        self.deleted = False
        self.upload_stream: _Stream | None = None

    def upload(self, stream: object, offset: int, length: int, flags: int = 0) -> int:
        del offset, length, flags
        assert isinstance(stream, _Stream)
        self.upload_stream = stream
        return 0

    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int:
        del offset, length, flags
        assert isinstance(stream, _Stream)
        stream.data = self.data or (self.upload_stream.sent if self.upload_stream else b"")
        return 0

    def delete(self, flags: int = 0) -> int:
        self.deleted = True
        return 0


class _Pool:
    def __init__(self, existing: _Volume | None = None) -> None:
        self.existing = existing
        self.created: _Volume | None = None
        self.created_xml: list[str] = []

    def storageVolLookupByName(self, name: str) -> _Volume:  # noqa: N802
        if self.existing is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)
        return self.existing

    def createXML(self, xml: str, flags: int = 0) -> _Volume:  # noqa: N802
        del flags
        self.created_xml.append(xml)
        self.created = _Volume()
        return self.created

    def createXMLFrom(self, xml: str, volume: object, flags: int = 0) -> _Volume:  # noqa: N802
        del flags
        assert isinstance(volume, _Volume)
        self.created_xml.append(xml)
        self.created = _Volume(volume.upload_stream.sent if volume.upload_stream else b"")
        return self.created


class _Conn:
    def __init__(self, pool: _Pool, streams: list[_Stream]) -> None:
        self.pool = pool
        self.streams = streams

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        return self.pool

    def newStream(self, flags: int = 0) -> _Stream:  # noqa: N802
        del flags
        return self.streams.pop(0)


def test_materializes_kernel_and_optional_initrd_with_opaque_deterministic_refs() -> None:
    pool = _Pool()
    conn = _Conn(pool, [_Stream(), _Stream(), _Stream(), _Stream(), _Stream(), _Stream()])

    result = materialize_boot_artifacts(
        conn, "images", system_id=SYSTEM, run_id=RUN, kernel=b"kernel", initrd=b"initrd"
    )

    assert isinstance(result, MaterializedBootArtifacts)
    assert result.kernel.ref == f"kernel/{SYSTEM}/{RUN}"
    assert result.initrd is not None
    assert result.initrd.ref == f"initrd/{SYSTEM}/{RUN}"
    assert len(pool.created_xml) == 4
    assert "kdive-boot-v1-kernel" in pool.created_xml[0]
    assert "-partial-" in pool.created_xml[0]
    assert "metadata" not in pool.created_xml[0]
    assert "kdive-boot-v1-kernel" in pool.created_xml[1]
    assert "-partial-" not in pool.created_xml[1]


def test_materialization_survives_metadata_free_dir_pool_readback() -> None:
    pool = FakeStoragePool(name="images")

    result = materialize_boot_artifacts(
        FakeStorageConn(pool),
        "images",
        system_id=SYSTEM,
        run_id=RUN,
        kernel=b"kernel",
        initrd=b"initrd",
        attempt_id=ATTEMPT,
    )

    assert result.kernel.ref == f"kernel/{SYSTEM}/{RUN}"
    assert result.initrd is not None
    volumes = pool.listAllVolumes(0)
    assert len(volumes) == 2
    identities = [parse_boot_artifact_name(volume.name()) for volume in volumes]
    assert identities[0] is not None and identities[0].kind == "kernel"
    assert identities[1] is not None and identities[1].kind == "initrd"
    assert all("metadata" not in volume.XMLDesc(0) for volume in volumes)


def test_changed_bytes_use_a_distinct_final_name_without_overwriting_prior_bytes() -> None:
    pool = FakeStoragePool(name="images")
    conn = FakeStorageConn(pool)
    materialize_boot_artifacts(
        conn, "images", system_id=SYSTEM, run_id=RUN, kernel=b"first", initrd=None
    )

    materialize_boot_artifacts(
        conn, "images", system_id=SYSTEM, run_id=RUN, kernel=b"second", initrd=None
    )

    volumes = pool.listAllVolumes(0)
    assert len(volumes) == 2
    parsed = [parse_boot_artifact_name(volume.name()) for volume in volumes]
    assert all(identity is not None for identity in parsed)
    assert len({identity.digest for identity in parsed if identity is not None}) == 2


def test_retry_rehashes_existing_volume_and_reuses_matching_identity() -> None:
    pool = _Pool(_Volume(b"kernel"))
    conn = _Conn(pool, [_Stream()])

    result = materialize_boot_artifacts(
        conn, "images", system_id=SYSTEM, run_id=RUN, kernel=b"kernel", initrd=None
    )

    assert result.kernel.ref == f"kernel/{SYSTEM}/{RUN}"
    assert pool.created is None
    assert pool.existing is not None
    assert not pool.existing.deleted


def test_retry_mismatch_is_conflict_and_leaves_existing_volume() -> None:
    pool = _Pool(_Volume(b"different"))
    conn = _Conn(pool, [_Stream()])

    with pytest.raises(CategorizedError) as exc:
        materialize_boot_artifacts(
            conn, "images", system_id=SYSTEM, run_id=RUN, kernel=b"kernel", initrd=None
        )

    assert exc.value.category is ErrorCategory.CONFLICT
    assert pool.existing is not None
    assert not pool.existing.deleted


def test_retry_mismatched_partial_is_conflict_and_preserves_evidence() -> None:
    pool = FakeStoragePool(name="images")
    conn = FakeStorageConn(pool)
    partial_name = artifact_partial_volume_name("kernel", SYSTEM, RUN, b"kernel", ATTEMPT)
    partial = pool.createXML(
        render_boot_artifact_volume_xml(partial_name, capacity_bytes=len(b"different"))
    )
    _store_bytes(conn, partial, b"different")

    with pytest.raises(CategorizedError) as exc:
        materialize_boot_artifacts(
            conn,
            "images",
            system_id=SYSTEM,
            run_id=RUN,
            kernel=b"kernel",
            initrd=None,
            attempt_id=ATTEMPT,
        )

    assert exc.value.category is ErrorCategory.CONFLICT
    assert pool.listVolumes() == [partial_name]


def test_failed_attempt_deletes_only_its_partial_volume() -> None:
    pool = _Pool()
    conn = _Conn(pool, [_Stream(fail_send=True)])

    with pytest.raises(CategorizedError) as exc:
        materialize_boot_artifacts(
            conn, "images", system_id=SYSTEM, run_id=RUN, kernel=b"kernel", initrd=None
        )

    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert pool.created is not None
    assert pool.created.deleted


def test_capacity_preflight_rejects_before_opening_or_mutating_the_pool() -> None:
    with pytest.raises(CategorizedError) as exc:
        materialize_boot_artifacts(
            _Conn(_Pool(), []),
            "images",
            system_id=SYSTEM,
            run_id=RUN,
            kernel=b"kernel",
            initrd=b"initrd",
            max_bytes=5,
        )

    assert exc.value.category is ErrorCategory.CAPACITY_EXHAUSTED


def test_capacity_preflight_accepts_exact_bound() -> None:
    require_boot_artifact_capacity(5, 1, max_bytes=6)
