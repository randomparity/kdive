"""RemoteLibvirtTrafficCapture tests — injected TLS opener + fake conn, no live host (ADR-0432).

The capture mechanic mirrors LocalLibvirtTrafficCapture (filter-dump over QMP), so those attach/
detach assertions are the same shape; what is new and remote-specific is netdev discovery from the
domain XML, the storage-pool dest resolution + stale sweep, and the storage-volume download
fetch-back. Unit tests inject a fake connection/domain/pool/volume/stream and a recording secret
backend; the real ``libvirt.open`` adapter is ``live_vm``-only, exercised by the remote live proof.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.lifecycle.traffic_capture import (
    REMOTE_PCAP_WRITE_REMEDIATION,
    RemoteLibvirtTrafficCapture,
    discover_netdev_id,
    pcap_volume_name,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend, libvirt_error

_DOMAIN = "kdive-sys-remote"
_POOL = "kdivepool"
_POOL_XML = "<pool type='dir'><target><path>/var/lib/libvirt/images</path></target></pool>"
_IFACE_XML = (
    "<domain><devices>"
    "<interface type='network'><alias name='net0'/><target dev='vnet3'/></interface>"
    "</devices></domain>"
)


class _FakeStream:
    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self.finished = False
        self.aborted = False

    def feed(self, data: bytes) -> None:
        self._chunks = [data[i : i + 4] for i in range(0, len(data), 4)] or [b""]

    def recvAll(self, callback, opaque) -> None:  # noqa: N802 - libvirt binding name
        for chunk in self._chunks:
            callback(self, chunk, opaque)

    def finish(self) -> None:
        self.finished = True

    def abort(self) -> None:
        self.aborted = True


class _FakeVolume:
    def __init__(self, name: str, data: bytes = b"") -> None:
        self._name = name
        self._data = data
        self.deleted = False

    def name(self) -> str:
        return self._name

    def info(self) -> list[int]:
        return [0, len(self._data), len(self._data)]

    def download(self, stream: _FakeStream, offset: int, length: int, flags: int) -> int:
        stream.feed(self._data)
        return 0

    def delete(self, flags: int = 0) -> int:
        self.deleted = True
        return 0


class _FakePool:
    def __init__(
        self, *, xml: str = _POOL_XML, volumes: dict[str, _FakeVolume] | None = None
    ) -> None:
        self._xml = xml
        self.volumes = volumes if volumes is not None else {}
        self.refreshed = 0

    def refresh(self, flags: int = 0) -> int:
        self.refreshed += 1
        return 0

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - libvirt binding name
        return self._xml

    def listAllVolumes(self, flags: int = 0) -> list[_FakeVolume]:  # noqa: N802
        return list(self.volumes.values())

    def storageVolLookupByName(self, name: str) -> _FakeVolume:  # noqa: N802
        if name not in self.volumes:
            raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)
        return self.volumes[name]


class _FakeDomain:
    def __init__(self, *, xml: str = _IFACE_XML) -> None:
        self._xml = xml
        self.commands: list[dict] = []
        self.raise_on: dict[str, Exception] = {}

    def XMLDesc(self, flags: int) -> str:  # noqa: N802 - libvirt binding name
        return self._xml

    def qemuMonitorCommand(self, cmd: str, flags: int) -> str:  # noqa: N802
        parsed = json.loads(cmd)
        self.commands.append(parsed)
        exc = self.raise_on.get(parsed["execute"])
        if exc is not None:
            raise exc
        return "{}"


class _FakeConn:
    def __init__(self, *, domain: _FakeDomain | None = None, pool: _FakePool | None = None) -> None:
        self._domain = domain
        self._pool = pool
        self.stream = _FakeStream()
        self.closed = False

    def lookupByName(self, name: str) -> _FakeDomain:  # noqa: N802 - libvirt binding name
        if self._domain is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return self._domain

    def storagePoolLookupByName(self, name: str) -> _FakePool:  # noqa: N802
        if self._pool is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_POOL)
        return self._pool

    def newStream(self, flags: int = 0) -> _FakeStream:  # noqa: N802 - libvirt binding name
        return self.stream

    def close(self) -> None:
        self.closed = True


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
        storage_pool=_POOL,
    )


def _capturer(conn: _FakeConn, tmp_path: Path) -> RemoteLibvirtTrafficCapture:
    return RemoteLibvirtTrafficCapture(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )


# --- netdev discovery -------------------------------------------------------------------------


def test_discover_netdev_prepends_host_to_the_interface_alias() -> None:
    assert discover_netdev_id(_IFACE_XML) == "hostnet0"


def test_discover_netdev_picks_the_first_aliased_interface() -> None:
    xml = (
        "<domain><devices>"
        "<interface type='network'><target dev='vnet0'/></interface>"  # no alias — skipped
        "<interface type='network'><alias name='net2'/></interface>"
        "</devices></domain>"
    )
    assert discover_netdev_id(xml) == "hostnet2"


def test_discover_netdev_without_interface_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        discover_netdev_id("<domain><devices></devices></domain>")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_discover_netdev_on_malformed_xml_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        discover_netdev_id("<domain><devices>")  # unclosed
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- attach / detach --------------------------------------------------------------------------


def test_attach_discovers_netdev_then_deletes_stale_and_adds_filter_dump(tmp_path: Path) -> None:
    domain = _FakeDomain()
    cap = _capturer(_FakeConn(domain=domain), tmp_path)
    cap.attach(_DOMAIN, qom_id="kdive-dump-J", dest_path="/pool/cap.pcap", snaplen=128)

    assert [c["execute"] for c in domain.commands] == ["object-del", "object-add"]
    add = domain.commands[1]["arguments"]
    assert add["qom-type"] == "filter-dump"
    assert add["id"] == "kdive-dump-J"
    assert add["netdev"] == "hostnet0"  # discovered from the domain XML alias
    assert add["file"] == "/pool/cap.pcap"  # the remote pool path the handler passes through
    assert add["maxlen"] == 128


def test_attach_tolerates_object_not_found_on_first_run(tmp_path: Path) -> None:
    domain = _FakeDomain()
    domain.raise_on["object-del"] = libvirt.libvirtError("object 'kdive-dump-J' not found")
    cap = _capturer(_FakeConn(domain=domain), tmp_path)
    cap.attach(_DOMAIN, qom_id="kdive-dump-J", dest_path="/pool/cap.pcap", snaplen=128)
    assert [c["execute"] for c in domain.commands] == ["object-del", "object-add"]


def test_attach_reraises_other_monitor_error_as_control_failure(tmp_path: Path) -> None:
    domain = _FakeDomain()
    domain.raise_on["object-add"] = libvirt.libvirtError("some other monitor failure")
    cap = _capturer(_FakeConn(domain=domain), tmp_path)
    with pytest.raises(CategorizedError) as exc:
        cap.attach(_DOMAIN, qom_id="q", dest_path="/pool/cap.pcap", snaplen=128)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE


def test_detach_issues_object_del_and_closes(tmp_path: Path) -> None:
    domain = _FakeDomain()
    conn = _FakeConn(domain=domain)
    cap = _capturer(conn, tmp_path)
    cap.detach(_DOMAIN, qom_id="kdive-dump-J")
    assert domain.commands[0]["execute"] == "object-del"
    assert domain.commands[0]["arguments"]["id"] == "kdive-dump-J"
    assert conn.closed is True


# --- prepare / per-job pre-delete -------------------------------------------------------------


def test_prepare_predeletes_this_jobs_stale_volume_and_returns_pool_path(tmp_path: Path) -> None:
    system_id, job_id = uuid4(), uuid4()
    own = _FakeVolume(pcap_volume_name(system_id, job_id))  # a prior attempt of THIS job
    concurrent = _FakeVolume(f"kdive-pcap-{system_id}-{uuid4()}.pcap")  # another job, same System
    disk = _FakeVolume("some-domain-disk.qcow2")
    pool = _FakePool(volumes={v.name(): v for v in (own, concurrent, disk)})
    cap = _capturer(_FakeConn(pool=pool), tmp_path)

    dest = cap.prepare(system_id, job_id)

    assert dest == f"/var/lib/libvirt/images/{pcap_volume_name(system_id, job_id)}"
    assert own.deleted is True  # this job's own stale volume is cleared before a retry
    assert concurrent.deleted is False  # a concurrent capture on the same System is NOT disturbed
    assert disk.deleted is False  # a non-pcap volume is never touched


def test_prepare_tolerates_no_stale_volume(tmp_path: Path) -> None:
    system_id, job_id = uuid4(), uuid4()
    cap = _capturer(_FakeConn(pool=_FakePool()), tmp_path)
    dest = cap.prepare(system_id, job_id)  # first-ever capture: nothing to pre-delete
    assert dest == f"/var/lib/libvirt/images/{pcap_volume_name(system_id, job_id)}"


def test_prepare_rejects_a_non_dir_storage_pool(tmp_path: Path) -> None:
    pool = _FakePool(xml="<pool type='logical'><target><path>/dev/vg</path></target></pool>")
    cap = _capturer(_FakeConn(pool=pool), tmp_path)
    with pytest.raises(CategorizedError) as exc:
        cap.prepare(uuid4(), uuid4())
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- captured_size / fetch / reclaim ----------------------------------------------------------


def test_captured_size_reads_volume_capacity_and_zero_when_absent(tmp_path: Path) -> None:
    system_id, job_id = uuid4(), uuid4()
    vol_name = pcap_volume_name(system_id, job_id)
    dest = f"/var/lib/libvirt/images/{vol_name}"

    empty_pool = _FakePool()
    assert _capturer(_FakeConn(pool=empty_pool), tmp_path).captured_size(dest) == 0
    assert empty_pool.refreshed == 1  # the pool is refreshed to see the growing file

    vol = _FakeVolume(vol_name, data=b"x" * 4096)
    pool = _FakePool(volumes={vol_name: vol})
    assert _capturer(_FakeConn(pool=pool), tmp_path).captured_size(dest) == 4096


def test_fetch_streams_volume_to_memory_and_empty_when_absent(tmp_path: Path) -> None:
    system_id, job_id = uuid4(), uuid4()
    vol_name = pcap_volume_name(system_id, job_id)
    dest = f"/var/lib/libvirt/images/{vol_name}"

    assert _capturer(_FakeConn(pool=_FakePool()), tmp_path).fetch(dest, max_bytes=64) == b""

    payload = b"PCAP" * 16
    conn = _FakeConn(pool=_FakePool(volumes={vol_name: _FakeVolume(vol_name, data=payload)}))
    assert _capturer(conn, tmp_path).fetch(dest, max_bytes=1024) == payload
    assert conn.stream.finished is True


def test_fetch_aborts_and_raises_when_download_exceeds_the_ceiling(tmp_path: Path) -> None:
    system_id, job_id = uuid4(), uuid4()
    vol_name = pcap_volume_name(system_id, job_id)
    dest = f"/var/lib/libvirt/images/{vol_name}"
    # ceiling = max_bytes * 2 = 8 bytes; a 40-byte download overruns it mid-stream.
    conn = _FakeConn(pool=_FakePool(volumes={vol_name: _FakeVolume(vol_name, data=b"x" * 40)}))
    cap = _capturer(conn, tmp_path)
    with pytest.raises(CategorizedError) as exc:
        cap.fetch(dest, max_bytes=4)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.stream.aborted is True  # the overrun aborts the stream, never returns partial data


def test_reclaim_deletes_the_volume_and_tolerates_absent(tmp_path: Path) -> None:
    system_id, job_id = uuid4(), uuid4()
    vol_name = pcap_volume_name(system_id, job_id)
    dest = f"/var/lib/libvirt/images/{vol_name}"

    vol = _FakeVolume(vol_name)
    _capturer(_FakeConn(pool=_FakePool(volumes={vol_name: vol})), tmp_path).reclaim(dest)
    assert vol.deleted is True

    # A reclaim of an already-gone volume must not raise (idempotent teardown/cancel cleanup).
    _capturer(_FakeConn(pool=_FakePool()), tmp_path).reclaim(dest)


def test_write_remediation_is_the_remote_pool_remedy(tmp_path: Path) -> None:
    assert _capturer(_FakeConn(), tmp_path).write_remediation == REMOTE_PCAP_WRITE_REMEDIATION
