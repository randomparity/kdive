"""Shared remote-libvirt reaper connection construction.

The reconciler reapers (host_dump orphan-volume sweep, ephemeral build-VM sweep) operate over the
whole declared remote-libvirt fleet, not a single allocated host (ADR-0187, #395). The bundle they
get binds :func:`all_remote_configs` as its fleet factory; the reapers fan out with
:func:`map_over_fleet` / :func:`find_over_fleet`, which open one connection per host and isolate a
per-host failure (an unreachable host is logged and skipped) so one down host never aborts the
fleet-wide sweep. The single-host ``config()`` path is unused by reapers and raises if called, so a
reaper can never silently sweep just one host.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from typing import NoReturn, Protocol

from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, all_remote_configs
from kdive.providers.remote_libvirt.transport import (
    ClosableConn,
    RemoteLibvirtConnections,
    open_libvirt_protocol,
    remote_libvirt_connections,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)


class FleetConnections[C](Protocol):
    """The fleet-iteration surface the reaper fan-out helpers need (a bundle satisfies it)."""

    def configs(self) -> list[RemoteLibvirtConfig]: ...
    def connection(self, config: RemoteLibvirtConfig) -> AbstractContextManager[C]: ...


def _enter_host[C](
    connections: FleetConnections[C], config: RemoteLibvirtConfig, *, operation: str
) -> tuple[ExitStack, C] | None:
    """Open one host's connection, isolating a *connection-open* failure (log + skip).

    Returns the open connection wrapped in an :class:`ExitStack` (close the stack to release the
    connection), or ``None`` when the host is unreachable. Only the connect itself is isolated: a
    failure raised by the caller's ``work`` on a reachable host is the caller's to propagate, so a
    genuine list/delete libvirt error is never silently swallowed (ADR-0187, #395).
    """
    stack = ExitStack()
    try:
        conn = stack.enter_context(connections.connection(config))
    except Exception:  # noqa: BLE001 - an unreachable host is isolated; healthy hosts still swept
        _log.warning(
            "reconciler: remote-libvirt %s skipped an unreachable host", operation, exc_info=True
        )
        stack.close()
        return None
    return stack, conn


def map_over_fleet[C, T](
    connections: FleetConnections[C],
    work: Callable[[C, RemoteLibvirtConfig], T],
    *,
    operation: str,
) -> list[T]:
    """Run ``work`` on every declared host, isolating an unreachable host (ADR-0187, #395).

    A host whose connection fails to open (unreachable / TLS error) is logged and skipped, so one
    down host never aborts the fleet-wide reaper sweep — the healthy hosts are still swept. A
    failure raised by ``work`` on a reachable host propagates (a genuine error must surface).
    Results are returned in declaration order.
    """
    results: list[T] = []
    for config in connections.configs():
        opened = _enter_host(connections, config, operation=operation)
        if opened is None:
            continue
        stack, conn = opened
        with stack:
            results.append(work(conn, config))
    return results


def find_over_fleet[C](
    connections: FleetConnections[C],
    work: Callable[[C, RemoteLibvirtConfig], bool],
    *,
    operation: str,
) -> bool:
    """Run ``work`` on each host until one returns ``True``; isolate an unreachable host.

    Delete-by-name reaps: the target lives on exactly one host, so iteration stops at the first
    host that reports it handled the target. A host that fails to connect is logged and skipped
    (the target may live on a later host), so one unreachable host never blocks reaping a target
    on a healthy host. A failure raised by ``work`` on a reachable host propagates (ADR-0187, #395).
    """
    for config in connections.configs():
        opened = _enter_host(connections, config, operation=operation)
        if opened is None:
            continue
        stack, conn = opened
        with stack:
            if work(conn, config):
                return True
    return False


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
