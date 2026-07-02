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
