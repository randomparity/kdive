"""The production remote-libvirt reachability probe adapter (ADR-0125, #453).

The libvirt boundary for :class:`RemoteLibvirtReachabilityCheck`: it resolves the single declared
``[[remote_libvirt]]`` instance, opens the mutual-TLS ``qemu+tls://`` connection, and calls
``getInfo()`` — the same connect path the discovery/provisioning planes use
(``remote_libvirt.transport``). The blocking libvirt work is offloaded with
:func:`asyncio.to_thread` (mirroring :class:`SshBuildHostProber`) so the probe never stalls the
diagnostics event loop, and the per-check timeout in :func:`run_check` bounds a black-holing host.

The outcome is driven by the ``CategorizedError.category`` the connection raises:

* ``TRANSPORT_FAILURE`` (the TLS connect failed) → :attr:`ReachabilityOutcome.UNREACHABLE`
  (a contract ``fail`` — the host is down / port closed).
* ``CONFIGURATION_ERROR`` (bad URI, unresolvable cert refs, zero/>1 declared instances) →
  :attr:`ReachabilityOutcome.MISCONFIGURED` (an ``error`` — the probe never ran).

Config resolution is **deferred to probe time** (not factory time) so a deployment that drifts to
a malformed/multi-instance inventory after assembly reports a legible ``configuration_error``
rather than collapsing the whole diagnostics report.

The probe builds a **fresh per-probe** :class:`SecretRegistry` for the TLS materialization — it is
short-lived and read-only, mirroring :class:`SshBuildHostProber`'s per-probe scope, and keeps the
default diagnostics factory free of a registry dependency.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from kdive.diagnostics.checks import ReachabilityOutcome, ReachabilityProbe
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    remote_config_from_inventory,
)
from kdive.providers.remote_libvirt.transport import open_libvirt, remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)


def _default_secret_backend() -> SecretBackend:
    # A fresh per-probe registry: the probe is short-lived and read-only, so the resolved TLS
    # material registers and is dropped with the registry when the probe returns.
    return secret_backend_from_env(registry=SecretRegistry())


def remote_libvirt_reachability_probe(
    *,
    config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_inventory,
    open_connection: Callable[[str], object] = open_libvirt,
    secret_backend_factory: Callable[[], SecretBackend] = _default_secret_backend,
    pki_base_dir: Path | None = None,
) -> ReachabilityProbe:
    """Build the async reachability probe over the injected libvirt/config/secret seams.

    Args:
        config_factory: Resolves the single declared remote-libvirt config; raises
            ``CategorizedError(CONFIGURATION_ERROR)`` for zero/>1 instances or a malformed
            inventory (deferred to probe time).
        open_connection: The libvirt opener (production: :func:`open_libvirt`; tests inject a
            fake that returns a stub or raises ``libvirt.libvirtError``).
        secret_backend_factory: Builds the secret backend for TLS materialization (production:
            a fresh per-probe registry; tests inject a fake).
        pki_base_dir: Optional base dir for the materialized pkipath (tests pass a tmp dir).

    Returns:
        An async, no-arg probe returning a :class:`ReachabilityOutcome`.
    """

    async def probe() -> ReachabilityOutcome:
        try:
            config = config_factory()
        except CategorizedError as exc:
            return _outcome_for(exc, stage="config")
        return await asyncio.to_thread(
            _probe_sync, config, open_connection, secret_backend_factory, pki_base_dir
        )

    return probe


def _probe_sync(
    config: RemoteLibvirtConfig,
    open_connection: Callable[[str], object],
    secret_backend_factory: Callable[[], SecretBackend],
    pki_base_dir: Path | None,
) -> ReachabilityOutcome:
    try:
        with remote_connection(
            config,
            secret_backend_factory(),
            open_connection=open_connection,
            pki_base_dir=pki_base_dir,
        ) as conn:
            conn.getInfo()
    except CategorizedError as exc:
        return _outcome_for(exc, stage="connect")
    return ReachabilityOutcome.REACHABLE


def _outcome_for(exc: CategorizedError, *, stage: str) -> ReachabilityOutcome:
    if exc.category is ErrorCategory.TRANSPORT_FAILURE:
        return ReachabilityOutcome.UNREACHABLE
    if exc.category is ErrorCategory.CONFIGURATION_ERROR:
        return ReachabilityOutcome.MISCONFIGURED
    # Any other categorized failure is treated as indeterminate (MISCONFIGURED) rather than a
    # confident "host down": a probe that cannot reach a verdict must not emit a transport fix.
    _log.warning("remote-libvirt reachability probe got unexpected %s at %s", exc.category, stage)
    return ReachabilityOutcome.MISCONFIGURED
