"""Remote-libvirt Connect plane: direct-TCP gdbstub transport (ADR-0079/0083).

`open_transport(system, "gdbstub")` composes the endpoint from operator config (the host is
``RemoteLibvirtConfig.gdb_addr``, the ACL'd listen address) and the per-System gdbstub port read
from the domain XML (ADR-0080), applies the ACL-remote host policy (no loopback gate — the host
is operator-trusted config, the operator ACL is the security boundary), probes RSP reachability,
and returns the encoded handle the gdb-MI tier consumes. The port read is a real read over the
mutual-TLS connection, mirroring the SSH endpoint read; only the socket ``open`` and the RSP
probe are injected ``live_vm`` seams, so orchestration and the full error contract stay
unit-tested with fakes. ``close_transport`` validates the handle and no-ops (connectionless RSP).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.handles import (
    SystemHandle,
    TransportHandle,
)
from kdive.providers.ports.lifecycle import (
    DebugTransportKind,
    TransportHandleData,
)
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, unbound_remote_config
from kdive.providers.remote_libvirt.connection.transport import remote_connection
from kdive.providers.remote_libvirt.lifecycle.xml import (
    recorded_gdb_port_strict,
    recorded_ssh_port_strict,
)
from kdive.providers.shared.debug_common.gdbmi.policy.hostpolicy import allow_acl_remote
from kdive.providers.shared.debug_common.rsp import rsp_reachable
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_GDBSTUB: DebugTransportKind = "gdbstub"
_DRGN_LIVE: DebugTransportKind = "drgn-live"

type _ResolvePort = Callable[[SystemHandle], int]
type _Probe = Callable[[str, int], bool]
type _OpenConnection = Callable[[str], Any]


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)


class RemoteLibvirtConnect:
    """The realized remote ``Connector``: a single-attach direct-TCP gdbstub transport."""

    def __init__(
        self,
        *,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
        resolve_port: _ResolvePort | None = None,
        probe: _Probe | None = None,
        open_connection: _OpenConnection | None = None,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._resolve_port = resolve_port if resolve_port is not None else self._read_gdb_port
        self._probe = probe if probe is not None else _real_probe
        self._open_connection = open_connection if open_connection is not None else _open_libvirt
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=SecretRegistry())
        )

    @classmethod
    def from_env(
        cls,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
    ) -> RemoteLibvirtConnect:
        """Build with the real domain-XML readers + socket probe.

        Both domain-XML reads are real production reads over the mutual-TLS connection: the
        gdbstub port (ADR-0080) and the SSH endpoint (ADR-0291, called on the live worker by
        ``ssh_info`` / ``authorize_ssh_key``). The gdbstub read was previously a stub that
        always raised ``MISSING_DEPENDENCY``, which made a remote gdbstub attach impossible in
        production however the host was configured (#1610).
        """
        return cls(
            config_factory=config_factory,
            secret_backend_factory=lambda: secret_backend_from_env(registry=secret_registry),
        )

    def open_transport(self, system: SystemHandle, kind: DebugTransportKind) -> TransportHandle:
        """Open the gdbstub or drgn-live transport for ``system``; raise for any other kind.

        ``drgn-live`` reaches in-guest drgn over the qemu-guest-agent keyed by domain, so its
        handle is the bare domain name core derived (``system``) — no gdb_addr, port, or probe
        (ADR-0083 §4). ``gdbstub`` composes the ACL'd direct-TCP endpoint and probes RSP.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown kind, an unset
                ``gdb_addr``, a malformed host, or a domain recording no gdbstub port;
                ``DEBUG_ATTACH_FAILURE`` if the domain is absent or the stub does not answer
                RSP; ``TRANSPORT_FAILURE`` on a socket fault; ``INFRASTRUCTURE_FAILURE`` on a
                libvirt fault reading the domain.
        """
        if kind == _DRGN_LIVE:
            return TransportHandle(str(system))
        if kind != _GDBSTUB:
            raise _config_error(f"unsupported transport kind: {kind!r}")
        config = self._config_factory()
        if not config.gdb_addr:
            raise _config_error("remote gdbstub host (instance gdb_addr in systems.toml) is unset")
        host = config.gdb_addr
        allow_acl_remote(host)
        port = self._resolve_port(system)
        try:
            reachable = self._probe(host, port)
        except OSError as exc:
            raise CategorizedError(
                "gdbstub transport socket fault",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"port": port},
            ) from exc
        if not reachable:
            raise CategorizedError(
                "remote gdbstub did not answer RSP framing",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"host": host, "port": port},
            )
        return TransportHandle(TransportHandleData(kind=_GDBSTUB, host=host, port=port).encode())

    def close_transport(self, handle: TransportHandle) -> None:
        """No-op close. A schemed gdbstub handle is validated; the bare-domain drgn-live
        handle (ADR-0083 §4) is connectionless and needs no validation."""
        if "://" in str(handle):
            TransportHandleData.decode(handle)

    def recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None:
        """Return the recorded ``(ssh_addr, ssh_port)``, or ``None`` when SSH parity is inactive.

        Reads the per-System hostfwd port from the live domain XML over the mutual-TLS connection
        (ADR-0291) — a real worker read (``ssh_info`` / ``authorize_ssh_key`` call this on the live
        path), **not** a ``live_vm`` stub. Only the socket ``open`` is the live seam; parsing and
        orchestration are unit-tested with an injected connection.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for missing operator config;
                ``INFRASTRUCTURE_FAILURE`` for a libvirt fault reading the domain (other than the
                domain being absent, which is ``None``); ``TRANSPORT_FAILURE`` when the TLS connect
                fails.
        """
        config = self._config_factory()
        if config.ssh_addr is None or config.ssh_port_min is None:
            return None
        port = self._read_ssh_port(config, str(system))
        return None if port is None else (config.ssh_addr, port)

    def _read_ssh_port(self, config: RemoteLibvirtConfig, domain_name: str) -> int | None:
        with remote_connection(
            config, self._secret_backend_factory(), open_connection=self._open_connection
        ) as conn:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    return None
                raise CategorizedError(
                    "libvirt error looking up the domain for its SSH endpoint",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"domain": domain_name},
                ) from exc
            return recorded_ssh_port_strict(
                domain.XMLDesc(), operation="reading ssh endpoint", domain=domain_name
            )

    def _read_gdb_port(self, system: SystemHandle) -> int:
        """Read the per-System gdbstub port recorded in the live domain XML (ADR-0080).

        The production counterpart of :meth:`_read_ssh_port`, over the same mutual-TLS
        connection. Provisioning records the port as the domain's ``-gdb tcp:<addr>:<port>``
        argument; this reads it back so the gdb-MI tier can dial it.

        Raises:
            CategorizedError: ``DEBUG_ATTACH_FAILURE`` when the domain is absent (nothing to
                attach to); ``INFRASTRUCTURE_FAILURE`` on any other libvirt fault reading it;
                ``CONFIGURATION_ERROR`` when the domain records no gdbstub port, which means it
                was provisioned without one rather than that the read failed.
        """
        config = self._config_factory()
        domain_name = str(system)
        with remote_connection(
            config, self._secret_backend_factory(), open_connection=self._open_connection
        ) as conn:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    raise CategorizedError(
                        "no remote domain to attach to",
                        category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                        details={"domain": domain_name},
                    ) from exc
                raise CategorizedError(
                    "libvirt error looking up the domain for its gdbstub port",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"domain": domain_name},
                ) from exc
            port = recorded_gdb_port_strict(
                domain.XMLDesc(), operation="reading gdbstub endpoint", domain=domain_name
            )
        if port is None:
            raise _config_error(
                f"domain {domain_name!r} records no gdbstub port; it was provisioned without one"
            )
        return port


def _open_libvirt(uri: str) -> Any:  # pragma: no cover - live_vm
    return libvirt.open(uri)


def _real_probe(host: str, port: int) -> bool:  # pragma: no cover - live_vm
    return rsp_reachable(host, port)


__all__ = ["RemoteLibvirtConnect"]
