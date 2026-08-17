"""Unit tests for the remote host_dump dump-volume reaper helpers (ADR-0094, #301, ADR-0562).

These cover the pure name/mtime parsing that drives the reconciler's live-holder guards, the
fleet fan-out, the identity re-read that makes the delete refuse a volume the reconciler never
classified, and the DumpVolumeReaper protocol conformance. Real hosts are live_vm-gated; every
libvirt object here is a fake.
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
from kdive.providers.remote_libvirt.connection.transport import remote_libvirt_connections
from kdive.providers.remote_libvirt.reaping.dump_volume import (
    OpenDumpReaperConnection,
    RemoteLibvirtDumpVolumeReaper,
    system_id_from_dump_volume_name,
    volume_mtime_epoch_s,
)
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import host_dump_volume_name
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import libvirt_error

_SID = UUID("00000000-0000-0000-0000-0000000000cc")
_CERT_REFS = TlsCertRefs(
    client_cert_ref="secret://client-cert",
    client_key_ref="secret://client-key",  # pragma: allowlist secret
    ca_cert_ref="secret://ca-cert",
)
#: The mtime ``_FakeVolume`` reports by default — i.e. the identity a reconciler sample of it
#: carries, and therefore the value a delete of that same volume is addressed to.
_SAMPLED_MTIME = 1700000000.0


async def _delete(
    reaper: RemoteLibvirtDumpVolumeReaper, *, expected_mtime_epoch_s: float = _SAMPLED_MTIME
) -> None:
    """Delete the fixture volume, addressed by default to the identity the fake reports."""
    await reaper.delete_dump_volume(
        host_dump_volume_name(_SID), expected_mtime_epoch_s=expected_mtime_epoch_s
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


def test_list_reports_each_volumes_name_system_id_and_mtime(tmp_path: Path) -> None:
    # A single deterministic dump volume must round-trip every field the reconciler reads:
    # its name, the System UUID parsed from that name, and the store mtime from its XML.
    conn = _FakeConn()
    reaper = _reaper(conn, tmp_path)

    volumes = asyncio.run(reaper.list_dump_volumes())

    assert len(volumes) == 1
    (volume,) = volumes
    assert volume.name == host_dump_volume_name(_SID)
    assert volume.system_id == _SID
    assert volume.mtime_epoch_s == 1700000000.0


def test_list_keeps_dump_prefixed_volumes_without_a_parseable_system_id(tmp_path: Path) -> None:
    # A kdive-host-dump- volume whose suffix is not a UUID has system_id=None but is still
    # reported (the reconciler must see it to reap it); a foreign volume is filtered out.
    matching = _FakeVolume(host_dump_volume_name(_SID))
    prefix_only = _FakeVolume("kdive-host-dump-not-a-uuid.kdump")
    foreign = _FakeVolume("some-overlay.qcow2")
    conn = _FakeConn(volumes=[matching, prefix_only, foreign])
    reaper = _reaper(conn, tmp_path)

    volumes = asyncio.run(reaper.list_dump_volumes())

    reported = {vol.name: vol.system_id for vol in volumes}
    assert reported == {
        host_dump_volume_name(_SID): _SID,
        "kdive-host-dump-not-a-uuid.kdump": None,
    }


def test_delete_dump_volume_skips_hosts_without_the_volume(tmp_path: Path) -> None:
    # Host A does not have the volume (NO_STORAGE_VOL); host B does — the reaper deletes on B.
    conn_a = _FakeConn(volume_error=libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL))
    conn_b = _FakeConn()
    reaper = _fleet_reaper(
        {"qemu+tls://host-a.example/system": conn_a, "qemu+tls://host-b.example/system": conn_b},
        tmp_path,
    )

    asyncio.run(_delete(reaper))

    assert conn_a.pool.volume.deleted == 0
    assert conn_b.pool.volume.deleted == 1


def test_delete_stops_at_the_first_host_that_holds_the_volume(tmp_path: Path) -> None:
    # Delete-by-name reaps a single host's copy: once a host reports it handled the volume the
    # fan-out stops, so a later host that also has the name is never touched.
    conn_a = _FakeConn()
    conn_b = _FakeConn()
    reaper = _fleet_reaper(
        {"qemu+tls://host-a.example/system": conn_a, "qemu+tls://host-b.example/system": conn_b},
        tmp_path,
    )

    asyncio.run(_delete(reaper))

    assert conn_a.pool.volume.deleted == 1
    assert conn_b.pool.volume.deleted == 0
    assert conn_b.pool.lookups == []


def test_delete_dump_volume_treats_missing_volume_as_done(tmp_path: Path) -> None:
    conn = _FakeConn(volume_error=libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL))
    reaper = _reaper(conn, tmp_path)

    asyncio.run(_delete(reaper))

    assert conn.pool.lookups == [host_dump_volume_name(_SID)]
    assert conn.closed


def test_delete_dump_volume_preserves_non_absence_lookup_failures(tmp_path: Path) -> None:
    conn = _FakeConn(volume_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    reaper = _reaper(conn, tmp_path)

    with pytest.raises(CategorizedError) as raised:
        asyncio.run(_delete(reaper))

    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(raised.value) == "libvirt error looking up host_dump volume"
    assert raised.value.details == {"volume": host_dump_volume_name(_SID)}
    assert conn.pool.lookups == [host_dump_volume_name(_SID)]
    assert conn.pool.volume.deleted == 0
    assert conn.closed


def test_delete_surfaces_libvirt_failures_raised_by_the_delete_call(tmp_path: Path) -> None:
    # A non-absence error from delete() (not lookup) is wrapped as an infrastructure failure
    # carrying the volume name, not swallowed.
    conn = _FakeConn(delete_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    reaper = _reaper(conn, tmp_path)

    with pytest.raises(CategorizedError) as raised:
        asyncio.run(_delete(reaper))

    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(raised.value) == "libvirt error deleting host_dump volume"
    assert raised.value.details == {"volume": host_dump_volume_name(_SID)}


def test_delete_declines_a_volume_recreated_since_the_reconciler_sampled_it(tmp_path: Path) -> None:
    """The identity guard (ADR-0562): the deterministic name is reused by every capture.

    The reconciler classified a volume with one mtime; the volume present under that name now has
    another, which means a capture deleted the orphan and dumped into a fresh one. Deleting it would
    unlink a core that ``coreDumpWithFormat`` is writing or ``virStorageVolDownload`` is streaming.
    """
    conn = _FakeConn()
    reaper = _reaper(conn, tmp_path)

    asyncio.run(_delete(reaper, expected_mtime_epoch_s=_SAMPLED_MTIME - 3600))

    assert conn.pool.lookups == [host_dump_volume_name(_SID)]
    assert conn.pool.volume.xml_reads == 1  # the re-read happened
    assert conn.pool.volume.deleted == 0  # and it stopped the delete
    assert conn.closed


def test_delete_deletes_when_the_identity_still_matches(tmp_path: Path) -> None:
    """The counterpart, so the guard above is not merely "never deletes".

    Without this arm the identity check could reject every volume — including every genuine orphan —
    and the decline test would still pass, leaving a reaper that reclaims nothing.
    """
    conn = _FakeConn()
    reaper = _reaper(conn, tmp_path)

    asyncio.run(_delete(reaper, expected_mtime_epoch_s=_SAMPLED_MTIME))

    assert conn.pool.volume.xml_reads == 1
    assert conn.pool.volume.deleted == 1


def test_a_declined_volume_stops_the_fleet_fan_out(tmp_path: Path) -> None:
    """A host that holds the name has handled it, deleted or not — the fan-out must not continue.

    Reporting "not mine" on a decline would send the sweep looking for another host's copy of the
    same deterministic name, and delete *that* one instead: the fan-out's stop condition is which
    host holds the volume, not whether anything was removed.
    """
    conn_a = _FakeConn()
    conn_b = _FakeConn()
    reaper = _fleet_reaper(
        {"qemu+tls://host-a.example/system": conn_a, "qemu+tls://host-b.example/system": conn_b},
        tmp_path,
    )

    asyncio.run(_delete(reaper, expected_mtime_epoch_s=_SAMPLED_MTIME - 3600))

    assert conn_a.pool.volume.deleted == 0
    assert conn_b.pool.lookups == []  # never consulted
    assert conn_b.pool.volume.deleted == 0


def test_delete_surfaces_libvirt_failures_from_the_identity_re_read(tmp_path: Path) -> None:
    """A failed re-read is an infrastructure fault, not a silent decline or a blind delete.

    Treating it as a decline would hide a broken host behind a clean sweep count; treating it as a
    match would delete on no evidence at all.
    """
    conn = _FakeConn(xml_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    reaper = _reaper(conn, tmp_path)

    with pytest.raises(CategorizedError) as raised:
        asyncio.run(_delete(reaper))

    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(raised.value) == "libvirt error re-reading host_dump volume identity"
    assert raised.value.details == {"volume": host_dump_volume_name(_SID)}
    assert conn.pool.volume.deleted == 0


class _SecretBackend:
    def resolve(self, ref: str) -> str:
        return f"PEM::{ref}"


class _FakeVolume:
    def __init__(
        self,
        name: str | None = None,
        *,
        delete_error: libvirt.libvirtError | None = None,
        mtime: str = "1700000000",
        xml_error: libvirt.libvirtError | None = None,
    ) -> None:
        self._name = name if name is not None else host_dump_volume_name(_SID)
        self._delete_error = delete_error
        self._mtime = mtime
        self._xml_error = xml_error
        self.deleted = 0
        self.xml_reads = 0

    def name(self) -> str:
        return self._name

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        del flags
        self.xml_reads += 1
        if self._xml_error is not None:
            raise self._xml_error
        return (
            f"<volume><target><timestamps><mtime>{self._mtime}</mtime>"
            "</timestamps></target></volume>"
        )

    def delete(self, flags: int = 0) -> int:
        del flags
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted += 1
        return 0


class _FakePool:
    def __init__(
        self,
        volume_error: libvirt.libvirtError | None = None,
        *,
        delete_error: libvirt.libvirtError | None = None,
        volumes: list[_FakeVolume] | None = None,
        xml_error: libvirt.libvirtError | None = None,
    ) -> None:
        self._volume_error = volume_error
        self.volumes = (
            volumes
            if volumes is not None
            else [_FakeVolume(delete_error=delete_error, xml_error=xml_error)]
        )
        self.volume = self.volumes[0]
        self.lookups: list[str] = []

    def listAllVolumes(self, flags: int = 0) -> list[_FakeVolume]:  # noqa: N802
        del flags
        return self.volumes

    def storageVolLookupByName(self, name: str) -> _FakeVolume:  # noqa: N802
        self.lookups.append(name)
        if self._volume_error is not None:
            raise self._volume_error
        return self.volume

    def refresh(self, flags: int = 0) -> int:
        del flags
        return 0


class _FakeConn:
    def __init__(
        self,
        volume_error: libvirt.libvirtError | None = None,
        *,
        delete_error: libvirt.libvirtError | None = None,
        volumes: list[_FakeVolume] | None = None,
        xml_error: libvirt.libvirtError | None = None,
    ) -> None:
        self.pool = _FakePool(
            volume_error, delete_error=delete_error, volumes=volumes, xml_error=xml_error
        )
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
