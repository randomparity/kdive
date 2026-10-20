"""Power and snapshot tool cells over HTTP on both providers (#2815, #3116, ADR-0722).

A functional cell provisions the lane image through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, installs and boots the verified
``longterm`` kernel fixture (:func:`~tests.integration.live_stack.deep_lifecycle.deep_body`), then
runs its tool's sequence. Only the tool under test goes through the cell's exposure; the
supporting calls use the operator client. The guest oracles read a synthetic value from the disk
(after dropping the page cache, so a restore that brings back RAM alone cannot pass), the same
value from tmpfs (RAM only) and a long-lived process's identity, so a no-op restore, a reboot in
place of a memory restore and a RAM-only revert each fail. Native observations read the
provider's libvirt for the exact owned domain through the lane's ``connect``: the worker's for a
local cell, the provider host's through the operator's observer for a remote one, whose kernel is
installed in the guest's own ``/boot`` (the lane's ``installed_kernel``).

A rejection cell aims at the stack's torn-down lane target. Every tool resolves the System and
checks the caller's project and role before its state, so the target is valid, and the protected
state adds the target's jobs (``jobs`` has no project column) and its absent domain to the
project snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import libvirt
import psycopg

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.providers.shared.runtime_paths import domain_name_for
from tests.integration.live_stack.cleanup import domain_disks
from tests.integration.live_stack.deep_lifecycle import deep_body
from tests.integration.live_stack.image_smoke import Endpoint, parse_probe, ssh
from tests.integration.live_stack.scenario import (
    CellRun,
    probe_new_boot,
    ssh_endpoint,
)
from tests.integration.live_stack.spine import drain_job, ok, scalar
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Connect,
    Grants,
    Guest,
    HttpCaller,
    LaneTarget,
    Rejection,
    one,
    project_state,
    worker_connect,
)

TOOLS = (
    "control.power",
    "systems.delete_snapshot",
    "systems.list_snapshots",
    "systems.restore",
    "systems.snapshot",
)
# Each tool's project gate; the snapshot tools resolve a foreign System as not_found, power as
# configuration_error, each identical to the answer for an absent id.
GATES = {tool: "contributor" for tool in TOOLS} | {"systems.list_snapshots": "viewer"}
_BELOW: dict[str, str | None] = {"viewer": None, "contributor": "viewer"}
_ISOLATION = {
    tool: ErrorCategory.CONFIGURATION_ERROR if tool == "control.power" else ErrorCategory.NOT_FOUND
    for tool in TOOLS
}
_DISK = "/root/kdive-cov-state"
_RAM = "/dev/shm/kdive-cov-state"  # noqa: S108 - the guest's tmpfs, which only RAM holds
_PID = "/dev/shm/kdive-cov-pid"  # noqa: S108 - the guest's tmpfs, which only RAM holds
# Read from the disk, not the page cache a memory restore brings back with RAM.
STATE_PROBE = (
    "sync; echo 3 > /proc/sys/vm/drop_caches; "
    'printf "boot_id=%s\\n" "$(cat /proc/sys/kernel/random/boot_id)"; '
    f'printf "disk=%s\\nram=%s\\n" "$(cat {_DISK} 2>/dev/null)" "$(cat {_RAM} 2>/dev/null)"; '
    f'pid=$(cat {_PID} 2>/dev/null); start=""; '
    '[ -n "$pid" ] && start=$(cut -d" " -f22 /proc/"$pid"/stat 2>/dev/null); '
    'printf "process=%s:%s\\n" "$pid" "$start"'
)
_STATES = {
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
}


@dataclass(frozen=True)
class GuestState:
    """What a checkpoint must bring back: the boot, the disk value, the RAM value, a process."""

    boot_id: str
    disk: str
    ram: str
    process: str

    @classmethod
    def parse(cls, stdout: str) -> GuestState:
        probe = parse_probe(stdout)
        return cls(probe["boot_id"], probe["disk"], probe["ram"], probe["process"])


def differs(expected: GuestState, actual: GuestState) -> list[str]:
    """The fields of ``actual`` that are not ``expected``'s, by name only (no guest values)."""
    return [
        f
        for f in ("boot_id", "disk", "ram", "process")
        if getattr(expected, f) != getattr(actual, f)
    ]


def require_restored(checkpoint: GuestState, mutated: GuestState, restored: GuestState) -> None:
    """A memory restore: the checkpoint's boot, disk, RAM and process, none of the mutation's.

    A no-op restore keeps the mutated values; a reboot loses the RAM value and the process; a
    revert of RAM alone keeps the mutated disk value, which the cache-dropped read sees.
    """
    assert checkpoint.process.split(":")[1], "the checkpoint holds no live sentinel process"
    assert mutated.disk != checkpoint.disk and mutated.ram != checkpoint.ram, (
        "the mutation did not change both values"
    )
    changed = differs(checkpoint, restored)
    assert not changed, f"the restore did not bring back the checkpoint's {changed}"


def require_disk_rollback(
    checkpoint: GuestState, mutated: GuestState, restored: GuestState
) -> None:
    """A disk-only restore: the checkpoint's disk value in a new boot; no memory claim."""
    assert mutated.disk != checkpoint.disk, "the mutation did not change the disk value"
    assert restored.disk == checkpoint.disk, "the disk-only restore did not roll the disk back"
    assert restored.boot_id != mutated.boot_id, "the disk-only restore did not boot the guest"


def _shell(value: str) -> str:
    assert value.isalnum(), "guest values are generated hex tokens"
    return value


def seed_command(disk: str, ram: str) -> str:
    """Write both values and start the sentinel process the checkpoint must keep alive."""
    return (
        f"printf %s {_shell(disk)} > {_DISK} && sync && printf %s {_shell(ram)} > {_RAM} && "
        f"(setsid sleep infinity </dev/null >/dev/null 2>&1 & echo $! > {_PID})"
    )


def mutate_command(disk: str, ram: str) -> str:
    return f"printf %s {_shell(disk)} > {_DISK} && sync && printf %s {_shell(ram)} > {_RAM}"


def token() -> str:
    return secrets.token_hex(8)


def snapshot_name() -> str:
    return f"cov-{secrets.token_hex(4)}"


def _domain(system_id: str, connect: Connect) -> tuple[libvirt.virConnect, libvirt.virDomain]:
    conn = connect()
    try:
        return conn, conn.lookupByName(domain_name_for(UUID(system_id)))
    except libvirt.libvirtError:
        conn.close()
        raise


def domain_state(system_id: str, connect: Connect) -> str:
    """The provider's libvirt state of the System's domain: running, paused or shutoff."""
    conn, domain = _domain(system_id, connect)
    try:
        state = domain.state()[0]
    finally:
        conn.close()
    return _STATES.get(state, f"state-{state}")


def checkpoints(system_id: str, connect: Connect) -> list[str]:
    """The domain's native snapshot names, sorted by name."""
    conn, domain = _domain(system_id, connect)
    try:
        return sorted(snap.getName() for snap in domain.listAllSnapshots())
    finally:
        conn.close()


def domain_defined(system_id: str, connect: Connect = worker_connect) -> bool:
    """Whether the provider's libvirt defines the domain: absent only on VIR_ERR_NO_DOMAIN."""
    conn = connect()
    try:
        conn.lookupByName(domain_name_for(UUID(system_id)))
    except libvirt.libvirtError as exc:
        if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
            raise
        return False
    finally:
        conn.close()
    return True


@dataclass
class Session:
    """A functional cell's System, its guest connection and who issues each call.

    ``call`` sends the tool under test through the cell's exposure and every other tool through
    the operator client; each drains its job before returning.
    """

    run: CellRun
    guest: Guest
    caller: HttpCaller
    token: str
    endpoint: Endpoint

    async def call(self, tool: str, **args: object) -> ToolResponse:
        if tool == self.run.cell.operation:
            env = one(await self.caller.call(tool, args, self.token, discover=True))
        else:
            # Not ``scalar``: its positional ``name`` would collide with a snapshot's name.
            env = one(await self.guest.op.call_tool(tool, **args))
        return ok(env, tool)

    async def job(self, tool: str, **args: object) -> ToolResponse:
        env = await self.call(tool, **args)
        assert env.status == "queued", f"{tool} answered {env.status}, not a queued job"
        try:
            return await drain_job(self.guest.op, tool, env.object_id)
        finally:
            # A disk-only checkpoint, or its restore or delete, can move the domain to another
            # overlay. The System owns every disk it used, so the cleanup proves each absent.
            for disk in domain_disks(self.guest.lane.xml(self.guest.system_id)):
                if disk not in self.guest.owned:
                    self.guest.owned.append(disk)

    async def status(self) -> str:
        env = ok(await scalar(self.guest.op, "systems.get", system_id=self.guest.system_id), "get")
        return env.status

    async def listing(self) -> list[Mapping[str, object]]:
        env = ok(
            await scalar(self.guest.op, "systems.list_snapshots", system_id=self.guest.system_id),
            "list",
        )
        return [item.data for item in env.items]

    async def shell(self, command: str) -> None:
        result = await asyncio.to_thread(ssh, self.endpoint, self.guest.key, command)
        assert result.returncode == 0, f"guest command exited {result.returncode}"

    async def state(self) -> GuestState:
        result = await asyncio.to_thread(ssh, self.endpoint, self.guest.key, STATE_PROBE)
        assert result.returncode == 0, f"state probe exited {result.returncode}"
        return GuestState.parse(result.stdout)

    async def new_boot(self, boot_id: str) -> GuestState:
        """Reconnect after a reboot and read the state of the boot that replaced ``boot_id``."""
        self.endpoint = await ssh_endpoint(self.guest.op, self.guest.system_id)
        probe = await asyncio.to_thread(
            probe_new_boot, self.endpoint, self.guest.key, boot_id, STATE_PROBE
        )
        return GuestState(probe["boot_id"], probe["disk"], probe["ram"], probe["process"])

    async def snapshot(self, name: str, *, include_memory: bool = True) -> None:
        await self.job(
            "systems.snapshot",
            system_id=self.guest.system_id,
            name=name,
            include_memory=include_memory,
        )
        rows = {str(r.get("name")): r for r in await self.listing()}
        assert rows.get(name, {}).get("state") == "available", f"{name} is not available"
        assert name in checkpoints(self.guest.system_id, self.guest.lane.connect), (
            f"libvirt holds no {name} checkpoint"
        )

    async def restore(self, name: str, *, paused: bool = False) -> None:
        await self.job(
            "systems.restore", system_id=self.guest.system_id, name=name, start_paused=paused
        )

    async def delete(self, name: str) -> None:
        await self.job("systems.delete_snapshot", system_id=self.guest.system_id, name=name)
        assert name not in checkpoints(self.guest.system_id, self.guest.lane.connect), (
            f"libvirt still holds {name}"
        )
        assert name not in {str(r.get("name")) for r in await self.listing()}, f"{name} listed"

    async def power(self, action: str) -> None:
        await self.job("control.power", system_id=self.guest.system_id, action=action)

    async def seeded(self) -> GuestState:
        await self.shell(seed_command(token(), token()))
        return await self.state()

    async def mutated(self) -> GuestState:
        await self.shell(mutate_command(token(), token()))
        return await self.state()


async def _memory_restore(s: Session, *, paused: bool) -> dict[str, object]:
    """Checkpoint, mutate, restore (paused then resumed when asked) and compare."""
    name = snapshot_name()
    checkpoint = await s.seeded()
    await s.snapshot(name)
    mutated = await s.mutated()
    await s.restore(name, paused=paused)
    observed: dict[str, object] = {}
    if paused:
        native, durable = domain_state(s.guest.system_id, s.guest.lane.connect), await s.status()
        assert (native, durable) == ("paused", "paused"), f"paused restore: {native}, {durable}"
        await s.power("resume")
        observed["paused"] = {"domain": native, "system": durable}
    native, durable = domain_state(s.guest.system_id, s.guest.lane.connect), await s.status()
    assert (native, durable) == ("running", "ready"), f"after restore: {native}, {durable}"
    require_restored(checkpoint, mutated, await s.state())
    await s.delete(name)
    return observed | {"restored": ["boot_id", "disk", "ram", "process"], "domain": native}


async def _disk_restore(s: Session) -> dict[str, object]:
    """A disk-only checkpoint, restored: the disk value rolls back in a new boot."""
    name = snapshot_name()
    checkpoint = await s.seeded()
    await s.snapshot(name, include_memory=False)
    mutated = await s.mutated()
    await s.restore(name)
    restored = await s.new_boot(mutated.boot_id)
    require_disk_rollback(checkpoint, mutated, restored)
    await s.delete(name)
    return {"disk_rolled_back": True, "new_boot": True}


async def snapshot_body(s: Session) -> dict[str, object]:
    """The checkpoint keeps disk, RAM and the running process: a restore brings all back."""
    return {"checkpoint": await _memory_restore(s, paused=False)}


async def restore_body(s: Session) -> dict[str, object]:
    """Running and paused memory restores, then a disk-only restore last (ADR-0378)."""
    return {
        "running": await _memory_restore(s, paused=False),
        "paused": await _memory_restore(s, paused=True),
        "disk_only": await _disk_restore(s),
    }


async def list_body(s: Session) -> dict[str, object]:
    """Two known checkpoints listed by name, order, state and mode, as libvirt holds them."""
    names = [snapshot_name(), snapshot_name()]
    await s.seeded()
    for name in names:
        await s.snapshot(name)
    env = await s.call("systems.list_snapshots", system_id=s.guest.system_id)
    rows = [
        (str(i.data.get("name")), i.data.get("state"), i.data.get("include_memory"))
        for i in env.items
    ]
    # The tool lists newest first. libvirt's creation times have one-second resolution, so its
    # inventory is compared as a set.
    assert rows == [(n, "available", True) for n in reversed(names)], f"listed {rows}"
    native = checkpoints(s.guest.system_id, s.guest.lane.connect)
    assert native == sorted(names), f"libvirt holds {native}"
    for name in names:
        await s.delete(name)
    return {"listed": len(rows), "order": "newest-first", "matches_libvirt": True}


async def delete_body(s: Session) -> dict[str, object]:
    """The deleted checkpoint leaves both ledgers, and its name is free for a new checkpoint."""
    name = snapshot_name()
    await s.seeded()
    await s.snapshot(name)
    await s.delete(name)
    # Admission refuses a name whose row is still available (snapshot_name_in_use), so a new
    # checkpoint under the same name shows the durable row is gone.
    await s.snapshot(name)
    await s.delete(name)
    return {"deleted": True, "name_reusable": True}


async def power_body(s: Session) -> dict[str, object]:
    """off, on, reset and cycle from READY, then resume from a real paused restore."""
    before = await s.state()
    await s.power("off")
    off = domain_state(s.guest.system_id, s.guest.lane.connect), await s.status()
    assert off == ("shutoff", "ready"), f"after off: {off}"
    observed: dict[str, object] = {"off": {"domain": off[0], "system": off[1]}}
    boot_id = before.boot_id
    for action in ("on", "reset", "cycle"):
        await s.power(action)
        booted = await s.new_boot(boot_id)
        native = domain_state(s.guest.system_id, s.guest.lane.connect)
        assert native == "running", f"after {action}: {native}"
        observed[action] = {"domain": native, "new_boot": True}
        boot_id = booted.boot_id
    observed["resume"] = await _memory_restore(s, paused=True)
    return observed


BODIES: dict[str, Callable[[Session], Awaitable[dict[str, object]]]] = {
    "control.power": power_body,
    "systems.delete_snapshot": delete_body,
    "systems.list_snapshots": list_body,
    "systems.restore": restore_body,
    "systems.snapshot": snapshot_body,
}


async def functional(
    run: CellRun,
    caller: HttpCaller,
    token_: str,
    tree: Path,
    manifest: dict[str, Any],
    guest: Guest,
) -> dict[str, object]:
    """Boot the fixture kernel, then run the tool's sequence; the tool alone uses the exposure."""
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
    endpoint = await ssh_endpoint(guest.op, guest.system_id)
    session = Session(run, guest, caller, token_, endpoint)
    observed = await BODIES[run.cell.operation](session)
    return {"tool": run.cell.operation, "kernel": sorted(deep.assertions), **observed}


def grants(project: str, role: str | None) -> Grants:
    """A token for ``project`` holding ``role``, or membership with no role."""
    return Grants(f"{project}-{role or 'member'}", (project,), {project: role} if role else {})


def _stranger() -> Grants:
    other = f"cov-{secrets.token_hex(4)}"
    return Grants(f"{other}-operator", (other,), {other: "operator"})


def _target_args(tool: str, system_id: str) -> dict[str, object]:
    args: dict[str, object] = {"system_id": system_id}
    if tool == "control.power":
        args["action"] = "reset"
    elif tool != "systems.list_snapshots":
        args["name"] = "cov-target"
    return args


def rejection(tool: str, boundary: Boundary, target: LaneTarget) -> Rejection:
    """The call ``boundary`` must reject, aimed at the torn-down lane target."""
    args = _target_args(tool, target.system_id)
    gate = GATES[tool]
    if boundary == "validation":
        return Rejection({**args, "system_id": 7}, grants(target.project, gate))
    if boundary == "authentication":
        # A viewer's issued-token call is refused by role (or only reads), writing nothing.
        return Rejection(args, grants(target.project, "viewer"))
    if boundary == "authorization":
        return Rejection(args, grants(target.project, _BELOW[gate]))
    assert boundary == "project-isolation", f"no {boundary} cell exists for {tool}"
    twin = _target_args(tool, str(uuid4()))
    return Rejection(args, _stranger(), frozenset({_ISOLATION[tool].value}), absent_twin=twin)


async def system_jobs(db_url: str, system_id: str) -> list[object]:
    """Row count and row-text SHA-256 of the jobs naming ``system_id`` in their payload."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*), coalesce(string_agg(t::text, ',' ORDER BY t::text), '') "
            "FROM jobs t WHERE t.payload->>'system_id' = %s",
            (system_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return [row[0], hashlib.sha256(str(row[1]).encode()).hexdigest()]


async def protected_state(db_url: str, target: LaneTarget, connect: Connect) -> dict[str, object]:
    """The target project's rows, the target's jobs and whether the provider defines its domain."""
    return {
        "project": await project_state(db_url, target.project),
        "jobs": await system_jobs(db_url, target.system_id),
        "domain_defined": await asyncio.to_thread(domain_defined, target.system_id, connect),
    }


def snapshot_of(
    db_url: str, target: LaneTarget, connect: Connect
) -> Callable[[], Awaitable[object]]:
    return partial(protected_state, db_url, target, connect)
