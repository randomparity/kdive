"""Remote-libvirt transport reset: re-arm a dead worker's gdbstub (#216, ADR-0086).

When the reconciler detaches a stale ``live`` DebugSession, this resetter frees the System's
single-client gdbstub so the next attach is not blocked by the dead worker's lingering
connection (ADR-0079). It self-selects: only a ``gdbstub`` transport whose handle host equals
the operator ``gdb_addr`` and that carries a domain name is re-armed; everything else is a
no-op. The re-arm is the explicit stop-then-rearm (``gdbserver none`` then
``gdbserver tcp::<port>``) over the ``qemu+tls`` monitor, closing the holding connection
deterministically (ADR-0083 host policy; ADR-0077 connection lifecycle). The monitor call runs
only under the ``live_vm`` gate; orchestration + self-selection are unit-tested with fakes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import TransportHandleData
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, all_remote_configs
from kdive.providers.remote_libvirt.connection.transport import (
    open_libvirt_protocol,
    remote_connection,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)
_GDBSTUB = "gdbstub"


class _Domain(Protocol):
    def qemuMonitorCommand(self, cmd: str, flags: int) -> str: ...  # noqa: N802 - libvirt name


class _ResetConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenResetConnection = Callable[[str], _ResetConn]


def open_libvirt_reset(uri: str) -> _ResetConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


def _real_rearm(domain: _Domain, port: int) -> None:  # pragma: no cover - live_vm
    """Stop-then-rearm the gdbstub over the QEMU monitor (HMP), dropping the stale client."""
    import libvirt_qemu

    hmp = libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_HMP
    domain.qemuMonitorCommand("gdbserver none", hmp)
    domain.qemuMonitorCommand(f"gdbserver tcp::{port}", hmp)


class RemoteLibvirtTransportResetter:
    """Re-arm a dead worker's remote gdbstub so the freed port no longer blocks re-attach."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        configs_factory: Callable[[], list[RemoteLibvirtConfig]] = all_remote_configs,
        open_connection: OpenResetConnection = open_libvirt_reset,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        rearm: Callable[[_Domain, int], None] = _real_rearm,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._configs_factory = configs_factory
        self._open_connection = open_connection
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._rearm = rearm
        self._pki_base_dir = pki_base_dir

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtTransportResetter:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None:
        """Re-arm the gdbstub if this is a matching remote gdbstub session; else no-op.

        Raises:
            CategorizedError: ``TRANSPORT_FAILURE`` if the monitor re-arm errors.
        """
        match = self._match_if_ours(transport, transport_handle, domain_name)
        if match is None or domain_name is None:
            return
        config, port = match
        await asyncio.to_thread(self._rearm_blocking, config, domain_name, port)
        _log.info("reconciler: re-armed remote gdbstub for domain %s (port %d)", domain_name, port)

    def _match_if_ours(
        self, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> tuple[RemoteLibvirtConfig, int] | None:
        """Resolve the fleet host whose ``gdb_addr`` matches the handle, with the freed port.

        The transport handle self-identifies its host (it encodes ``gdb_addr`` + port), so the
        matching host is selected from the declared fleet by ``gdb_addr`` — no DB lookup. Returns
        ``None`` (a no-op) for a non-gdbstub transport, a missing/undecodable handle, a handle host
        that matches no declared host (a local loopback gdbstub, or another fleet's host), or a
        missing domain name.
        """
        if transport != _GDBSTUB:
            return None
        if transport_handle is None:
            _log.info("reconciler: gdbstub session has no handle; skipping reset")
            return None
        try:
            data = TransportHandleData.decode(transport_handle)
        except CategorizedError:
            _log.info("reconciler: undecodable transport handle; skipping reset")
            return None
        if data.kind != _GDBSTUB:
            return None
        config = next((c for c in self._configs_factory() if c.gdb_addr == data.host), None)
        if config is None:
            return None  # a local loopback gdbstub, or no declared host owns this gdb_addr
        if domain_name is None:
            _log.info("reconciler: remote gdbstub session has no domain_name; cannot reset")
            return None
        return config, data.port

    def _rearm_blocking(self, config: RemoteLibvirtConfig, domain_name: str, port: int) -> None:
        with self._connection(config) as conn:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    f"looking up domain {domain_name!r} for gdbstub reset failed",
                    category=ErrorCategory.TRANSPORT_FAILURE,
                ) from exc
            try:
                self._rearm(domain, port)
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    "re-arming the remote gdbstub failed",
                    category=ErrorCategory.TRANSPORT_FAILURE,
                    details={"port": port},
                ) from exc

    def _connection(self, config: RemoteLibvirtConfig) -> AbstractContextManager[_ResetConn]:
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )


__all__ = ["RemoteLibvirtTransportResetter"]
