"""Unit tests for the remote host_dump dump-volume reaper helpers (ADR-0094, #301).

The libvirt I/O is live_vm-gated; these cover the pure name/mtime parsing that drives the
reconciler's live-holder guards, plus the DumpVolumeReaper protocol conformance.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import DumpVolumeReaper
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.reaping.dump_volume import (
    OpenDumpReaperConnection,
    RemoteLibvirtDumpVolumeReaper,
    system_id_from_dump_volume_name,
    volume_mtime_epoch_s,
)
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import host_dump_volume_name
from kdive.providers.remote_libvirt.transport import remote_libvirt_connections
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import libvirt_error

_SID = UUID("00000000-0000-0000-0000-0000000000cc")
_CERT_REFS = TlsCertRefs(
    client_cert_ref="secret://client-cert",
    client_key_ref="secret://client-key",  # pragma: allowlist secret
    ca_cert_ref="secret://ca-cert",
)


def test_reaper_satisfies_the_dump_volume_reaper_port() -> None:
    reaper = RemoteLibvirtDumpVolumeReaper.from_env(secret_registry=SecretRegistry())
    assert isinstance(reaper, DumpVolumeReaper)


def test_system_id_parses_from_the_deterministic_capture_name() -> None:
    # The reaper must parse exactly what the capture path writes.
    name = host_dump_volume_name(_SID)
    assert system_id_from_dump_volume_name(name) == _SID


def test_system_id_is_none_for_a_non_dump_name() -> None:
    assert system_id_from_dump_volume_name("some-overlay.qcow2") is None
    assert system_id_from_dump_volume_name("kdive-host-dump-not-a-uuid.kdump") is None


def test_mtime_reads_the_target_timestamps_mtime() -> None:
    xml = """
    <volume>
      <name>kdive-host-dump.kdump</name>
      <target>
        <timestamps><mtime>1700000000.123456</mtime></timestamps>
      </target>
    </volume>
    """
    assert volume_mtime_epoch_s(xml) == 1700000000.123456


def test_mtime_is_zero_when_absent_or_malformed() -> None:
    assert volume_mtime_epoch_s("<volume><target/></volume>") == 0.0
    assert volume_mtime_epoch_s("not xml at all <") == 0.0
    assert (
        volume_mtime_epoch_s(
            "<volume><target><timestamps><mtime>nope</mtime></timestamps></target></volume>"
        )
        == 0.0
    )


def test_list_dump_volumes_fans_out_over_the_fleet(tmp_path: Path) -> None:
    # Two declared hosts, each carrying one orphaned dump volume; the reaper lists across both.
    conn_a = _FakeConn()
    conn_b = _FakeConn()
    reaper = _fleet_reaper(
        {"qemu+tls://host-a.example/system": conn_a, "qemu+tls://host-b.example/system": conn_b},
        tmp_path,
    )

    volumes = asyncio.run(reaper.list_dump_volumes())

    assert len(volumes) == 2
    assert conn_a.closed and conn_b.closed


def test_delete_dump_volume_skips_hosts_without_the_volume(tmp_path: Path) -> None:
    # Host A does not have the volume (NO_STORAGE_VOL); host B does — the reaper deletes on B.
    conn_a = _FakeConn(volume_error=libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL))
    conn_b = _FakeConn()
    reaper = _fleet_reaper(
        {"qemu+tls://host-a.example/system": conn_a, "qemu+tls://host-b.example/system": conn_b},
        tmp_path,
    )

    asyncio.run(reaper.delete_dump_volume(host_dump_volume_name(_SID)))

    assert conn_a.pool.volume.deleted == 0
    assert conn_b.pool.volume.deleted == 1


def test_delete_dump_volume_treats_missing_volume_as_done(tmp_path: Path) -> None:
    conn = _FakeConn(volume_error=libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL))
    reaper = _reaper(conn, tmp_path)

    asyncio.run(reaper.delete_dump_volume(host_dump_volume_name(_SID)))

    assert conn.pool.lookups == [host_dump_volume_name(_SID)]
    assert conn.closed


def test_delete_dump_volume_preserves_non_absence_lookup_failures(tmp_path: Path) -> None:
    conn = _FakeConn(volume_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    reaper = _reaper(conn, tmp_path)

    with pytest.raises(CategorizedError) as raised:
        asyncio.run(reaper.delete_dump_volume(host_dump_volume_name(_SID)))

    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert conn.pool.lookups == [host_dump_volume_name(_SID)]
    assert conn.pool.volume.deleted == 0
    assert conn.closed


class _SecretBackend:
    def resolve(self, ref: str) -> str:
        return f"PEM::{ref}"


class _FakeVolume:
    def __init__(self) -> None:
        self.deleted = 0

    def name(self) -> str:
        return host_dump_volume_name(_SID)

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        del flags
        return (
            "<volume><target><timestamps><mtime>1700000000</mtime></timestamps></target></volume>"
        )

    def delete(self, flags: int = 0) -> int:
        del flags
        self.deleted += 1
        return 0


class _FakePool:
    def __init__(self, volume_error: libvirt.libvirtError | None = None) -> None:
        self._volume_error = volume_error
        self.volume = _FakeVolume()
        self.lookups: list[str] = []

    def listAllVolumes(self, flags: int = 0) -> list[_FakeVolume]:  # noqa: N802
        del flags
        return [self.volume]

    def storageVolLookupByName(self, name: str) -> _FakeVolume:  # noqa: N802
        self.lookups.append(name)
        if self._volume_error is not None:
            raise self._volume_error
        return self.volume

    def refresh(self, flags: int = 0) -> int:
        del flags
        return 0


class _FakeConn:
    def __init__(self, volume_error: libvirt.libvirtError | None = None) -> None:
        self.pool = _FakePool(volume_error)
        self.closed = False

    def storagePoolLookupByName(self, name: str) -> _FakePool:  # noqa: N802
        assert name == "default"
        return self.pool

    def close(self) -> None:
        self.closed = True


def _fleet_reaper(
    conns_by_uri: dict[str, _FakeConn], pki_base_dir: Path
) -> RemoteLibvirtDumpVolumeReaper:
    configs = [
        RemoteLibvirtConfig(uri=uri, cert_refs=_CERT_REFS, concurrent_allocation_cap=1)
        for uri in conns_by_uri
    ]

    def open_connection(uri: str) -> _FakeConn:
        for base, conn in conns_by_uri.items():
            if uri.startswith(base):
                return conn
        raise AssertionError(f"unexpected uri {uri!r}")

    return RemoteLibvirtDumpVolumeReaper(
        secret_registry=SecretRegistry(),
        connections=remote_libvirt_connections(
            secret_registry=SecretRegistry(),
            config_factory=lambda: configs[0],
            open_connection=cast(OpenDumpReaperConnection, open_connection),
            secret_backend_factory=_SecretBackend,
            pki_base_dir=pki_base_dir,
            configs_factory=lambda: configs,
        ),
    )


def _reaper(conn: _FakeConn, pki_base_dir: Path) -> RemoteLibvirtDumpVolumeReaper:
    config = RemoteLibvirtConfig(
        uri="qemu+tls://builder.example/system",
        cert_refs=_CERT_REFS,
        concurrent_allocation_cap=1,
    )

    def open_connection(uri: str) -> _FakeConn:
        del uri
        return conn

    return RemoteLibvirtDumpVolumeReaper(
        secret_registry=SecretRegistry(),
        connections=remote_libvirt_connections(
            secret_registry=SecretRegistry(),
            config_factory=lambda: config,
            open_connection=cast(OpenDumpReaperConnection, open_connection),
            secret_backend_factory=_SecretBackend,
            pki_base_dir=pki_base_dir,
        ),
    )
