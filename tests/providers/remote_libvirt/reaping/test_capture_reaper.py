"""Unit tests for the remote-libvirt orphaned-capture reaper (ADR-0556, #1947).

Every libvirt object here is a fake; real hosts are live_vm_remote-gated. The ordering test is
the control: deleting the volume before detaching the filter unlinks an inode QEMU keeps
writing, turning a bounded visible leak into an unbounded invisible one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import CaptureReaper, OrphanedCapture
from kdive.providers.ports.traffic import capture_qom_id, pcap_volume_name
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    TlsCertRefs,
    remote_config_for_resource,
)
from kdive.providers.remote_libvirt.reaping.capture import (
    OpenCaptureReaperConnection,
    RemoteLibvirtCaptureReaper,
)
from kdive.providers.remote_libvirt.reaping.connections import open_libvirt_reaper
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import libvirt_error

_SID = UUID("00000000-0000-0000-0000-0000000000cc")
_JID = UUID("00000000-0000-0000-0000-0000000000dd")

_CERT_REFS = TlsCertRefs(
    client_cert_ref="secret://client-cert",
    client_key_ref="secret://client-key",  # pragma: allowlist secret
    ca_cert_ref="secret://ca-cert",
)


def _capture() -> OrphanedCapture:
    return OrphanedCapture(
        provider_kind="remote-libvirt",
        resource_id=UUID("00000000-0000-0000-0000-0000000000aa"),
        resource_name="builder",
        system_id=_SID,
        domain_name="kdive-system-cc",
        job_id=_JID,
    )


class _FakeDomain:
    def __init__(self, calls: list[str], *, monitor_error: libvirt.libvirtError | None = None):
        self._calls = calls
        self._monitor_error = monitor_error
        self.del_commands: list[str] = []

    def qemuMonitorCommand(self, cmd: str, flags: int) -> str:  # noqa: N802
        del flags
        self._calls.append("object-del")
        self.del_commands.append(cmd)
        if self._monitor_error is not None:
            raise self._monitor_error
        return '{"return": {}}'


class _FakeVolume:
    def __init__(self, calls: list[str], *, delete_error: libvirt.libvirtError | None = None):
        self._calls = calls
        self._delete_error = delete_error

    def delete(self, flags: int = 0) -> int:
        del flags
        self._calls.append("volume-delete")
        if self._delete_error is not None:
            raise self._delete_error
        return 0


class _FakePool:
    def __init__(
        self,
        calls: list[str],
        *,
        volume: _FakeVolume | None = None,
        volume_error: libvirt.libvirtError | None = None,
    ):
        self._calls = calls
        self._volume = volume
        self._volume_error = volume_error
        self.lookups: list[str] = []

    def refresh(self, flags: int = 0) -> int:
        del flags
        self._calls.append("pool-refresh")
        return 0

    def storageVolLookupByName(self, name: str) -> _FakeVolume:  # noqa: N802
        self.lookups.append(name)
        self._calls.append("volume-lookup")
        if self._volume_error is not None:
            raise self._volume_error
        assert self._volume is not None
        return self._volume


class _FakeConn:
    def __init__(
        self,
        *,
        domain: _FakeDomain | None = None,
        domain_error: libvirt.libvirtError | None = None,
        pool: _FakePool | None = None,
    ):
        self.calls: list[str] = []
        self.domain = domain if domain is not None else _FakeDomain(self.calls)
        self._domain_error = domain_error
        self.pool = (
            pool if pool is not None else _FakePool(self.calls, volume=_FakeVolume(self.calls))
        )
        self.closed = False

    def lookupByName(self, name: str) -> _FakeDomain:  # noqa: N802
        del name
        self.calls.append("domain-lookup")
        if self._domain_error is not None:
            raise self._domain_error
        return self.domain

    def storagePoolLookupByName(self, name: str) -> _FakePool:  # noqa: N802
        assert name == "default"
        self.calls.append("pool-lookup")
        return self.pool

    def close(self) -> None:
        self.closed = True


class _SecretBackend:
    def resolve(self, ref: str) -> str:
        return f"PEM::{ref}"


def _reaper(
    conn: _FakeConn,
    tmp_path: Path,
    *,
    config_sink: list[str] | None = None,
) -> RemoteLibvirtCaptureReaper:
    """Build the reaper over ``conn`` with a fake TLS backend and no live config read."""

    def config_for_resource(resource_name: str) -> RemoteLibvirtConfig:
        if config_sink is not None:
            config_sink.append(resource_name)
        return RemoteLibvirtConfig(
            uri="qemu+tls://builder.example/system",
            cert_refs=_CERT_REFS,
            concurrent_allocation_cap=1,
        )

    def open_connection(uri: str) -> _FakeConn:
        del uri
        return conn

    return RemoteLibvirtCaptureReaper(
        secret_registry=SecretRegistry(),
        config_for_resource=cast(Callable[[str], RemoteLibvirtConfig], config_for_resource),
        open_connection=cast(OpenCaptureReaperConnection, open_connection),
        secret_backend_factory=_SecretBackend,
        pki_base_dir=tmp_path,
    )


def _reclaim(conn: _FakeConn, tmp_path: Path, *, config_sink: list[str] | None = None) -> bool:
    return asyncio.run(_reaper(conn, tmp_path, config_sink=config_sink).reclaim_capture(_capture()))


def test_reaper_satisfies_the_capture_reaper_port() -> None:
    reaper = RemoteLibvirtCaptureReaper.from_env(secret_registry=SecretRegistry())
    assert isinstance(reaper, CaptureReaper)


def test_detach_happens_before_the_volume_delete(tmp_path: Path) -> None:
    """The ordering is the whole control (ADR-0556): delete-first unlinks a live inode."""
    conn = _FakeConn()

    reclaimed = _reclaim(conn, tmp_path)

    assert reclaimed is True
    assert conn.calls.index("object-del") < conn.calls.index("volume-delete")
    # Both halves ran against the names the shared conventions produce.
    cmd = json.loads(conn.domain.del_commands[0])
    assert cmd == {"execute": "object-del", "arguments": {"id": capture_qom_id(_JID)}}
    assert conn.pool.lookups == [pcap_volume_name(_SID, _JID)]


def test_a_missing_filter_is_tolerated(tmp_path: Path) -> None:
    """A QMP object-del on an absent id must not fail the reclaim; the volume still goes."""
    calls: list[str] = []
    conn = _FakeConn(
        domain=_FakeDomain(
            calls, monitor_error=libvirt.libvirtError("object 'kdive-dump-x' not found")
        )
    )

    reclaimed = _reclaim(conn, tmp_path)

    assert reclaimed is True
    assert "volume-delete" in conn.calls


def test_a_missing_domain_is_tolerated(tmp_path: Path) -> None:
    """The domain stopped (or was reaped) after the capture died; there is no filter to detach."""
    conn = _FakeConn(domain_error=libvirt_error(libvirt.VIR_ERR_NO_DOMAIN))

    reclaimed = _reclaim(conn, tmp_path)

    assert reclaimed is True
    assert "object-del" not in conn.calls
    assert "volume-delete" in conn.calls


def test_a_missing_volume_is_tolerated(tmp_path: Path) -> None:
    calls: list[str] = []
    conn = _FakeConn(
        pool=_FakePool(calls, volume_error=libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL))
    )

    reclaimed = _reclaim(conn, tmp_path)

    assert reclaimed is True
    assert "object-del" in conn.calls  # the filter was still detached first


def test_a_non_not_found_filter_error_surfaces_as_categorized(tmp_path: Path) -> None:
    """A genuine monitor failure is not swallowed into a fake success."""
    calls: list[str] = []
    conn = _FakeConn(
        domain=_FakeDomain(calls, monitor_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    )

    with pytest.raises(CategorizedError) as raised:
        _reclaim(conn, tmp_path)

    assert raised.value.category is ErrorCategory.CONTROL_FAILURE
    assert "volume-delete" not in conn.calls  # never delete-first on an unknown filter state


def test_a_non_not_found_volume_error_surfaces_as_categorized(tmp_path: Path) -> None:
    calls: list[str] = []
    conn = _FakeConn(
        pool=_FakePool(
            calls,
            volume=_FakeVolume(calls, delete_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)),
        )
    )

    with pytest.raises(CategorizedError) as raised:
        _reclaim(conn, tmp_path)

    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert raised.value.details == {"volume": pcap_volume_name(_SID, _JID)}


def test_binding_resolves_the_row_resource_and_never_the_fleet(tmp_path: Path) -> None:
    """ADR-0187: one connection, bound to the captured Resource's own declared instance."""
    config_sink: list[str] = []
    conn = _FakeConn()

    reclaimed = _reclaim(conn, tmp_path, config_sink=config_sink)

    assert reclaimed is True
    assert config_sink == ["builder"]  # exactly the capture's resource_name, once
    assert conn.closed


def test_production_defaults_are_the_gated_opener_and_per_resource_config() -> None:
    """The fleet bundle is never wired: the default opener is the reaping seam's gated one."""
    reaper = RemoteLibvirtCaptureReaper.from_env(secret_registry=SecretRegistry())

    assert reaper._config_for_resource is remote_config_for_resource
    assert reaper._open_connection is open_libvirt_reaper
