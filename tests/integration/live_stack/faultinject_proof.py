"""Fault-injection tool cells against a guest kernel built with ``CONFIG_FAULT_INJECTION`` (#3726).

The functional cells of ``faultinject.inspect``, ``faultinject.set`` and ``faultinject.reset`` boot
the pinned ``longterm`` fixture kernel (:func:`boot_fixture_kernel`), whose
``fixtures/kernel/debug.config`` enables ``FAILSLAB`` and ``FAIL_PAGE_ALLOC``, and then compare the
tool's answer with the guest's own debugfs files read over SSH without the helper.

``set`` arms ``failslab`` with ``probability=100``, ``times=1`` and ``verbose=1``. ``times`` counts
injected failures across the guest, so exactly one allocation fails and the kernel then disarms
itself; the default ``ignore-gfp-wait`` keeps that allocation to one that cannot sleep (a
``GFP_ATOMIC`` skb or similar), which the caller retries or drops. ``verbose=1`` prints the one
``FAULT_INJECTION: forcing a failure`` line without the stack dump. The failure is observed twice
in the guest: ``times`` reaches 0 and ``dmesg`` gains one line naming ``failslab``.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from kdive.mcp.responses import ToolResponse
from kdive.providers.ports.fault_injection import FAULT_KNOB_PARAMETERS
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.deep_lifecycle import FIXTURE_ROOT_ENV, deep_body, load_fixture
from tests.integration.live_stack.image_smoke import Endpoint, ssh
from tests.integration.live_stack.scenario import CellRun, ScenarioStop, ssh_endpoint
from tests.integration.live_stack.tool_cells import Guest, HttpCaller
from tests.mcp.json_data import data_mapping

BASELINE = "longterm"
DEBUGFS = "/sys/kernel/debug"
KNOBS = ("failslab", "fail_page_alloc")
# Fires on the next atomic slab allocation, then disarms itself after that one failure.
ARM = {"knob": "failslab", "probability": 100, "times": 1, "verbose": 1}
# Interval 1_000_000 lets one allocation in a million fail: armed, and in practice inert.
INERT = {"knob": "failslab", "probability": 100, "times": 1, "interval": 1_000_000}
_INJECTION_LINE = "FAULT_INJECTION: forcing a failure"
_FIRE_DEADLINE_S = 60.0
_POLL_S = 2.0


def fixture(arch: str) -> tuple[Path, dict[str, Any]]:
    """The verified ``longterm`` fixture; ``blocked`` before any stack mutation when absent."""
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the {BASELINE} fixture"
        )
    try:
        return load_fixture(Path(root), BASELINE, arch)
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{BASELINE} fixture: {exc}") from None


async def boot_fixture_kernel(
    run: CellRun, guest: Guest, tree: Path, manifest: dict[str, Any]
) -> Endpoint:
    """Upload, install and boot the fixture kernel on ``guest``; return its refreshed endpoint."""
    deep = CellRun(run.cell, run.writer, observed=run.observed)
    tmp = guest.scratch / "deep"
    tmp.mkdir()
    await deep_body(
        deep,
        guest.op,
        guest.system_id,
        guest.owned,
        project=guest.project,
        entry=guest.lane.entry,
        tree=tree,
        manifest=manifest,
        tmp=tmp,
        installed_kernel=guest.lane.installed_kernel,
    )
    run.artifacts.extend(deep.assertions.values())
    return await ssh_endpoint(guest.op, guest.system_id)


def guest_knob(endpoint: Endpoint, key: Path, knob: str) -> dict[str, object]:
    """The guest's own debugfs view of one knob, read over SSH without the helper."""
    probe = f"test -d {DEBUGFS}/{knob} || {{ echo absent; exit 0; }}; cd {DEBUGFS}/{knob} && "
    names = " ".join(FAULT_KNOB_PARAMETERS[knob])
    show = f"for f in {names}; do [ -f $f ] && echo $f=$(cat $f); done; true"
    result = ssh(endpoint, key, probe + show)
    assert result.returncode == 0, f"reading {knob} exited {result.returncode}"
    if result.stdout.strip() == "absent":
        return {"present": False}
    pairs = (line.split("=", 1) for line in result.stdout.split() if "=" in line)
    return {"present": True, **{name: int(value) for name, value in pairs}}


def guest_views(endpoint: Endpoint, key: Path) -> dict[str, dict[str, object]]:
    return {knob: guest_knob(endpoint, key, knob) for knob in KNOBS}


def injections(endpoint: Endpoint, key: Path) -> list[str]:
    """The kernel log's injection lines, each with the ``name <knob>`` line that follows it."""
    result = ssh(endpoint, key, f"dmesg | grep -A1 '{_INJECTION_LINE}'; true")
    assert result.returncode == 0, f"dmesg exited {result.returncode}"
    return [line for line in result.stdout.splitlines() if line.strip() and line != "--"]


def assert_matches_guest(reported: object, guest_view: dict[str, object], knob: str) -> None:
    """Every parameter the guest shows for ``knob`` equals what the tool reported."""
    shown = cast(dict[str, object], reported)
    compared = {name: shown[name] for name in guest_view if name in shown}
    assert compared == guest_view, f"{knob}: tool {shown} vs guest {guest_view}"


async def await_fired(endpoint: Endpoint, key: Path) -> dict[str, object]:
    """Poll the guest until ``failslab`` shows its single failure spent (``times`` 0)."""
    deadline = time.monotonic() + _FIRE_DEADLINE_S
    while True:
        view = await asyncio.to_thread(guest_knob, endpoint, key, "failslab")
        if view.get("times") == 0:
            return view
        assert time.monotonic() < deadline, f"failslab never fired within {_FIRE_DEADLINE_S}s"
        await asyncio.sleep(_POLL_S)


async def arm(
    caller: HttpCaller, token: str, guest: Guest, args: Mapping[str, object]
) -> ToolResponse:
    env = await caller.call("faultinject.set", {"system_id": guest.system_id, **args}, token)
    assert not isinstance(env, list), "expected one envelope"
    assert env.error_category is None, f"{env.error_category}: {env.detail}"
    return env


async def reset(
    caller: HttpCaller, token: str, guest: Guest, knob: str | None = None
) -> ToolResponse:
    args: dict[str, object] = {"system_id": guest.system_id}
    if knob:
        args["knob"] = knob
    env = await caller.call("faultinject.reset", args, token)
    assert not isinstance(env, list), "expected one envelope"
    assert env.error_category is None, f"{env.error_category}: {env.detail}"
    return env


def failslab_of(env: ToolResponse) -> dict[str, object]:
    return cast(dict[str, object], data_mapping(env, "knobs")["failslab"])
