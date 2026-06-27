"""Remote-libvirt base-image-staging probe adapter (ADR-0150, #513).

The libvirt boundary for :class:`~kdive.diagnostics.checks.BaseImageStagingCheck`: it resolves the
single declared ``[[remote_libvirt]]`` instance's storage pool and staged base-image volume name,
opens the mutual-TLS ``qemu+tls://`` connection through the shared ``remote_connection`` lifecycle,
and looks the volume up on the pool via the shared :func:`lookup_volume_staged` helper. It mirrors
:func:`kdive.providers.remote_libvirt.diagnostics.reachability.remote_libvirt_reachability_probe`:
blocking libvirt work is offloaded with :func:`asyncio.to_thread`, config/volume resolution is
deferred to probe time so an inventory that drifts after assembly reports a legible
``configuration_error`` rather than collapsing the report, and a fresh per-probe
:class:`SecretRegistry` backs the TLS materialization.

Outcome mapping:

* config/volume unresolvable (``CONFIGURATION_ERROR``) → ``INDETERMINATE``
  (the opener is never called).
* ``qemu+tls`` connect failed (``TRANSPORT_FAILURE``) → :attr:`BaseImageStagingOutcome.UNREACHABLE`.
* pool present + volume present → :attr:`BaseImageStagingOutcome.STAGED`.
* pool present + volume absent → :attr:`BaseImageStagingOutcome.NOT_STAGED`.
* pool absent, or a storage ``libvirtError`` after a successful open (a transport drop mid-RPC is
  indistinguishable from a malformed pool name at this layer; host-down is the reachability check's
  job) → :attr:`BaseImageStagingOutcome.INDETERMINATE`, never a confident ``NOT_STAGED``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.diagnostics.checks import BaseImageStagingOutcome, BaseImageStagingProbe
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
from kdive.providers.remote_libvirt.connection.transport import (
    open_libvirt_protocol,
    remote_connection,
)
from kdive.providers.remote_libvirt.lifecycle.storage import (
    StorageConn,
    VolumeStaging,
    lookup_volume_staged,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)


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


def base_image_staging_probe(
    *,
    config_factory: Callable[[], RemoteLibvirtConfig],
    volume_factory: Callable[[], str],
    open_connection: Callable[[str], _StorageProbeConn] = _open_storage_connection,
    secret_backend_factory: Callable[[], SecretBackend] = _default_secret_backend,
    pki_base_dir: Path | None = None,
) -> BaseImageStagingProbe:
    """Build the async base-image-staging probe over the injected libvirt/config/secret seams.

    Args:
        config_factory: Resolves the remote-libvirt connection config (storage pool + TLS refs +
            URI); raises ``CategorizedError(CONFIGURATION_ERROR)`` for an unresolvable inventory.
        volume_factory: Resolves the staged base-image volume name; raises
            ``CategorizedError(CONFIGURATION_ERROR)`` when the base image is missing or not staged.
        open_connection: The libvirt opener (production narrows the binding; tests inject a fake).
        secret_backend_factory: Builds the secret backend for TLS materialization.
        pki_base_dir: Optional base dir for the materialized pkipath (tests pass a tmp dir).

    Returns:
        An async, no-arg probe returning a :class:`BaseImageStagingOutcome`.
    """

    async def probe() -> BaseImageStagingOutcome:
        try:
            config = config_factory()
            volume = volume_factory()
        except CategorizedError:
            # An unresolvable inventory / non-staged image is a check-cannot-run condition; the
            # opener is never reached.
            return BaseImageStagingOutcome.INDETERMINATE
        return await asyncio.to_thread(
            _probe_sync, config, volume, open_connection, secret_backend_factory, pki_base_dir
        )

    return probe


def _probe_sync(
    config: RemoteLibvirtConfig,
    volume: str,
    open_connection: Callable[[str], _StorageProbeConn],
    secret_backend_factory: Callable[[], SecretBackend],
    pki_base_dir: Path | None,
) -> BaseImageStagingOutcome:
    try:
        with remote_connection(
            config,
            secret_backend_factory(),
            open_connection=open_connection,
            pki_base_dir=pki_base_dir,
        ) as conn:
            staging = lookup_volume_staged(conn, config.storage_pool, volume)
    except CategorizedError as exc:
        if exc.category is ErrorCategory.TRANSPORT_FAILURE:
            return BaseImageStagingOutcome.UNREACHABLE
        return BaseImageStagingOutcome.INDETERMINATE
    except libvirt.libvirtError:
        # A storage RPC that failed after a successful open (lookup_volume_staged re-raises any
        # non-NO_STORAGE_* error). A transport drop mid-RPC and a malformed pool name are
        # indistinguishable here, and host-down is the reachability check's job, so report
        # INDETERMINATE rather than a confident NOT_STAGED.
        _log.warning("remote-libvirt base-image staging probe storage lookup failed", exc_info=True)
        return BaseImageStagingOutcome.INDETERMINATE
    if staging is VolumeStaging.STAGED:
        return BaseImageStagingOutcome.STAGED
    if staging is VolumeStaging.ABSENT:
        return BaseImageStagingOutcome.NOT_STAGED
    # POOL_ABSENT: a missing pool is a different misconfiguration than a missing volume, so no
    # stage-the-volume fix — an INDETERMINATE error.
    return BaseImageStagingOutcome.INDETERMINATE
