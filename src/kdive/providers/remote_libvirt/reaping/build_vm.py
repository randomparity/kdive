"""Remote-libvirt ephemeral build-VM reaper (ADR-0100).

The reconciler's build-host upkeep consumes this provider port (the ``BuildVmReaper`` contract)
to reap ``kdive-build-<run_id>`` domains leaked by a worker/host crash that bypassed the
session's ``finally`` teardown. It lists the host's build domains (matched by the deterministic
name) and deletes one — destroy + undefine + delete its overlay — by name. The reconciler owns
the live-holder guard (the owning BUILD job's liveness, never elapsed time); this port is the
narrow libvirt I/O seam. The blocking libvirt calls run only under the ``live_vm`` gate;
name parsing + protocol conformance are unit-tested.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import BuildVm
from kdive.providers.remote_libvirt.lifecycle.build_vm import (
    BUILD_DOMAIN_PREFIX,
    build_overlay_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.storage import delete_volume
from kdive.providers.remote_libvirt.reaping.connections import (
    open_libvirt_reaper,
    remote_libvirt_reaper_connections,
)
from kdive.providers.remote_libvirt.transport import (
    RemoteLibvirtConnections,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)

# The deterministic build-domain name carries the owning Run's UUID (ADR-0100). Anchored so a
# System domain (kdive-<uuid>, no "build-") can never match.
_BUILD_VM_RE = re.compile(
    r"^kdive-build-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def run_id_from_build_vm_name(name: str) -> UUID | None:
    """The owning Run UUID encoded in a build-VM domain name, or ``None`` if it does not match."""
    match = _BUILD_VM_RE.match(name)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:  # pragma: no cover - the regex already constrains the shape
        return None


class _Domain(Protocol):
    def name(self) -> str: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...


class _ReaperConn(Protocol):
    def listAllDomains(self, flags: int = 0) -> list[_Domain]: ...  # noqa: N802 - binding name
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - binding name
    def storagePoolLookupByName(self, name: str) -> Any: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


type OpenReaperConnection = Callable[[str], _ReaperConn]


class RemoteLibvirtBuildVmReaper:
    """List + delete leaked ephemeral build-VM domains on the remote host (the reconciler seam)."""

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
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtBuildVmReaper:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    async def list_build_vms(self) -> list[BuildVm]:
        """List the host's ``kdive-build-*`` domains with their owning Run id (offloaded)."""
        return await asyncio.to_thread(self._list_blocking)

    async def delete_build_vm(self, domain_name: str) -> None:
        """Destroy+undefine the domain and delete its overlay; already-gone is not an error."""
        await asyncio.to_thread(self._delete_blocking, domain_name)

    def _list_blocking(self) -> list[BuildVm]:  # pragma: no cover - live_vm
        config = self._connections.config()
        with self._connections.connection(config) as conn:
            vms: list[BuildVm] = []
            for domain in conn.listAllDomains(0):
                name = domain.name()
                if not name.startswith(BUILD_DOMAIN_PREFIX):
                    continue
                vms.append(BuildVm(domain_name=name, run_id=run_id_from_build_vm_name(name)))
            return vms

    def _delete_blocking(self, domain_name: str) -> None:  # pragma: no cover - live_vm
        config = self._connections.config()
        run_id = run_id_from_build_vm_name(domain_name)
        with self._connections.connection(config) as conn:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    domain = None
                else:
                    raise _infra("looking up build VM domain", domain=domain_name) from exc
            if domain is not None:
                self._destroy_undefine(domain, domain_name)
            if run_id is not None:
                delete_volume(conn, config.storage_pool, build_overlay_volume_name(run_id))
            _log.info("reconciler: reaped leaked build VM %s", domain_name)

    @staticmethod
    def _destroy_undefine(domain: _Domain, domain_name: str) -> None:  # pragma: no cover - live_vm
        try:
            domain.destroy()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                _log.warning("build VM %s destroy failed during reap", domain_name)
        try:
            domain.undefine()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                _log.warning("build VM %s undefine failed during reap", domain_name)


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )


__all__ = ["RemoteLibvirtBuildVmReaper", "run_id_from_build_vm_name"]
