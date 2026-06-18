"""Production probe adapter for the ephemeral build-host guest-agent check (ADR-0167).

The build-host boundary for :class:`~kdive.diagnostics.checks.EphemeralLibvirtBuildHostAgentCheck`:
the only place that imports :class:`EphemeralBuildVm`'s session seam (``diagnostics → providers``,
the legal direction). It enumerates the ``ephemeral_libvirt`` + ``enabled`` build hosts at probe
time, and for each provisions a throwaway builder via ``ephemeral_build_session`` with
``wait_network=False``, waits for its guest agent, execs one trivial command, and tears it down —
all under a reaper-visible heartbeat marker (``db.buildhost_agent_probes``) and a **module-level**
per-host :class:`SingleFlight` so concurrent doctor runs in one process spin one builder per host.

Because ``ephemeral_build_session`` is a synchronous (blocking libvirt + ``time.sleep``)
contextmanager, the blocking provision/exec/teardown runs in :func:`asyncio.to_thread` while an
async heartbeat task beats; the heartbeat-cancel and marker-release live in the probe coroutine's
``finally`` so a ``run_check`` timeout that cancels the coroutine still stops the heartbeat and
frees the marker (the orphaned thread's builder is then reclaimed by the reaper via the stale
heartbeat, with the marker TTL as the hard backstop).

The agent-vs-host discriminator is whether ``wait_for_agent`` returned (the session yielded a
transport): a failure inside the body (exec raised / non-zero rc) means the agent connected →
``AGENT_UNREACHABLE``; a ``CategorizedError`` escaping the session is ``HOST_UNREACHABLE`` unless
its category is ``PROVISIONING_FAILURE`` (the agent never connected) → ``AGENT_UNREACHABLE``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db import buildhost_agent_probes as probes
from kdive.db.build_hosts import BuildHost, BuildHostKind, list_all_hosts
from kdive.diagnostics.checks import (
    BuildHostAgentOutcome,
    BuildHostAgentProbe,
    BuildHostProbeResult,
)
from kdive.diagnostics.egress_probe import DEFAULT_PROBE_HEARTBEAT_INTERVAL, SingleFlight
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import GitSourceRef
from kdive.providers.ports.build_transport import CommandResult
from kdive.providers.remote_libvirt.lifecycle.build_vm import ephemeral_build_session
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)
_TRIVIAL_ARGV = ["true"]
_TRIVIAL_CWD = "/"
_TRIVIAL_TIMEOUT_S = 30

# Module-level (process-scope) single-flight: default_service_factory runs per ops.diagnostics call,
# so a per-call coalescer would coalesce nothing (egress_probe.SingleFlight docstring) — ADR-0167.
_SINGLE_FLIGHT: SingleFlight[BuildHostProbeResult] = SingleFlight()


class _BuildTransport(Protocol):
    """The slice of the build transport the probe execs through (structural; ADR-0167)."""

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult: ...


class _SessionFactory(Protocol):
    """The ``ephemeral_build_session`` seam the probe provisions through (ADR-0100, ADR-0167)."""

    def __call__(
        self,
        base_image_volume: str,
        secret_registry: SecretRegistry,
        *,
        run_id: UUID,
        source: GitSourceRef | None = ...,
        wait_network: bool = ...,
    ) -> AbstractContextManager[_BuildTransport]: ...


def buildhost_agent_probe(
    pool: AsyncConnectionPool,
    *,
    secret_registry: SecretRegistry | None = None,
    session_factory: _SessionFactory = ephemeral_build_session,
) -> BuildHostAgentProbe:
    """Build the async probe the check calls: enumerate ephemeral_libvirt hosts, probe each.

    Args:
        pool: The async pool used to enumerate build hosts and write reaper markers.
        secret_registry: The secret registry the build session resolves through (default: a fresh
            one, matching production assembly).
        session_factory: The ``ephemeral_build_session`` seam (injectable for tests).

    Returns:
        An async, no-arg probe returning one :class:`BuildHostProbeResult` per enabled
        ephemeral_libvirt host (the check aggregates them).
    """
    registry = secret_registry or SecretRegistry()

    async def probe() -> list[BuildHostProbeResult]:
        async with pool.connection() as conn:
            hosts = [
                host
                for host in await list_all_hosts(conn)
                if host.kind is BuildHostKind.EPHEMERAL_LIBVIRT and host.enabled
            ]
        results: list[BuildHostProbeResult] = []
        for host in hosts:
            results.append(
                await _SINGLE_FLIGHT.run(
                    str(host.id),
                    lambda host=host: _probe_one_host(host, pool, registry, session_factory),
                )
            )
        return results

    return probe


async def _probe_one_host(
    host: BuildHost,
    pool: AsyncConnectionPool,
    secret_registry: SecretRegistry,
    session_factory: _SessionFactory,
) -> BuildHostProbeResult:
    """Probe one host: register the marker, beat the heartbeat, run the session, classify."""
    if not host.base_image_volume:
        return BuildHostProbeResult(host.name, BuildHostAgentOutcome.HOST_UNREACHABLE)
    run_id = uuid4()
    try:
        probe_id = await probes.register(pool, build_host_id=host.id, run_id=run_id)
    except probes.ProbeInFlightError:
        return BuildHostProbeResult(host.name, BuildHostAgentOutcome.HOST_UNREACHABLE)
    except Exception:  # noqa: BLE001 - marker backend down → indeterminate, never a fail
        _log.error(
            "buildhost agent probe marker register failed for host=%s", host.name, exc_info=True
        )
        return BuildHostProbeResult(host.name, BuildHostAgentOutcome.HOST_UNREACHABLE)
    beat = asyncio.create_task(_beat_until_cancelled(pool, probe_id))
    try:
        outcome, transport_error = await asyncio.to_thread(
            _blocking_probe, host, run_id, secret_registry, session_factory
        )
        return BuildHostProbeResult(host.name, outcome, transport_error)
    except Exception:  # noqa: BLE001 - one host's unexpected failure is its own indeterminate
        # result, never a propagated error that collapses the whole aggregate (and masks other
        # hosts' real verdicts). _blocking_probe already maps the expected CategorizedError cases;
        # this is the per-host backstop mirroring run_check's per-check one.
        _log.error(
            "buildhost agent probe failed unexpectedly for host=%s", host.name, exc_info=True
        )
        return BuildHostProbeResult(host.name, BuildHostAgentOutcome.HOST_UNREACHABLE)
    finally:
        await _cancel(beat)
        await _release(pool, probe_id, host.name)


def _blocking_probe(
    host: BuildHost,
    run_id: UUID,
    secret_registry: SecretRegistry,
    session_factory: _SessionFactory,
) -> tuple[BuildHostAgentOutcome, bool]:
    """The synchronous provision → wait_for_agent → trivial exec → teardown, classified.

    Returns ``(outcome, transport_error)``. ``transport_error`` is meaningful only for
    ``HOST_UNREACHABLE`` (it marks a transport-vs-config cause for the aggregate category rule).
    """
    base_image_volume = host.base_image_volume
    if not base_image_volume:
        # No operator-staged base image to overlay: the builder cannot be provisioned. A config
        # prerequisite is missing, not a transport drop, so this is a config-flavored host error.
        return BuildHostAgentOutcome.HOST_UNREACHABLE, False
    try:
        with session_factory(
            base_image_volume, secret_registry, run_id=run_id, source=None, wait_network=False
        ) as transport:
            try:
                result = transport.run(
                    _TRIVIAL_ARGV, cwd=_TRIVIAL_CWD, timeout_s=_TRIVIAL_TIMEOUT_S
                )
            except CategorizedError:
                _log.warning(
                    "buildhost agent probe exec dropped on host=%s", host.name, exc_info=True
                )
                return BuildHostAgentOutcome.AGENT_UNREACHABLE, False
            if result.returncode != 0:
                return BuildHostAgentOutcome.AGENT_UNREACHABLE, False
            return BuildHostAgentOutcome.AGENT_READY, False
    except CategorizedError as exc:
        if exc.category is ErrorCategory.PROVISIONING_FAILURE:
            return BuildHostAgentOutcome.AGENT_UNREACHABLE, False
        transport_error = exc.category in (
            ErrorCategory.TRANSPORT_FAILURE,
            ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
        _log.warning(
            "buildhost agent probe host=%s unreachable: %s", host.name, exc.category, exc_info=True
        )
        return BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error


async def _beat_until_cancelled(pool: AsyncConnectionPool, probe_id: UUID) -> None:
    """Advance the marker heartbeat every interval so the reaper never reaps a live (slow) probe."""
    while True:
        with contextlib.suppress(Exception):  # a heartbeat blip must not fail the verdict
            await probes.heartbeat(pool, probe_id)
        await asyncio.sleep(DEFAULT_PROBE_HEARTBEAT_INTERVAL.total_seconds())


async def _release(pool: AsyncConnectionPool, probe_id: UUID, host_name: str) -> None:
    try:
        await probes.release(pool, probe_id)
    except Exception:  # noqa: BLE001 - release is best-effort; the marker TTL is the backstop
        _log.warning(
            "buildhost agent probe marker release failed for host=%s", host_name, exc_info=True
        )


async def _cancel(task: asyncio.Task[None]) -> None:
    """Cancel ``task`` and await its completion, swallowing the resulting cancellation."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
