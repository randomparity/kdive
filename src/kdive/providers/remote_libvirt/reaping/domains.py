"""Remote-libvirt reconciler ``InfraReaper`` adapter (ADR-0111, #1429).

Realizes the reconciler's :class:`~kdive.providers.infra.reaping.InfraReaper` port over the
``qemu+tls://`` remote-libvirt fleet, so a remote-only deployment reaps its own leaked
``kdive-<uuid>`` domains. A stock deployment gets the local-libvirt reaper; a remote-only one
(no local libvirt socket) previously composed a :class:`NullReaper` and never reclaimed a
leaked domain at all.

``list_owned`` fans out over the whole declared fleet with :func:`map_over_fleet`, enumerating
each host's kdive-owned domains — ownership is the kdive metadata tag when present, else the
``kdive-<uuid>`` naming convention (ADR-0111): the same predicate
:class:`~kdive.providers.local_libvirt.discovery.LocalLibvirtDiscovery` applies, so remote reaps
exactly what local would. ``destroy`` fans out with :func:`find_over_fleet` to the one host that
holds the named domain and runs the same destroy+undefine+overlay-reclaim teardown provisioning
does (ADR-0080 §4), idempotent over an already-absent domain/volume/pool — the contract the
composite's egress-probe fan-out relies on. The fleet helpers isolate an unreachable host (log +
skip), so one down host never aborts the fleet-wide sweep; a genuine libvirt error on a
*reachable* host still surfaces. The blocking libvirt calls run only under the ``live_vm`` gate;
the ownership predicate and per-host teardown are unit-tested directly with fakes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import OwnedDomain
from kdive.providers.remote_libvirt.connection.transport import RemoteLibvirtConnections
from kdive.providers.remote_libvirt.lifecycle.gdb import DOMAIN_PREFIX
from kdive.providers.remote_libvirt.lifecycle.storage import Pool, delete_volume
from kdive.providers.remote_libvirt.lifecycle.xml import disk_pool_strict, overlay_volume_name
from kdive.providers.remote_libvirt.reaping.connections import (
    find_over_fleet,
    map_over_fleet,
    open_libvirt_reaper,
    remote_libvirt_reaper_connections,
)
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS, parse_metadata_system_id
from kdive.providers.shared.runtime_paths import system_id_from_domain_name
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)


class _Domain(Protocol):
    """The domain slice the reaper reads/tears down (duck-typed seam)."""

    def name(self) -> str: ...
    def metadata(self, kind: int, uri: str | None, flags: int) -> str: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - libvirt binding name
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...


class _ReaperConn(Protocol):
    """The connection slice the reaper needs (list + look up + storage + close)."""

    def listAllDomains(self, flags: int = 0) -> list[_Domain]: ...  # noqa: N802 - binding name
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - binding name
    def storagePoolLookupByName(self, name: str) -> Pool: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


type OpenReaperConnection = Callable[[str], _ReaperConn]


@dataclass(frozen=True, slots=True)
class _OwnedDomain:
    """The reconciler ``OwnedDomain`` shape (``name`` + optional System id)."""

    name: str
    system_id: UUID | None


def _uuid_or_none(value: str) -> UUID | None:
    """Parse ``value`` to a ``UUID``; an empty or invalid string is ``None`` (never raises)."""
    try:
        return UUID(value)
    except ValueError:
        return None


def _name_fallback(name: str) -> OwnedDomain | None:
    """A convention-named domain with no usable tag is ours; else it is foreign and skipped.

    The System id is left ``None`` so the reconciler resolves it from the ``kdive-<uuid>`` name
    (ADR-0111) and can reap a genuinely orphaned domain that lost its metadata tag.
    """
    if system_id_from_domain_name(name) is None:
        return None
    return _OwnedDomain(name=name, system_id=None)


def _owned_domain(domain: _Domain) -> OwnedDomain | None:
    """Resolve one domain to an ``OwnedDomain``, or ``None`` when it is not kdive-owned.

    Ownership is the kdive metadata tag when present, else the ``kdive-<uuid>`` naming
    convention (ADR-0111) — the same predicate ``LocalLibvirtDiscovery.list_owned`` applies. A
    tagged System id is authoritative; an empty/malformed tag or an absent tag falls back to the
    naming convention. A domain that is neither tagged nor convention-named is skipped.
    """
    name = domain.name()
    try:
        meta = domain.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, KDIVE_METADATA_NS, 0)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
            return _name_fallback(name)  # no tag → try the naming convention
        raise _infra("reading domain metadata", domain=name) from exc
    tagged = parse_metadata_system_id(meta)
    if tagged is None:
        return _name_fallback(name)  # empty/malformed tag → naming convention
    return _OwnedDomain(name=name, system_id=_uuid_or_none(tagged))


def list_host_owned(conn: _ReaperConn) -> list[OwnedDomain]:
    """Return one host's kdive-owned domains in the reconciler ``OwnedDomain`` shape."""
    owned: list[OwnedDomain] = []
    for domain in conn.listAllDomains(0):
        entry = _owned_domain(domain)
        if entry is not None:
            owned.append(entry)
    return owned


def teardown_on_host(conn: _ReaperConn, storage_pool: str, name: str) -> bool:
    """Destroy+undefine the domain and reclaim its overlay on the host that holds it.

    Returns ``True`` when the domain was found (and torn down) on this host, ``False`` when it
    is not here — so :func:`find_over_fleet` moves on to the next host. An already-absent
    domain/volume/pool is an achieved post-state, so a destroy reaching a non-owner is harmless
    and the operation is idempotent (ADR-0080 §4).
    """
    try:
        domain = conn.lookupByName(name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return False  # not on this host (or already gone) — try the next
        raise _infra("looking up leaked domain", domain=name) from exc
    recorded_pool = _recorded_pool(domain, name)
    _destroy_and_undefine(domain, name)
    overlay_name = overlay_volume_name(name.removeprefix(DOMAIN_PREFIX))
    delete_volume(conn, recorded_pool or storage_pool, overlay_name)
    _log.info("reconciler: reaped leaked remote domain %s", name)
    return True


def _recorded_pool(domain: _Domain, name: str) -> str | None:
    """The storage pool the domain's disk records, or ``None`` if it is already unreadable."""
    try:
        return disk_pool_strict(domain.XMLDesc(), operation="leaked-domain teardown", domain=name)
    except libvirt.libvirtError:
        return None


def _destroy_and_undefine(domain: _Domain, name: str) -> None:
    """Destroy then undefine; ``not running`` and ``no such domain`` are achieved post-states."""
    try:
        domain.destroy()
    except libvirt.libvirtError as exc:
        if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
            raise _infra("destroying leaked domain", domain=name) from exc
    try:
        domain.undefine()
    except libvirt.libvirtError as exc:
        if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
            raise _infra("undefining leaked domain", domain=name) from exc


class RemoteLibvirtInfraReaper:
    """List + reap leaked domains across the remote-libvirt fleet (the reconciler seam)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        connections: RemoteLibvirtConnections[_ReaperConn] | None = None,
    ) -> None:
        self._connections = connections or remote_libvirt_reaper_connections(
            secret_registry=secret_registry,
            open_connection=open_libvirt_reaper,
        )

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtInfraReaper:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    async def list_owned(self) -> list[OwnedDomain]:
        """List every declared host's kdive-owned domains (offloaded; unreachable hosts skipped)."""
        return await asyncio.to_thread(self._list_blocking)

    async def destroy(self, name: str) -> None:
        """Reap one leaked domain by name on the host that holds it; idempotent (offloaded)."""
        await asyncio.to_thread(self._destroy_blocking, name)

    def _list_blocking(self) -> list[OwnedDomain]:  # pragma: no cover - live_vm thread body
        per_host = map_over_fleet(
            self._connections,
            lambda conn, _config: list_host_owned(conn),
            operation="leaked-domain list",
        )
        return [domain for host in per_host for domain in host]

    def _destroy_blocking(self, name: str) -> None:  # pragma: no cover - live_vm thread body
        find_over_fleet(
            self._connections,
            lambda conn, config: teardown_on_host(conn, config.storage_pool, name),
            operation="leaked-domain destroy",
        )


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )


__all__ = [
    "OpenReaperConnection",
    "RemoteLibvirtInfraReaper",
    "list_host_owned",
    "teardown_on_host",
]
