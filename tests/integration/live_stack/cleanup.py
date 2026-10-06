"""Owned-resource cleanup and released-capacity proof for live carriers (#2808, ADR-0715).

``release_and_verify`` is the ``cleanup`` assertion: after ``allocations.release`` the System is
``torn_down``, the Allocation is ``released``, the worker's libvirt no longer defines the
domain, every file-backed disk of the domain is gone, and the fleet's summed ``in_use``
capacity, which counted the Allocation while held, is back to its value from before the
Allocation was requested.
"""

from __future__ import annotations

import asyncio
import os
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import time
import xml.etree.ElementTree as ET  # noqa: S405 - parses the worker's own domain XML  # nosec B405
from collections.abc import Callable
from typing import Protocol

import libvirt

from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.spine import (
    DRAIN_DEADLINE_S,
    POLL_INTERVAL_S,
    await_system_state,
    ok,
    scalar,
    worker_libvirt_uri,
)


class _Conn(Protocol):
    def lookupByName(self, name: str) -> object: ...  # noqa: N802 - libvirt binding name
    def close(self) -> int: ...


def _worker_connect() -> _Conn:
    return libvirt.open(worker_libvirt_uri())


def domain_disks(xml: str) -> list[str]:
    """The ``<source file=…>`` path of every file-backed disk in a domain's XML."""
    root = ET.fromstring(xml)  # noqa: S314 - trusted worker-rendered XML  # nosec B314
    return [
        source.attrib["file"]
        for source in root.findall("./devices/disk/source")
        if "file" in source.attrib
    ]


def _sudo_test_exists(path: str) -> int:
    return subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["sudo", "-n", "test", "-e", path], check=False, timeout=10.0
    ).returncode


def disk_absent(path: str, *, privileged_test: Callable[[str], int] = _sudo_test_exists) -> bool:
    """True only when ``path`` provably does not exist.

    ``Path.exists`` reports ``False`` for a file under an unsearchable worker-owned parent, so
    only ``FileNotFoundError`` counts; a ``PermissionError`` asks ``sudo -n test -e`` instead.
    """
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    except PermissionError:
        status = privileged_test(path)
        if status in (0, 1):
            return status == 1
        raise AssertionError(f"cannot observe disk {path}: sudo test exited {status}") from None
    return False


async def capacity_in_use(client: LiveStackClient) -> int:
    """Summed ``in_use`` over every resource ``resources.availability`` reports."""
    env = await client.call_tool("resources.availability")
    assert isinstance(env, ToolResponse), f"unexpected availability reply: {env!r}"
    ok(env, "capacity")
    counts = [item.data.get("in_use") for item in env.items]
    assert all(isinstance(n, int) for n in counts), f"availability lacks in_use: {counts!r}"
    return sum(n for n in counts if isinstance(n, int))


async def _await_released(
    client: LiveStackClient, allocation_id: str, deadline_s: float, poll_s: float
) -> None:
    deadline = time.monotonic() + deadline_s
    while True:
        env = await scalar(client, "allocations.wait", allocation_id=allocation_id, timeout_s=0)
        if env.status == "released":
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"allocation stayed {env.status}, not released")
        await asyncio.sleep(poll_s)


async def release_and_verify(
    client: LiveStackClient,
    *,
    allocation_id: str,
    system_id: str,
    domain: str,
    disks: list[str],
    in_use_before: int,
    connect: Callable[[], _Conn] = _worker_connect,
    absent: Callable[[str], bool] = disk_absent,
    deadline_s: float = DRAIN_DEADLINE_S,
    poll_s: float = POLL_INTERVAL_S,
) -> dict[str, object]:
    """Release the Allocation and prove its owned domain, disks and capacity were reclaimed."""
    in_use_held = await capacity_in_use(client)
    assert in_use_held > in_use_before, (
        f"capacity in use is {in_use_held} while allocated, not above {in_use_before}; "
        "resources.availability does not see this allocation"
    )
    ok(await scalar(client, "allocations.release", allocation_id=allocation_id), "release")
    await await_system_state(client, "cleanup", system_id, "torn_down", deadline_s=deadline_s)
    await _await_released(client, allocation_id, deadline_s, poll_s)
    conn = connect()
    try:
        conn.lookupByName(domain)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
            raise
    else:
        raise AssertionError(f"domain {domain} is still defined after teardown")
    finally:
        conn.close()
    surviving = [path for path in disks if not absent(path)]
    assert not surviving, f"owned disk(s) survived teardown: {surviving}"
    in_use_after = await capacity_in_use(client)
    assert in_use_after == in_use_before, (
        f"capacity in use is {in_use_after}, not the {in_use_before} held before the allocation"
    )
    return {
        "system": "torn_down",
        "allocation": "released",
        "domain": "absent",
        "disks_absent": len(disks),
        "in_use": [in_use_before, in_use_held, in_use_after],
    }
