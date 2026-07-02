"""SSH-reachability probe primitives for the check_ssh_reachable worker job (ADR-0298, #972).

The probe opens a bounded, connection-retried TCP connection to a System's recorded loopback SSH
forward and reads the server banner. It sends nothing (sshd banners first; no handshake, no auth)
and never echoes the raw banner — the guest banner is external output, so it is classified into a
fixed vocabulary. The bounded retry tolerates the ~46 ms readiness (sshd-bind) race that
authorize_ssh_key also retries for (ADR-0289), so the probe is not more pessimistic than the op it
gates, while a far shorter deadline keeps it a quick check.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.payloads import CheckSshReachablePayload, load_payload
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.handles import SystemHandle
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.serialization import JsonValue

_PROBE_DEADLINE_S = 15.0
_CONNECT_TIMEOUT_S = 5.0
_BANNER_MAX_BYTES = 255
_BACKOFF_S = 0.5


@dataclass(frozen=True, slots=True)
class ReachResult:
    """The classified outcome of one probe: reachable, plus a fixed-vocabulary detail."""

    reachable: bool
    detail: str  # "reachable" | "unreachable" | "no SSH banner"


type ProbeFn = Callable[[str, int], Awaitable[ReachResult]]


async def _real_probe(
    host: str, port: int, *, deadline_s: float = _PROBE_DEADLINE_S
) -> ReachResult:
    """Probe ``host:port`` for an SSH banner, retrying connection-level failures until the deadline.

    Returns ``reachable`` iff a banner beginning ``SSH-`` arrives; ``no SSH banner`` when a
    connection is accepted but no ``SSH-`` line arrives before the deadline; ``unreachable`` when
    nothing accepts a connection before the deadline. Sends no bytes and never returns the raw
    banner. ``asyncio.TimeoutError`` is an ``OSError`` subclass, so the connect ``except OSError``
    also covers a connect timeout.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    while True:
        remaining = end - loop.time()
        if remaining <= 0:
            return ReachResult(False, "unreachable")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=min(_CONNECT_TIMEOUT_S, remaining),
            )
        except OSError:  # refused / reset / connect-timeout: sshd may still be binding — retry
            await asyncio.sleep(min(_BACKOFF_S, max(0.0, end - loop.time())))
            continue
        try:
            banner = await asyncio.wait_for(
                reader.read(_BANNER_MAX_BYTES), timeout=max(0.1, end - loop.time())
            )
        except OSError:
            banner = b""
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        if banner.startswith(b"SSH-"):
            return ReachResult(True, "reachable")
        return ReachResult(False, "no SSH banner")


def serialize_reach_verdict(result: ReachResult, host: str, port: int, checked_at: str) -> str:
    """Compact-JSON reachability verdict carried inline in ``result_ref`` (the ADR-0164 pattern)."""
    verdict: dict[str, JsonValue] = {
        "reachable": result.reachable,
        "checked_at": checked_at,
        "endpoint": {"host": host, "port": port},
        "detail": result.detail,
    }
    return json.dumps(verdict, separators=(",", ":"))


async def check_ssh_reachable_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    probe: ProbeFn = _real_probe,
) -> str | None:
    """Probe a ready System's guest sshd and return the compact-JSON reachability verdict.

    Re-checks the System is still ``ready`` before probing: a torn-down System's loopback port can
    be reused by another System's forward, so probing a stale endpoint could misattribute another
    guest's liveness. A probe that *ran* — reachable or not — is a success; only an inability to run
    raises. ``checked_at`` is read from the module-level ``datetime`` (tests monkeypatch it).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` ``reason="system_not_ready"`` when the System is
            no longer ready, or ``reason="ssh_not_provisioned"`` when it has no loopback forward.
    """
    payload = load_payload(job, CheckSshReachablePayload)
    system_id = UUID(payload.system_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None or system.state is not SystemState.READY:
        raise CategorizedError(
            "system is no longer ready; cannot probe SSH reachability",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "system_not_ready"},
        )
    binding = await resolver.binding_for_system(conn, system_id)
    endpoint = binding.runtime.connector.recorded_ssh_endpoint(
        SystemHandle(system.domain_name or domain_name_for(system_id))
    )
    if endpoint is None:
        raise CategorizedError(
            "This System's provider exposes no loopback SSH forward; direct SSH to a System is a "
            "local-libvirt capability",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "ssh_not_provisioned"},
        )
    host, port = endpoint
    result = await probe(host, port)
    return serialize_reach_verdict(result, host, port, datetime.now(UTC).isoformat())
