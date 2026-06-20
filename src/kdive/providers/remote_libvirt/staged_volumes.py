"""Server-vantage staged-volume probe for ``resources.describe`` (ADR-0156, #511).

Resolves the remote-libvirt connection config (URI, TLS refs, storage pool) internally, opens one
mutual-TLS ``qemu+tls://`` connection over the shared :func:`remote_connection` lifecycle, and runs
the shared :func:`lookup_volume_staged` helper (ADR-0150) once per requested volume. It is
best-effort: a transport failure / post-open libvirt error / timeout degrades every requested
volume to ``unreachable``, and an unresolvable config degrades to ``unknown`` — it never raises.

The pool is ``config.storage_pool`` (the pool provisioning uses, ADR-0080 §5), never the
``Resource`` row's advisory ``pool`` column.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    unbound_remote_config,
)
from kdive.providers.remote_libvirt.lifecycle.storage import (
    StorageConn,
    VolumeStaging,
    lookup_volume_staged,
)
from kdive.providers.remote_libvirt.transport import open_libvirt_protocol, remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)

_STAGED_PROBE_TIMEOUT_SECONDS = 5.0

_STATUS_BY_STAGING: dict[VolumeStaging, str] = {
    VolumeStaging.STAGED: "staged",
    VolumeStaging.ABSENT: "absent",
    VolumeStaging.POOL_ABSENT: "pool_absent",
}


class _StorageProbeConn(StorageConn, Protocol):
    """The slice this probe needs: storage lookup + the ``close`` ``remote_connection`` calls."""

    def close(self) -> None: ...


def _open_storage_connection(uri: str) -> _StorageProbeConn:
    """The production opener: narrow the libvirt binding to the storage-probe slice at the seam."""
    return open_libvirt_protocol(uri)


def _default_secret_backend() -> SecretBackend:
    # A fresh per-probe registry: the probe is short-lived and read-only, so the resolved TLS
    # material registers and is dropped with the registry when the probe returns.
    return secret_backend_from_env(registry=SecretRegistry())


async def probe_staged_volumes(
    volumes: list[str],
    *,
    config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
    open_connection: Callable[[str], _StorageProbeConn] = _open_storage_connection,
    secret_backend_factory: Callable[[], SecretBackend] = _default_secret_backend,
    timeout: float = _STAGED_PROBE_TIMEOUT_SECONDS,
    pki_base_dir: Path | None = None,
) -> dict[str, str]:
    """Probe each volume's staged status on the remote host's pool; never raises.

    Args:
        volumes: The base-image volume names to look up. An empty list opens no connection.
        config_factory: Resolves the remote-libvirt connection config (URI, TLS refs, storage
            pool); raises ``CategorizedError(CONFIGURATION_ERROR)`` for an unresolvable inventory.
        open_connection: The libvirt opener (production narrows the binding; tests inject a fake).
        secret_backend_factory: Builds the secret backend for the TLS materialization.
        timeout: Bounds the blocking libvirt work; injectable so a test can drive the
            timeout-degrade path without a real multi-second wait.
        pki_base_dir: Optional base dir for the materialized pkipath (tests pass a tmp dir).

    Returns:
        A ``{volume: status}`` map. For a reachable host the status is a real per-volume verdict —
        ``staged`` / ``absent`` / ``pool_absent``. ``unreachable`` is a host/RPC failure or timeout.
        ``unknown`` is reserved for "the probe could not run" — the remote config could not be
        resolved (e.g. an unbound runtime, ADR-0194) — never a reachable-but-not-staged verdict.
    """
    if not volumes:
        return {}
    try:
        config = config_factory()
    except CategorizedError:
        _log.warning("staged-volume probe could not resolve remote config", exc_info=True)
        return dict.fromkeys(volumes, "unknown")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _probe_sync, config, volumes, open_connection, secret_backend_factory, pki_base_dir
            ),
            timeout,
        )
    except TimeoutError:
        _log.warning("staged-volume probe timed out after %.2fs", timeout)
        return dict.fromkeys(volumes, "unreachable")


def _probe_sync(
    config: RemoteLibvirtConfig,
    volumes: list[str],
    open_connection: Callable[[str], _StorageProbeConn],
    secret_backend_factory: Callable[[], SecretBackend],
    pki_base_dir: Path | None,
) -> dict[str, str]:
    try:
        with remote_connection(
            config,
            secret_backend_factory(),
            open_connection=open_connection,
            pki_base_dir=pki_base_dir,
        ) as conn:
            return {
                volume: _STATUS_BY_STAGING[lookup_volume_staged(conn, config.storage_pool, volume)]
                for volume in volumes
            }
    except CategorizedError as exc:
        if exc.category is ErrorCategory.TRANSPORT_FAILURE:
            return dict.fromkeys(volumes, "unreachable")
        return dict.fromkeys(volumes, "unknown")
    except libvirt.libvirtError:
        _log.warning("staged-volume probe storage lookup failed", exc_info=True)
        return dict.fromkeys(volumes, "unreachable")
