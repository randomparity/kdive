"""Unit tests for the ephemeral build-VM reaper helpers (ADR-0100).

The libvirt list/delete I/O is live_vm-gated; these cover the pure domain-name parsing that
drives the reconciler's job-liveness guard, plus the BuildVmReaper protocol conformance and
the disjointness from the System / dump-volume name schemes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from uuid import UUID

import libvirt

from kdive.providers.core.runtime_paths import domain_name_for
from kdive.providers.infra.reaping import BuildVmReaper
from kdive.providers.remote_libvirt.build_vm_reaper import (
    OpenReaperConnection,
    RemoteLibvirtBuildVmReaper,
    run_id_from_build_vm_name,
)
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.dump_volume_reaper import system_id_from_dump_volume_name
from kdive.providers.remote_libvirt.lifecycle.build_vm import (
    build_domain_name,
    build_overlay_volume_name,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import libvirt_error

_RID = UUID("00000000-0000-0000-0000-00000000ca11")
_OTHER_RID = UUID("00000000-0000-0000-0000-00000000ca12")
_CERT_REFS = TlsCertRefs(
    client_cert_ref="secret://client-cert",
    client_key_ref="secret://client-key",  # pragma: allowlist secret
    ca_cert_ref="secret://ca-cert",
)


def test_reaper_satisfies_the_build_vm_reaper_port() -> None:
    reaper = RemoteLibvirtBuildVmReaper.from_env(secret_registry=SecretRegistry())
    assert isinstance(reaper, BuildVmReaper)


def test_run_id_parses_from_the_build_domain_name() -> None:
    assert run_id_from_build_vm_name(build_domain_name(_RID)) == _RID


def test_run_id_is_none_for_non_build_names() -> None:
    # A System domain (kdive-<uuid>) must NOT parse as a build VM (disjoint namespaces).
    assert run_id_from_build_vm_name(domain_name_for(_RID)) is None
    assert run_id_from_build_vm_name("kdive-build-not-a-uuid") is None
    assert run_id_from_build_vm_name("unrelated") is None


def test_build_vm_name_is_not_a_dump_volume_name() -> None:
    # The build-domain marker and the dump-volume marker must be mutually exclusive.
    assert system_id_from_dump_volume_name(build_domain_name(_RID)) is None


def test_list_build_vms_filters_domains_and_parses_run_ids(tmp_path) -> None:
    conn = _FakeConn(
        domains=[
            _FakeDomain(build_domain_name(_RID)),
            _FakeDomain(domain_name_for(_RID)),
            _FakeDomain("kdive-build-not-a-uuid"),
            _FakeDomain(build_domain_name(_OTHER_RID)),
        ],
    )
    reaper = _reaper(conn, tmp_path)

    result = asyncio.run(reaper.list_build_vms())

    assert [(vm.domain_name, vm.run_id) for vm in result] == [
        (build_domain_name(_RID), _RID),
        ("kdive-build-not-a-uuid", None),
        (build_domain_name(_OTHER_RID), _OTHER_RID),
    ]
    assert conn.closed


def test_delete_build_vm_destroys_domain_and_deletes_overlay(tmp_path) -> None:
    domain = _FakeDomain(build_domain_name(_RID))
    volume = _FakeVolume()
    conn = _FakeConn(domains=[domain], volume=volume)
    reaper = _reaper(conn, tmp_path)

    asyncio.run(reaper.delete_build_vm(build_domain_name(_RID)))

    assert domain.destroyed == 1
    assert domain.undefined == 1
    assert conn.pool.lookups == [build_overlay_volume_name(_RID)]
    assert volume.deleted == 1
    assert conn.closed


def test_delete_build_vm_treats_missing_domain_and_overlay_as_done(tmp_path) -> None:
    conn = _FakeConn(
        domains=[],
        lookup_error=libvirt_error(libvirt.VIR_ERR_NO_DOMAIN),
        volume_error=libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL),
    )
    reaper = _reaper(conn, tmp_path)

    asyncio.run(reaper.delete_build_vm(build_domain_name(_RID)))

    assert conn.pool.lookups == [build_overlay_volume_name(_RID)]
    assert conn.closed


def test_delete_build_vm_tolerates_already_inactive_or_undefined_domain(tmp_path) -> None:
    domain = _FakeDomain(
        build_domain_name(_RID),
        destroy_error=libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID),
        undefine_error=libvirt_error(libvirt.VIR_ERR_NO_DOMAIN),
    )
    conn = _FakeConn(domains=[domain])
    reaper = _reaper(conn, tmp_path)

    asyncio.run(reaper.delete_build_vm(build_domain_name(_RID)))

    assert domain.destroyed == 1
    assert domain.undefined == 1
    assert conn.pool.lookups == [build_overlay_volume_name(_RID)]
    assert conn.closed


def test_delete_build_vm_skips_overlay_delete_for_malformed_build_name(tmp_path) -> None:
    domain = _FakeDomain("kdive-build-not-a-uuid")
    conn = _FakeConn(domains=[domain])
    reaper = _reaper(conn, tmp_path)

    asyncio.run(reaper.delete_build_vm("kdive-build-not-a-uuid"))

    assert domain.destroyed == 1
    assert domain.undefined == 1
    assert conn.pool.lookups == []
    assert conn.closed


class _SecretBackend:
    def resolve(self, ref: str) -> str:
        return f"PEM::{ref}"


class _FakeDomain:
    def __init__(
        self,
        name: str,
        *,
        destroy_error: libvirt.libvirtError | None = None,
        undefine_error: libvirt.libvirtError | None = None,
    ) -> None:
        self._name = name
        self._destroy_error = destroy_error
        self._undefine_error = undefine_error
        self.destroyed = 0
        self.undefined = 0

    def name(self) -> str:
        return self._name

    def destroy(self) -> int:
        self.destroyed += 1
        if self._destroy_error is not None:
            raise self._destroy_error
        return 0

    def undefine(self) -> int:
        self.undefined += 1
        if self._undefine_error is not None:
            raise self._undefine_error
        return 0


class _FakeVolume:
    def __init__(self) -> None:
        self.deleted = 0

    def delete(self, flags: int = 0) -> int:
        del flags
        self.deleted += 1
        return 0


class _FakePool:
    def __init__(
        self,
        *,
        volume: _FakeVolume | None = None,
        volume_error: libvirt.libvirtError | None = None,
    ) -> None:
        self._volume = volume or _FakeVolume()
        self._volume_error = volume_error
        self.lookups: list[str] = []

    def storageVolLookupByName(self, name: str) -> _FakeVolume:  # noqa: N802
        self.lookups.append(name)
        if self._volume_error is not None:
            raise self._volume_error
        return self._volume


class _FakeConn:
    def __init__(
        self,
        *,
        domains: list[_FakeDomain],
        lookup_error: libvirt.libvirtError | None = None,
        volume: _FakeVolume | None = None,
        volume_error: libvirt.libvirtError | None = None,
        pool_error: libvirt.libvirtError | None = None,
    ) -> None:
        self._domains = {domain.name(): domain for domain in domains}
        self._lookup_error = lookup_error
        self._pool_error = pool_error
        self.pool = _FakePool(volume=volume, volume_error=volume_error)
        self.closed = False

    def listAllDomains(self, flags: int = 0) -> list[_FakeDomain]:  # noqa: N802
        del flags
        return list(self._domains.values())

    def lookupByName(self, name: str) -> _FakeDomain:  # noqa: N802
        if self._lookup_error is not None:
            raise self._lookup_error
        try:
            return self._domains[name]
        except KeyError:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN) from None

    def storagePoolLookupByName(self, name: str) -> _FakePool:  # noqa: N802
        assert name == "default"
        if self._pool_error is not None:
            raise self._pool_error
        return self.pool

    def close(self) -> None:
        self.closed = True


def _reaper(conn: _FakeConn, pki_base_dir: Path) -> RemoteLibvirtBuildVmReaper:
    config = RemoteLibvirtConfig(
        uri="qemu+tls://builder.example/system",
        cert_refs=_CERT_REFS,
        concurrent_allocation_cap=1,
    )

    def open_connection(uri: str) -> _FakeConn:
        del uri
        return conn

    return RemoteLibvirtBuildVmReaper(
        secret_registry=SecretRegistry(),
        config_factory=lambda: config,
        open_connection=cast(OpenReaperConnection, open_connection),
        secret_backend_factory=_SecretBackend,
        pki_base_dir=pki_base_dir,
    )
