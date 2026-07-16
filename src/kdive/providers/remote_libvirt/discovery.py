"""Remote-libvirt Discovery plane over qemu+tls (ADR-0076, ADR-0077).

Enumerates the remote host over an injected mutual-TLS connection (unit tests never
touch a real host; the real ``libvirt.open`` adapter is the production opener) and
advertises arch/cpu/memory, the gdbstub transport, the connect URI + TLS secret refs,
and the per-host concurrent-Allocation cap into ``resources.capabilities``.

The ``systems.toml`` ``[[remote_libvirt]]`` instance is authoritative for connections (ADR-0112);
the capabilities row is advertisory (insert-if-absent, refreshed only by re-registration).

PCIe enumeration and ``list_owned`` reaping are deferred to the provisioning issue,
which creates the domains they would inspect.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import libvirt

from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.discovery import ResourceRecord
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY, HOST_CPU_KEY
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.platform.cpu_baseline import baseline_level
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_for_resource
from kdive.providers.remote_libvirt.connection.transport import (
    OpenConnection,
    _LibvirtConn,  # the connection slice yielded by remote_connection (host_cpu read)
    open_libvirt,
    remote_connection,
)
from kdive.providers.shared.libvirt_xml import parse_capabilities_arch, parse_host_cpu
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)


class RemoteLibvirtDiscovery:
    """The realized discovery port for one remote qemu+tls host."""

    def __init__(
        self,
        *,
        config: RemoteLibvirtConfig,
        secret_backend: SecretBackend,
        open_connection: OpenConnection,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._secret_backend = secret_backend
        self._open_connection = open_connection
        self._pki_base_dir = pki_base_dir
        self.host_uri = config.uri

    @classmethod
    def from_env(
        cls, *, secret_registry: SecretRegistry, resource_name: str
    ) -> RemoteLibvirtDiscovery:
        """Build for the named ``[[remote_libvirt]]`` instance (ADR-0112, ADR-0187).

        A remote-libvirt resource row's ``name`` is its instance name, so discovery enumerates a
        single named host (#395); the fleet is registered by ``reconcile_resources`` from the
        config overlay, not by discovery (the registration is ``creates=False``).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` when no instance named ``resource_name`` is
                declared or the inventory is invalid (see :func:`remote_config_for_resource`).
        """
        return cls(
            config=remote_config_for_resource(resource_name),
            secret_backend=secret_backend_from_env(registry=secret_registry),
            open_connection=open_libvirt,
        )

    def list_resources(self) -> list[ResourceRecord]:
        """Return one ``ResourceRecord`` for the remote host (resource id = the URI).

        Raises:
            CategorizedError: ``TRANSPORT_FAILURE`` when the TLS connect fails, or
                ``CONFIGURATION_ERROR`` for unresolvable cert refs or an unsafe URI.
        """
        with remote_connection(
            self._config,
            self._secret_backend,
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        ) as conn:
            info = conn.getInfo()
            arch = parse_capabilities_arch(conn.getCapabilities())
            host_cpu = _discover_host_cpu(conn, arch, self._config.machine)
        refs = self._config.cert_refs
        capabilities: dict[str, Any] = {
            "arch": arch,
            "vcpus": int(info[2]),
            "memory_mb": int(info[1]),
            "transports": ["gdbstub"],
            "connect_uri": self._config.uri,
            "tls_client_cert_ref": refs.client_cert_ref,
            "tls_client_key_ref": refs.client_key_ref,
            "tls_ca_cert_ref": refs.ca_cert_ref,
            CONCURRENT_ALLOCATION_CAP_KEY: self._config.concurrent_allocation_cap,
            # Provisioning host topology (ADR-0080 §5); advisory, like the rest of
            # the row — the env config stays authoritative for ops.
            "storage_pool": self._config.storage_pool,
            "gdbstub_port_min": self._config.gdb_port_min,
            "gdbstub_port_max": self._config.gdb_port_max,
        }
        if self._config.gdb_addr is not None:
            capabilities["gdbstub_addr"] = self._config.gdb_addr
        if host_cpu is not None:
            capabilities[HOST_CPU_KEY] = host_cpu
        return [
            ResourceRecord(
                resource_id=self.host_uri,
                kind=ResourceKind.REMOTE_LIBVIRT,
                capabilities=capabilities,
                status=ResourceStatus.AVAILABLE,
            )
        ]


def _discover_host_cpu(conn: _LibvirtConn, arch: str, machine: str) -> dict[str, Any] | None:
    """Advertise the host-model guest CPU baseline (ADR-0368), or ``None`` on any fault.

    Parameterized to match the renderer (``render_domain_xml``): ``virttype='kvm'``, ``machine``
    from config, host ``arch``, default emulator. ``virttype='kvm'`` is exact, not a narrowing:
    the remote renderer always emits ``<domain type='kvm'>`` — remote-libvirt is KVM-only, TCG is a
    local-only concern (ADR-0341, ``install.py`` ``del accel``). A ``libvirtError`` (old libvirt
    without the API, transient RPC fault) or an unparseable/absent host-model block yields ``None``
    so a new advisory field never drops the host from discovery.
    """
    try:
        dom_caps = conn.getDomainCapabilities(None, arch, machine, "kvm")
    except libvirt.libvirtError:
        _log.warning("getDomainCapabilities failed; omitting host_cpu", exc_info=True)
        return None
    parsed = parse_host_cpu(dom_caps)
    if parsed is None:
        # Connected, valid XML, but no modelable host-model CPU (unsupported mode / empty <model>).
        # Log so an operator can tell this from a stale row or a never-discovered host (ADR-0368).
        _log.info("host advertises no modelable host-model CPU; omitting host_cpu")
        return None
    result: dict[str, Any] = {"model": parsed.model, "arch": parsed.arch or arch}
    if parsed.vendor is not None:
        result["vendor"] = parsed.vendor
    level = baseline_level(parsed.model, parsed.disabled_features)
    if level is not None:
        result["baseline_level"] = level
    return result
