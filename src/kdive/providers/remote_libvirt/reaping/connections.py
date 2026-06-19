"""Shared remote-libvirt reaper connection construction.

The reconciler reapers (host_dump orphan-volume sweep, ephemeral build-VM sweep) operate over the
whole declared remote-libvirt fleet, not a single allocated host (ADR-0187, #395). The bundle they
get binds :func:`all_remote_configs` as its fleet factory; iterating ``connections.configs()`` and
opening one connection per host is the reaper's fan-out seam. The single-host ``config()`` path is
unused by reapers and raises if called, so a reaper can never silently sweep just one host.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, all_remote_configs
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


def _no_single_host() -> NoReturn:
    raise AssertionError(
        "remote-libvirt reaper bundle has no single host; iterate connections.configs() instead"
    )


def remote_libvirt_reaper_connections[ConnT: ClosableConn](
    *,
    secret_registry: SecretRegistry,
    open_connection: Callable[[str], ConnT],
    configs_factory: Callable[[], list[RemoteLibvirtConfig]] = all_remote_configs,
) -> RemoteLibvirtConnections[ConnT]:
    """Build fleet-wide remote-libvirt connections for reaper ports.

    ``configs_factory`` defaults to the whole declared fleet (:func:`all_remote_configs`); the
    reapers fan out over ``connections.configs()``, opening one connection per host.
    """
    return remote_libvirt_connections(
        secret_registry=secret_registry,
        config_factory=_no_single_host,
        open_connection=open_connection,
        configs_factory=configs_factory,
    )
