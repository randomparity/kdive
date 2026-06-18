"""TCP-connect gdbstub_acl probe for the remote-libvirt worker-vantage check (ADR-0164).

A *policy* check with no live listener (ADR-0091 §2): the worker attempts a TCP connect to the
lowest port of the configured gdbstub range. A connect or a fast ``ECONNREFUSED`` means the SYN
reached the host's TCP stack (the M2 ``DROP``/blackhole fault is excluded) -> admits; a connect
timeout means the firewall drops it -> blocked; any other error is indeterminate. Known limitation:
a fast ``ECONNREFUSED`` cannot distinguish "no listener" from an iptables ``-j REJECT`` rule, so a
REJECT-style block reads as admit (documented in ADR-0164).
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable

from kdive.diagnostics.checks import GdbstubAclProbe

_CONNECT_TIMEOUT_S = 3.0
_log = logging.getLogger(__name__)

AclConnector = Callable[[str, int], None]


def _connect(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_S):
        pass


def _lowest_port(port_range: str) -> int:
    return int(port_range.split("-", 1)[0])


def gdbstub_acl_probe(*, connector: AclConnector = _connect) -> GdbstubAclProbe:
    """Build the async gdbstub_acl probe over an injectable TCP connector."""

    async def probe(host: str, port_range: str) -> bool | None:
        return await asyncio.to_thread(_probe_sync, host, _lowest_port(port_range), connector)

    return probe


def _probe_sync(host: str, port: int, connector: AclConnector) -> bool | None:
    if not host:
        # An unset gdb_addr cannot be probed -> indeterminate (error), never a guess. Without this
        # guard `socket.create_connection(("", port))` would resolve "" to localhost and probe the
        # wrong hop (ADR-0164).
        _log.warning("gdbstub_acl probe has no host (gdb_addr unset); reporting indeterminate")
        return None
    try:
        connector(host, port)
    except ConnectionRefusedError:
        return True
    except TimeoutError:  # socket.timeout is an alias of TimeoutError (3.10+)
        return False
    except OSError:
        _log.warning("gdbstub_acl probe to %s:%s was indeterminate", host, port, exc_info=True)
        return None
    return True
