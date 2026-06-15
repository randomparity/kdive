"""Shared remote-libvirt reaper connection construction."""

from __future__ import annotations

from collections.abc import Callable

from kdive.providers.remote_libvirt.config import remote_config_from_inventory
from kdive.providers.remote_libvirt.transport import (
    ClosableConn,
    RemoteLibvirtConnections,
    open_libvirt_protocol,
    remote_libvirt_connections,
)
from kdive.security.secrets.secret_registry import SecretRegistry


def open_libvirt_reaper[ConnT: ClosableConn](uri: str) -> ConnT:
    """Production opener for live remote-libvirt reaper paths."""
    return open_libvirt_protocol(uri)


def remote_libvirt_reaper_connections[ConnT: ClosableConn](
    *,
    secret_registry: SecretRegistry,
    open_connection: Callable[[str], ConnT],
) -> RemoteLibvirtConnections[ConnT]:
    """Build default remote-libvirt connections for reaper ports."""
    return remote_libvirt_connections(
        secret_registry=secret_registry,
        config_factory=remote_config_from_inventory,
        open_connection=open_connection,
    )
