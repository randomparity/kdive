"""Device hot-plug tool cells over HTTP on both libvirt providers (#3687, ADR-0823).

A functional cell provisions the lane image through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, then plugs and un-plugs one device
of the cell's kind on the running guest. Only the tool under test goes through the cell's
exposure; the supporting calls use the operator client. Each plug is observed three ways: the
ledger row (``systems.list_devices``), the provider's own libvirt for the exact domain (the device
alias, and for a disk its backing volume), and the guest itself over SSH (the MAC in
``/sys/class/net`` for a NIC, the virtio serial in ``/sys/block`` for a disk). Each un-plug
requires all three to report the device gone and the backing volume freed.

A rejection cell aims at the stack's torn-down lane target, as the power and snapshot carrier does.
"""

from __future__ import annotations

import asyncio
import secrets
import time
import xml.etree.ElementTree as ET  # noqa: S405 - parses the provider's own domain XML  # nosec B405
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.image_smoke import Endpoint, ssh
from tests.integration.live_stack.power_snapshots import grants
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.spine import POLL_INTERVAL_S, drain_job, ok
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Grants,
    Guest,
    HttpCaller,
    LaneTarget,
    Rejection,
    one,
)

TOOLS = ("systems.list_devices", "systems.plug_device", "systems.unplug_device")
KINDS = ("disk", "nic")
GATES = {
    "systems.list_devices": "viewer",
    "systems.plug_device": "contributor",
    "systems.unplug_device": "contributor",
}
_BELOW: dict[str, str | None] = {"viewer": None, "contributor": "viewer"}
# A foreign System is refused like an absent one: list reports not_found, the job tools a
# configuration_error. The twin comparison, not this set, proves they are indistinguishable.
_ISOLATION = frozenset({ErrorCategory.NOT_FOUND.value, ErrorCategory.CONFIGURATION_ERROR.value})
_ALIAS_PREFIX = "ua-kdive-"
_DISK_GB = 1
_GUEST_WAIT_S = 90.0
_NIC_PROBE = "cat /sys/class/net/*/address"
_DISK_PROBE = "for d in /sys/block/vd*; do cat $d/serial 2>/dev/null; echo; done"


def variant_of(cell: Cell) -> str:
    """The device kind of ``cell``, read from its local or provider-qualified scenario id."""
    kind = cell.scenario_id.split("/")[-2]
    assert kind in KINDS, f"{cell.scenario_id} names no device kind"
    return kind


def plug_args(system_id: str, kind: str) -> dict[str, object]:
    args: dict[str, object] = {"system_id": system_id, "kind": kind}
    if kind == "disk":
        args["size_gb"] = _DISK_GB
    return args


def domain_devices(domain_xml: str) -> dict[str, str | None]:
    """Each kdive-plugged device's device id mapped to its disk source path (``None`` for a NIC)."""
    root = ET.fromstring(domain_xml)  # noqa: S314 - the provider's own domain XML  # nosec B314
    found: dict[str, str | None] = {}
    for element in [*root.findall("./devices/interface"), *root.findall("./devices/disk")]:
        alias = element.find("alias")
        name = alias.get("name", "") if alias is not None else ""
        if not name.startswith(_ALIAS_PREFIX):
            continue
        source = element.find("source")
        found[name.removeprefix(_ALIAS_PREFIX)] = (
            source.get("file") or source.get("volume") if source is not None else None
        )
    return found


@dataclass
class Session:
    """A functional cell's System, its guest connection and who issues each call."""

    run: CellRun
    guest: Guest
    caller: HttpCaller
    token: str
    endpoint: Endpoint

    async def call(self, tool: str, **args: object) -> ToolResponse:
        """Send the tool under test through the cell's exposure, every other tool as operator."""
        if tool == self.run.cell.operation:
            env = one(await self.caller.call(tool, args, self.token, discover=True))
        else:
            env = one(await self.guest.op.call_tool(tool, **args))
        return ok(env, tool)

    async def job(self, tool: str, **args: object) -> ToolResponse:
        env = await self.call(tool, **args)
        assert env.status == "running", f"{tool} answered {env.status}, not a running job"
        await drain_job(self.guest.op, tool, str(env.data["job_id"]))
        return env

    async def devices(self) -> list[Mapping[str, object]]:
        env = await self.call("systems.list_devices", system_id=self.guest.system_id)
        rows = env.data["devices"]
        assert isinstance(rows, list)
        return [row for row in rows if isinstance(row, dict)]

    async def seen_by_guest(self, kind: str, identity: str) -> bool:
        probe = _NIC_PROBE if kind == "nic" else _DISK_PROBE
        result = await asyncio.to_thread(ssh, self.endpoint, self.guest.key, probe)
        assert result.returncode == 0, f"guest probe exited {result.returncode}"
        return identity.lower() in result.stdout.lower().split()

    async def guest_reports(self, kind: str, identity: str, *, present: bool) -> None:
        deadline = time.monotonic() + _GUEST_WAIT_S
        while await self.seen_by_guest(kind, identity) != present:
            assert time.monotonic() < deadline, (
                f"the guest {'does not show' if present else 'still shows'} the {kind}"
            )
            await asyncio.sleep(POLL_INTERVAL_S)

    def native(self) -> dict[str, str | None]:
        return domain_devices(self.guest.lane.xml(self.guest.system_id))


async def _lifecycle(s: Session, kind: str) -> dict[str, object]:
    """Plug one device, prove it in the ledger, libvirt and the guest, then un-plug and re-prove."""
    system_id = s.guest.system_id
    assert not s.native(), "the guest already holds a kdive device"
    plugged = await s.job("systems.plug_device", **plug_args(system_id, kind))
    device_id = plugged.object_id
    [row] = await s.devices()
    assert row["device_id"] == device_id and row["state"] == "attached", f"ledger row {row}"
    identity = row["identity"]
    assert isinstance(identity, dict)
    value = str(identity["mac" if kind == "nic" else "serial"])
    native = s.native()
    assert list(native) == [device_id], f"libvirt holds {sorted(native)}, not {device_id}"
    volume = native[device_id]
    if kind == "disk":
        assert volume, "the disk has no backing source in libvirt"
        s.guest.owned.append(volume)
    await s.guest_reports(kind, value, present=True)

    await s.job("systems.unplug_device", system_id=system_id, device_id=device_id)
    assert await s.devices() == [], "the ledger still lists the device"
    assert not s.native(), "libvirt still holds the device"
    if volume:
        assert s.guest.lane.absent(volume), "the backing volume was not freed"
    await s.guest_reports(kind, value, present=False)
    return {"kind": kind, "identity": value, "guest_saw": True, "volume_freed": volume is not None}


async def functional(
    run: CellRun, caller: HttpCaller, token_: str, guest: Guest
) -> dict[str, object]:
    """Run the hot-plug lifecycle of the cell's device kind."""
    session = Session(run, guest, caller, token_, guest.endpoint)
    observed = await _lifecycle(session, variant_of(run.cell))
    return {"tool": run.cell.operation, **observed}


def _stranger_grants() -> Grants:
    other = f"cov-{secrets.token_hex(4)}"
    return Grants(f"{other}-operator", (other,), {other: "operator"})


def target_args(tool: str, system_id: str, kind: str) -> dict[str, object]:
    if tool == "systems.plug_device":
        return plug_args(system_id, kind)
    if tool == "systems.unplug_device":
        return {"system_id": system_id, "device_id": str(uuid4())}
    return {"system_id": system_id}


def rejection(tool: str, kind: str, boundary: Boundary, target: LaneTarget) -> Rejection:
    """The call ``boundary`` must reject, aimed at the torn-down lane target."""
    args = target_args(tool, target.system_id, kind)
    gate = GATES[tool]
    if boundary == "validation":
        return Rejection({**args, "system_id": 7}, grants(target.project, gate))
    if boundary == "authentication":
        # A viewer's issued-token call is refused by role (or only reads), writing nothing.
        return Rejection(args, grants(target.project, "viewer"))
    if boundary == "authorization":
        return Rejection(args, grants(target.project, _BELOW[gate]))
    assert boundary == "project-isolation", f"no {boundary} cell exists for {tool}"
    twin = target_args(tool, str(uuid4()), kind)
    return Rejection(args, _stranger_grants(), _ISOLATION, absent_twin=twin)


__all__ = ["KINDS", "TOOLS", "functional", "rejection", "variant_of"]
