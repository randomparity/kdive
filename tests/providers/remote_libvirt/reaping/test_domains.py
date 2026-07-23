"""Unit tests for the remote-libvirt leaked-domain reaper (ADR-0111, #1429).

The libvirt I/O is ``live_vm``-gated; these cover the ownership predicate and the per-host
teardown directly (synchronous, fully hermetic) plus the fleet fan-out over fakes — an
unreachable host must be isolated, a genuine libvirt error must surface, and destroy must be
idempotent over an already-absent domain.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import InfraReaper, OwnedDomain
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.connection.transport import remote_libvirt_connections
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name
from kdive.providers.remote_libvirt.reaping.domains import (
    OpenReaperConnection,
    RemoteLibvirtInfraReaper,
    _owned_domain,
    _ReaperConn,
    list_host_owned,
    teardown_on_host,
)
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import libvirt_error

_SID = UUID("00000000-0000-0000-0000-0000000000dd")
_DOMAIN = domain_name_for(_SID)
_OVERLAY = overlay_volume_name(_SID)
_CERT_REFS = TlsCertRefs(
    client_cert_ref="secret://client-cert",
    client_key_ref="secret://client-key",  # pragma: allowlist secret
    ca_cert_ref="secret://ca-cert",
)
_METADATA_ERROR = libvirt_error(libvirt.VIR_ERR_NO_DOMAIN_METADATA)
_NO_DOMAIN = libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)


def _domain_xml(pool: str = "default") -> str:
    return f"<domain><devices><disk><source pool='{pool}'/></disk></devices></domain>"


def _list(conn: _FakeConn) -> list[OwnedDomain]:
    # The fakes duck-type the reaper's private conn slice; cast at the seam (list invariance).
    return list_host_owned(cast("_ReaperConn", conn))


def _teardown(conn: _FakeConn, pool: str, name: str) -> bool:
    return teardown_on_host(cast("_ReaperConn", conn), pool, name)


def test_reaper_satisfies_the_infra_reaper_port() -> None:
    reaper = RemoteLibvirtInfraReaper.from_env(secret_registry=SecretRegistry())
    assert isinstance(reaper, InfraReaper)


# --- ownership predicate ------------------------------------------------------------------


def test_owned_domain_uses_the_metadata_tag_when_present() -> None:
    domain = _FakeDomain(_DOMAIN, metadata=f"<system>{_SID}</system>")
    owned = _owned_domain(domain)
    assert owned is not None
    assert owned.name == _DOMAIN
    assert owned.system_id == _SID


def test_owned_domain_falls_back_to_the_naming_convention_without_a_tag() -> None:
    # No kdive metadata element, but the name matches kdive-<uuid>: still ours, system_id
    # deferred to the reconciler's name resolution (surfaced as None here).
    domain = _FakeDomain(_DOMAIN, metadata_error=_METADATA_ERROR)
    owned = _owned_domain(domain)
    assert owned is not None
    assert owned.name == _DOMAIN
    assert owned.system_id is None


def test_owned_domain_falls_back_when_the_tag_is_empty() -> None:
    domain = _FakeDomain(_DOMAIN, metadata="<system></system>")
    owned = _owned_domain(domain)
    assert owned is not None
    assert owned.system_id is None


def test_owned_domain_skips_a_foreign_untagged_domain() -> None:
    # Neither tagged nor kdive-<uuid> named → not ours → never reaped.
    domain = _FakeDomain("someone-elses-vm", metadata_error=_METADATA_ERROR)
    assert _owned_domain(domain) is None


def test_owned_domain_ignores_the_build_vm_naming_form() -> None:
    # kdive-build-<uuid> is an in-flight build VM (own sweep), not a leaked System domain.
    domain = _FakeDomain(f"kdive-build-{_SID}", metadata_error=_METADATA_ERROR)
    assert _owned_domain(domain) is None


def test_owned_domain_surfaces_a_non_absence_metadata_error() -> None:
    domain = _FakeDomain(_DOMAIN, metadata_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    with pytest.raises(CategorizedError) as raised:
        _owned_domain(domain)
    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert raised.value.details == {"domain": _DOMAIN}


def test_list_host_owned_filters_to_kdive_domains() -> None:
    mine = _FakeDomain(_DOMAIN, metadata_error=_METADATA_ERROR)
    foreign = _FakeDomain("postgres-vm", metadata_error=_METADATA_ERROR)
    conn = _FakeConn(domains=[mine, foreign])
    owned = _list(conn)
    assert [d.name for d in owned] == [_DOMAIN]


# --- per-host teardown --------------------------------------------------------------------


def test_teardown_reaps_the_domain_and_reclaims_its_overlay() -> None:
    domain = _FakeDomain(_DOMAIN, xml=_domain_xml("warm-pool"))
    conn = _FakeConn(domains=[domain])

    assert _teardown(conn, "default", _DOMAIN) is True

    assert domain.destroyed and domain.undefined
    # Overlay reclaimed from the pool the domain XML recorded, not the fallback config pool.
    assert conn.pool.deleted == [("warm-pool", _OVERLAY)]


def test_teardown_uses_the_config_pool_when_the_domain_records_none() -> None:
    domain = _FakeDomain(_DOMAIN, xml="<domain><devices><disk><source/></disk></devices></domain>")
    conn = _FakeConn(domains=[domain])

    assert _teardown(conn, "fallback-pool", _DOMAIN) is True
    assert conn.pool.deleted == [("fallback-pool", _OVERLAY)]


def test_teardown_reports_a_domain_absent_on_this_host() -> None:
    conn = _FakeConn(lookup_error=_NO_DOMAIN)
    assert _teardown(conn, "default", _DOMAIN) is False
    assert conn.pool.deleted == []


def test_teardown_surfaces_a_non_absence_lookup_error() -> None:
    conn = _FakeConn(lookup_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    with pytest.raises(CategorizedError) as raised:
        _teardown(conn, "default", _DOMAIN)
    assert str(raised.value) == "libvirt error looking up leaked domain"
    assert raised.value.details == {"domain": _DOMAIN}


def test_teardown_tolerates_a_domain_that_is_not_running() -> None:
    # destroy() of a stopped domain raises OPERATION_INVALID — an achieved post-state.
    domain = _FakeDomain(_DOMAIN, destroy_error=libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID))
    conn = _FakeConn(domains=[domain])
    assert _teardown(conn, "default", _DOMAIN) is True
    assert domain.undefined
    assert conn.pool.deleted == [("default", _OVERLAY)]


def test_teardown_tolerates_a_domain_undefined_between_destroy_and_undefine() -> None:
    domain = _FakeDomain(_DOMAIN, undefine_error=_NO_DOMAIN)
    conn = _FakeConn(domains=[domain])
    assert _teardown(conn, "default", _DOMAIN) is True
    assert conn.pool.deleted == [("default", _OVERLAY)]


def test_teardown_surfaces_a_genuine_destroy_failure() -> None:
    domain = _FakeDomain(_DOMAIN, destroy_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    conn = _FakeConn(domains=[domain])
    with pytest.raises(CategorizedError) as raised:
        _teardown(conn, "default", _DOMAIN)
    assert str(raised.value) == "libvirt error destroying leaked domain"


# --- fleet fan-out ------------------------------------------------------------------------


def test_list_owned_fans_out_over_the_fleet(tmp_path: Path) -> None:
    conn_a = _FakeConn(domains=[_FakeDomain(_DOMAIN, metadata_error=_METADATA_ERROR)])
    other = domain_name_for(UUID("11111111-1111-1111-1111-111111111111"))
    conn_b = _FakeConn(domains=[_FakeDomain(other, metadata_error=_METADATA_ERROR)])
    reaper = _fleet_reaper(
        {"qemu+tls://host-a.example/system": conn_a, "qemu+tls://host-b.example/system": conn_b},
        tmp_path,
    )

    owned = asyncio.run(reaper.list_owned())

    assert {d.name for d in owned} == {_DOMAIN, other}
    assert conn_a.closed and conn_b.closed


def test_list_owned_isolates_an_unreachable_host(tmp_path: Path) -> None:
    # Host A is unreachable (connect raises); host B's leaked domain is still listed — one down
    # host must not strand the fleet-wide sweep (the criterion that protects local reaping too).
    reachable = _FakeConn(domains=[_FakeDomain(_DOMAIN, metadata_error=_METADATA_ERROR)])
    reaper = _fleet_reaper(
        {"qemu+tls://host-b.example/system": reachable},
        tmp_path,
        unreachable=("qemu+tls://host-a.example/system",),
    )

    owned = asyncio.run(reaper.list_owned())

    assert [d.name for d in owned] == [_DOMAIN]


def test_destroy_reaps_on_the_host_that_holds_the_domain(tmp_path: Path) -> None:
    absent = _FakeConn(lookup_error=_NO_DOMAIN)
    holder = _FakeConn(domains=[_FakeDomain(_DOMAIN, xml=_domain_xml())])
    reaper = _fleet_reaper(
        {"qemu+tls://host-a.example/system": absent, "qemu+tls://host-b.example/system": holder},
        tmp_path,
    )

    asyncio.run(reaper.destroy(_DOMAIN))

    assert holder.domains[_DOMAIN].destroyed
    assert holder.pool.deleted == [("default", _OVERLAY)]


def test_destroy_is_idempotent_when_no_host_holds_the_domain(tmp_path: Path) -> None:
    # An egress-probe name (or an already-gone domain) is absent everywhere — a benign no-op.
    conn_a = _FakeConn(lookup_error=_NO_DOMAIN)
    conn_b = _FakeConn(lookup_error=_NO_DOMAIN)
    reaper = _fleet_reaper(
        {"qemu+tls://host-a.example/system": conn_a, "qemu+tls://host-b.example/system": conn_b},
        tmp_path,
    )

    asyncio.run(reaper.destroy("kdive-egress-probe-xyz"))  # never raises

    assert conn_a.pool.deleted == [] and conn_b.pool.deleted == []


# --- fakes --------------------------------------------------------------------------------


class _SecretBackend:
    def resolve(self, ref: str) -> str:
        return f"PEM::{ref}"


class _FakeDomain:
    def __init__(
        self,
        name: str,
        *,
        metadata: str | None = None,
        metadata_error: libvirt.libvirtError | None = None,
        xml: str = "<domain/>",
        destroy_error: libvirt.libvirtError | None = None,
        undefine_error: libvirt.libvirtError | None = None,
    ) -> None:
        self._name = name
        self._metadata = metadata
        self._metadata_error = metadata_error
        self._xml = xml
        self._destroy_error = destroy_error
        self._undefine_error = undefine_error
        self.destroyed = False
        self.undefined = False

    def name(self) -> str:
        return self._name

    def metadata(self, kind: int, uri: str | None, flags: int) -> str:
        del kind, flags
        assert uri == KDIVE_METADATA_NS
        if self._metadata_error is not None:
            raise self._metadata_error
        assert self._metadata is not None
        return self._metadata

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        del flags
        return self._xml

    def destroy(self) -> int:
        if self._destroy_error is not None:
            raise self._destroy_error
        self.destroyed = True
        return 0

    def undefine(self) -> int:
        if self._undefine_error is not None:
            raise self._undefine_error
        self.undefined = True
        return 0


class _FakeVolume:
    def __init__(self) -> None:
        self.deleted = False

    def path(self) -> str:
        return "/dev/null"

    def info(self) -> list[int]:
        return [0, 0, 0]

    def delete(self, flags: int = 0) -> int:
        del flags
        self.deleted = True
        return 0


class _FakePool:
    def __init__(self, deleted: list[tuple[str, str]], pool_name: str) -> None:
        self._deleted = deleted
        self._pool_name = pool_name

    def storageVolLookupByName(self, name: str) -> _FakeVolume:  # noqa: N802
        self._deleted.append((self._pool_name, name))
        return _FakeVolume()

    def createXML(self, xml: str, flags: int = 0) -> _FakeVolume:  # noqa: N802
        del xml, flags
        return _FakeVolume()


class _FakeConn:
    def __init__(
        self,
        *,
        domains: list[_FakeDomain] | None = None,
        lookup_error: libvirt.libvirtError | None = None,
    ) -> None:
        self.domains = {d.name(): d for d in (domains or [])}
        self._lookup_error = lookup_error
        self.pool = _FakePoolRegistry()
        self.closed = False

    def listAllDomains(self, flags: int = 0) -> list[_FakeDomain]:  # noqa: N802
        del flags
        return list(self.domains.values())

    def lookupByName(self, name: str) -> _FakeDomain:  # noqa: N802
        if self._lookup_error is not None:
            raise self._lookup_error
        domain = self.domains.get(name)
        if domain is None:
            raise _NO_DOMAIN
        return domain

    def storagePoolLookupByName(self, name: str) -> _FakePool:  # noqa: N802
        return _FakePool(self.pool.deleted, name)

    def close(self) -> None:
        self.closed = True


class _FakePoolRegistry:
    """Records ``(pool_name, volume_name)`` deletions across the conn's storage pools."""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []


def _fleet_reaper(
    conns_by_uri: dict[str, _FakeConn],
    pki_base_dir: Path,
    *,
    unreachable: tuple[str, ...] = (),
) -> RemoteLibvirtInfraReaper:
    configs = [
        RemoteLibvirtConfig(uri=uri, cert_refs=_CERT_REFS, concurrent_allocation_cap=1)
        for uri in (*conns_by_uri, *unreachable)
    ]

    def open_connection(uri: str) -> _FakeConn:
        for base in unreachable:
            if uri.startswith(base):
                raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)
        for base, conn in conns_by_uri.items():
            if uri.startswith(base):
                return conn
        raise AssertionError(f"unexpected uri {uri!r}")

    return RemoteLibvirtInfraReaper(
        secret_registry=SecretRegistry(),
        connections=remote_libvirt_connections(
            secret_registry=SecretRegistry(),
            config_factory=lambda: configs[0],
            open_connection=cast("OpenReaperConnection", open_connection),
            secret_backend_factory=_SecretBackend,
            pki_base_dir=pki_base_dir,
            configs_factory=lambda: configs,
        ),
    )
