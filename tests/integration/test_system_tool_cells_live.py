"""Prove the x86_64 local-libvirt System lifecycle tool cells over HTTP (#3062, ADR-0722).

``live_stack``-marked. One parameter per native local-libvirt contract cell of the six
``systems.*`` lifecycle tools, framed by
:func:`~tests.integration.live_stack.tool_cells.run_tool_cell`. A functional cell provisions the
lane image in a fresh ``cov-<hex>`` project through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, calls the tool in the cell's
exposure, proves its effect against the worker's libvirt domain, the guest over SSH and its
disks, and proves owned cleanup. A rejection cell aims at the stack's
:func:`~tests.integration.live_stack.tool_cells.lane_target`, a torn-down System and its
released Allocation. ``docs/operating/runbooks/live-testing.md`` covers the run.
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import secrets
import socket
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import tempfile
import xml.etree.ElementTree as ET  # noqa: S405 - the worker's own domain XML  # nosec B405
from collections.abc import Awaitable, Callable, Mapping
from functools import cache, partial
from pathlib import Path
from typing import cast
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.cleanup import disk_absent, domain_disks
from tests.integration.live_stack.image_smoke import Endpoint, os_matches, ssh
from tests.integration.live_stack.scenario import (
    CellRun,
    Provision,
    authorize_ssh,
    catalog_profile,
    domain_xml,
    provision_catalog,
    ssh_probe,
)
from tests.integration.live_stack.spine import await_system_state, drain_job
from tests.integration.live_stack.tool_cells import (
    IDENTITY_PROBE,
    Boundary,
    Exposure,
    Grants,
    Guest,
    HttpCaller,
    LaneTarget,
    Rejection,
    boundary_of,
    lane_image,
    lane_target,
    on_lane_system,
    one,
    project_state,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)
from tests.mcp.json_data import data_mapping, data_str

pytestmark = pytest.mark.live_stack

TOOLS = (
    "systems.authorize_ssh_key",
    "systems.check_ssh_reachable",
    "systems.provision",
    "systems.reprovision",
    "systems.ssh_info",
    "systems.teardown",
)
# Each tool's project gate, and the role one rank below it (none below viewer).
_GATES = {
    "systems.authorize_ssh_key": "contributor",
    "systems.check_ssh_reachable": "viewer",
    "systems.provision": "contributor",
    "systems.reprovision": "contributor",
    "systems.ssh_info": "viewer",
    "systems.teardown": "admin",
}
_BELOW: dict[str, str | None] = {"viewer": None, "contributor": "viewer", "admin": "contributor"}
# Teardown answers an id outside the caller's projects with configuration_error, the others with
# not_found; each answer is byte-identical to the one for an absent id.
_CONFIG_ERROR_TOOLS = frozenset({"systems.teardown"})
_HOSTFWD = re.compile(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-:22")
_MARKER = "/root/kdive-cov-marker"
_LANE_VCPUS, _LANE_MEMORY_KIB = 2, 2 * 1024 * 1024
_KEYS = "cat /root/.ssh/authorized_keys"
Body = Callable[[HttpCaller, str, Guest], Awaitable[dict[str, object]]]


def _grants(project: str, role: str | None) -> Grants:
    """A token for ``project`` holding ``role``, or membership with no role."""
    return Grants(f"{project}-{role or 'member'}", (project,), {project: role} if role else {})


def _stranger() -> Grants:
    other = f"cov-{secrets.token_hex(4)}"
    return Grants(f"{other}-operator", (other,), {other: "operator"})


def _keypair(directory: Path) -> tuple[Path, str]:
    """A fresh ed25519 key under ``directory``: its private path and public line."""
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / "id_ed25519"
    subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "tool-cell", "-f", str(key)],
        check=True,
        timeout=30.0,
    )
    return key, (directory / "id_ed25519.pub").read_text(encoding="utf-8").strip()


@cache
def _public_key() -> str:
    """One valid public key the rejected ``authorize_ssh_key`` calls carry."""
    with tempfile.TemporaryDirectory() as scratch:
        return _keypair(Path(scratch))[1]


def _key_id(line: str) -> str:
    """``<type> <base64>`` of an ``authorized_keys`` line, ignoring options and comment."""
    tokens = line.split()
    start = next(i for i, t in enumerate(tokens) if t.startswith(("ssh-", "ecdsa-", "sk-")))
    return " ".join(tokens[start : start + 2])


def _keys(result: subprocess.CompletedProcess[str]) -> set[str]:
    assert result.returncode == 0, f"reading authorized_keys exited {result.returncode}"
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return {_key_id(line) for line in lines if not line.startswith("#")}


def _defined(system_id: str) -> bool:
    try:
        domain_xml(system_id)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return False
        raise
    return True


def _banner(endpoint: Endpoint) -> str:
    with socket.create_connection((endpoint.host, endpoint.port), timeout=10.0) as conn:
        return conn.recv(256).decode("ascii", "replace").strip()


async def _call(
    caller: HttpCaller, tool: str, args: Mapping[str, object], token: str
) -> ToolResponse:
    return one(await caller.call(tool, args, token, discover=True))


def _provisioner(caller: HttpCaller, token: str) -> Provision:
    """Provision through the cell's exposure: the tool under test is ``systems.provision``."""

    async def provision(op: LiveStackClient, allocation_id: str, profile: dict[str, object]) -> str:
        args = {"allocation_id": allocation_id, "profile": profile}
        system_id = data_str(await _call(caller, "systems.provision", args, token), "system_id")
        await await_system_state(op, "provision", system_id, "ready")
        return system_id

    return provision


async def _provision(_caller: HttpCaller, _token: str, guest: Guest) -> dict[str, object]:
    domain = ET.fromstring(domain_xml(guest.system_id))  # noqa: S314  # nosec B314
    vcpus = int(domain.findtext("vcpu") or 0)
    memory = domain.find("memory")
    assert memory is not None and memory.get("unit", "KiB") == "KiB", "domain memory unit"
    kib = int(memory.text or 0)
    assert (vcpus, kib) == (_LANE_VCPUS, _LANE_MEMORY_KIB), f"domain sized {vcpus}/{kib} KiB"
    return {"system": "ready", "boot_id_seen": True, "vcpus": vcpus, "memory_kib": kib}


async def _ssh_info(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    env = await _call(caller, "systems.ssh_info", {"system_id": guest.system_id}, token)
    coords = data_mapping(env, "ssh")
    xml = domain_xml(guest.system_id)
    forwarded = _HOSTFWD.search(xml)
    assert forwarded, "the domain XML carries no loopback SSH forward"
    assert coords.get("port") == int(forwarded.group(1)), f"ssh_info port {coords.get('port')}"
    endpoint = Endpoint(str(coords["host"]), int(cast(int, coords["port"])))
    probe = await asyncio.to_thread(ssh_probe, endpoint, guest.key, IDENTITY_PROBE)
    uuid = ET.fromstring(xml).findtext("uuid") or ""  # noqa: S314  # nosec B314
    assert probe.get("product_uuid", "").lower() == uuid.lower(), "guest is not this domain"
    return {"user": coords.get("user"), "port_matches_domain": True, "guest_is_domain": True}


async def _authorize(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    before = _keys(await asyncio.to_thread(ssh, guest.endpoint, guest.key, _KEYS))
    key, public = await asyncio.to_thread(_keypair, guest.scratch / "second")
    args = {"system_id": guest.system_id, "public_key": public}
    env = await _call(caller, "systems.authorize_ssh_key", args, token)
    await drain_job(guest.op, "authorize", env.object_id)
    after = _keys(await asyncio.to_thread(ssh, guest.endpoint, key, _KEYS))
    kept = _keys(await asyncio.to_thread(ssh, guest.endpoint, guest.key, _KEYS))
    assert after == kept == before | {_key_id(public)}, "authorized_keys changed beyond the key"
    return {"keys_before": len(before), "keys_after": len(after), "frame_key_kept": True}


async def _check(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    env = await _call(caller, "systems.check_ssh_reachable", {"system_id": guest.system_id}, token)
    job = await drain_job(guest.op, "check", env.object_id)
    verdict = json.loads(job.refs["result"])
    assert verdict.get("reachable") is True, f"verdict {verdict}"
    banner = await asyncio.to_thread(_banner, guest.endpoint)
    assert banner.startswith("SSH-"), "the endpoint sent no SSH banner"
    active = await asyncio.to_thread(ssh, guest.endpoint, guest.key, "systemctl is-active sshd")
    assert active.stdout.strip() == "active", f"sshd is {active.stdout.strip()!r}"
    return {"verdict": "reachable", "banner": banner.split("-", 2)[1], "sshd": "active"}


async def _reprovision(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    marked = await asyncio.to_thread(ssh, guest.endpoint, guest.key, f"touch {_MARKER}")
    assert marked.returncode == 0, f"marker write exited {marked.returncode}"
    old = list(guest.owned)
    profile = catalog_profile(guest.entry, guest.image, f"{guest.project}-unread")
    args = {"system_id": guest.system_id, "profile": profile}
    env = await _call(caller, "systems.reprovision", args, token)
    await drain_job(guest.op, "reprovision", env.object_id)
    await await_system_state(guest.op, "reprovision", guest.system_id, "ready")
    guest.owned.extend(p for p in domain_disks(domain_xml(guest.system_id)) if p not in old)
    after = guest.scratch / "after"
    after.mkdir()
    endpoint, key = await authorize_ssh(guest.op, guest.system_id, after, "cov")
    probe = await asyncio.to_thread(ssh_probe, endpoint, key)
    assert probe.get("boot_id") != guest.probe.get("boot_id"), "the guest did not reboot"
    assert os_matches(guest.entry, probe), "the replacement guest is not the catalog image"
    marker = await asyncio.to_thread(ssh, endpoint, key, f"test -e {_MARKER}")
    assert marker.returncode == 1, "the old install's marker survived the reprovision"
    return {"ready": True, "boot_id_changed": True, "marker": "absent", "disks": len(guest.owned)}


async def _teardown(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    env = await _call(caller, "systems.teardown", {"system_id": guest.system_id}, token)
    if env.status != "torn_down":
        await drain_job(guest.op, "teardown", env.object_id)
    await await_system_state(guest.op, "teardown", guest.system_id, "torn_down")
    assert not await asyncio.to_thread(_defined, guest.system_id), "the domain is still defined"
    surviving = [path for path in guest.owned if not disk_absent(path)]
    assert not surviving, f"owned disk(s) survived teardown: {surviving}"
    return {"system": "torn_down", "domain": "absent", "disks_absent": len(guest.owned)}


_FUNCTIONAL: dict[str, Body] = {
    "systems.authorize_ssh_key": _authorize,
    "systems.check_ssh_reachable": _check,
    "systems.provision": _provision,
    "systems.reprovision": _reprovision,
    "systems.ssh_info": _ssh_info,
    "systems.teardown": _teardown,
}


async def _functional(
    run: CellRun, caller: HttpCaller, base_url: str, issuer: OidcIssuer, db_url: str
) -> None:
    tool = run.cell.operation
    project = f"cov-{secrets.token_hex(4)}"
    token = caller.token(_grants(project, _GATES[tool]))
    provision = _provisioner(caller, token) if tool == "systems.provision" else provision_catalog
    await on_lane_system(
        run,
        base_url,
        issuer,
        db_url,
        project=project,
        body=partial(_FUNCTIONAL[tool], caller, token),
        provision=provision,
    )


def _target_args(
    tool: str, target: LaneTarget, system_id: str, allocation_id: str
) -> dict[str, object]:
    name, entry = lane_image()
    profile = catalog_profile(entry, name, f"{target.project}-unread")
    if tool == "systems.provision":
        return {"allocation_id": allocation_id, "profile": profile}
    args: dict[str, object] = {"system_id": system_id}
    if tool == "systems.authorize_ssh_key":
        args["public_key"] = _public_key()
    if tool == "systems.reprovision":
        args["profile"] = profile
    return args


def _rejection(tool: str, boundary: Boundary, target: LaneTarget) -> Rejection:
    args = _target_args(tool, target, target.system_id, target.allocation_id)
    gate = _GATES[tool]
    if boundary == "validation":
        key = "allocation_id" if tool == "systems.provision" else "system_id"
        return Rejection({**args, key: 7}, _grants(target.project, gate))
    if boundary == "authentication":
        # A viewer's issued-token control call is refused by role or readiness, writing nothing.
        return Rejection(args, _grants(target.project, "viewer"))
    if boundary == "authorization":
        return Rejection(args, _grants(target.project, _BELOW[gate]))
    category = (
        ErrorCategory.CONFIGURATION_ERROR
        if tool in _CONFIG_ERROR_TOOLS
        else ErrorCategory.NOT_FOUND
    )
    twin = _target_args(tool, target, str(uuid4()), str(uuid4()))
    return Rejection(args, _stranger(), frozenset({category.value}), absent_twin=twin)


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    caller = HttpCaller(cast(Exposure, run.cell.exposure), base_url, issuer)
    if run.cell.kind == "functional":
        await _functional(run, caller, base_url, issuer, db_url)
        return
    target = await lane_target(run, base_url, issuer, db_url)
    boundary = boundary_of(run.cell)
    snapshot = partial(project_state, db_url, target.project)
    await prove_rejection(
        run, caller, boundary, _rejection(run.cell.operation, boundary, target), snapshot
    )


def _cells() -> list[Cell]:
    host = platform.machine()
    return [c for c in tool_cells(TOOLS) if c.provider == "local-libvirt" and c.guest_arch == host]


@pytest.mark.parametrize("cell", _cells(), ids=lambda cell: cell.id)
def test_system_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a System lifecycle tool."""
    run_tool_cell(cell, _scenario)
