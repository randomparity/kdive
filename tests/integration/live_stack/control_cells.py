"""Traffic capture, diagnostic SysRq and crash-watch tool cells over HTTP (#3115, #3116).

A functional cell provisions the lane image through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, boots the verified ``longterm``
kernel fixture (:func:`~tests.integration.live_stack.capture_cells.boot`), then runs its tool's
sequence. Only the tool under test goes through the cell's exposure; the supporting calls use the
frame's operator client.

- ``control.capture_traffic``: the guest sends UDP datagrams carrying a fresh token to the
  user-network gateway while a capture runs; the downloaded pcap must hold that flow, and the
  domain must hold no capture filter afterwards. The System's host pcap spool joins ``owned``,
  so the frame's cleanup proves it reclaimed. The stored pcap is Run-owned and retained.
- ``control.diagnostic_sysrq``: the captured console holds the requested action's header and its
  output, and the guest's own kernel log holds one more header than before the call.
- ``control.watch_for_crash``: a watch with no signature ends ``not_fired`` within its bound; a
  second watch fires on a crash signature the guest writes to ``/dev/kmsg`` until the watch
  ends. A written signature, not a real panic, so it outlasts the watch's pickup baseline and
  the System stays ``ready``.

A rejection cell aims at :func:`~tests.integration.live_stack.capture_cells.capture_target`, a
torn-down, Run-bound System. No rejection cell records a kernel field: none declares kernel
inputs. No capability cell exists: both providers support all three (ADR-0722, 2026-10-13).

A remote cell (#3116) runs the same sequences on the provider lane: native reads and the capture
spool (the job's pool volume) come from the lane, the flow goes to the guest's libvirt-network
gateway, which is the NIC a remote capture taps (ADR-0432), and a rejection cell aims at
:func:`run_target`, a torn-down lane System and its booted Run that records the provider host.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import secrets
import struct
import subprocess  # noqa: S404 - the TimeoutExpired an ssh call raises  # nosec B404
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import libvirt_qemu
import psycopg

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.providers.shared.runtime_paths import domain_name_for
from tests.integration.live_stack.capture_cells import (
    CaptureTarget,
    Session,
    boot,
    drained,
    named_jobs,
)
from tests.integration.live_stack.image_smoke import ssh
from tests.integration.live_stack.power_snapshots import domain_defined, grants
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.spine import full_artifact_text, ok, scalar
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Connect,
    Guest,
    HttpCaller,
    Rejection,
    Snapshot,
    on_lane_system,
    project_state,
    settled,
)

TOOLS = ("control.capture_traffic", "control.diagnostic_sysrq", "control.watch_for_crash")
# Each tool's project gate (src/kdive/mcp/exposure.py).
GATES = dict.fromkeys(TOOLS, "contributor")
# A foreign or absent id: the System tools answer configuration_error, the Run-resolved
# capture_traffic not_found.
_ISOLATION = {
    "control.capture_traffic": ErrorCategory.NOT_FOUND,
    "control.diagnostic_sysrq": ErrorCategory.CONFIGURATION_ERROR,
    "control.watch_for_crash": ErrorCategory.CONFIGURATION_ERROR,
}
_ID = {
    "control.capture_traffic": "run_id",
    "control.diagnostic_sysrq": "system_id",
    "control.watch_for_crash": "system_id",
}
SYSRQ_COMMAND = "show_memory"
# Required non-id arguments, valid, so only the boundary under test can reject a call.
_EXTRA: dict[str, dict[str, object]] = {"control.diagnostic_sysrq": {"command": SYSRQ_COMMAND}}

FLOW_PORT = 9
_GATEWAY = "10.0.2.2"
_MIN_FLOW_RECORDS = 5
_CAPTURE_S = 15
_DOWNLOAD_TIMEOUT_S = 120.0
_ETHERNET = 1
_IPV4 = 0x0800
_UDP = 17
_PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": "<",
    b"\x4d\x3c\xb2\xa1": "<",
    b"\xa1\xb2\xc3\xd4": ">",
    b"\xa1\xb2\x3c\x4d": ">",
}
SYSRQ_HEADER = "sysrq: Show Memory"
SYSRQ_OUTPUT = "Mem-Info:"
QUIET_DEADLINE_S = 20.0
QUIET_SLACK_S = 10.0
FIRE_DEADLINE_S = 180.0
SIGNATURE = "Kernel panic"
_FLOW = (
    "setsid bash -c 'while :; do printf %s {token} > /dev/udp/{gateway}/{port}; sleep 0.5; done' "
    "</dev/null >/dev/null 2>&1 & echo pid=$!"
)
_KMSG = (
    "setsid bash -c 'for i in $(seq 1 {count}); do "
    'echo "<2>Kernel panic - not syncing: {token}" > /dev/kmsg; sleep 2; done\' '
    "</dev/null >/dev/null 2>&1 & echo pid=$!"
)
# The first default route outside the user-mode SSH NIC's subnet: a remote capture taps the
# libvirt-network NIC (ADR-0432), and the user-mode NIC is restrict=on (ADR-0291).
_ROUTE = (
    "printf 'gateway=%s\\n' "
    "\"$(ip -4 route show default | awk '$3 !~ /^10[.]0[.]2[.]/ {print $3; exit}')\""
)
_SLIRP = ipaddress.ip_network("10.0.2.0/24")
_TARGET_KEYS = ("host_os", "host_arch", "guest_os", "guest_arch", "accelerator", "image_sha256")
_SYSRQ_COUNT = f"printf 'count=%s\\n' \"$(dmesg | grep -c '{SYSRQ_HEADER}')\""
_SYSRQ_ENABLE = "sysctl -qw kernel.sysrq=1 && dmesg -n 8 && echo ok=1"


def token() -> str:
    return f"kdivecov{secrets.token_hex(8)}"


def flow_records(pcap: bytes, payload: bytes, port: int = FLOW_PORT) -> int:
    """Whole Ethernet records of IPv4/UDP datagrams to ``port`` whose payload is ``payload``.

    A truncated final record is not counted; a file that is not Ethernet libpcap fails.
    """
    endian = _PCAP_MAGICS.get(pcap[:4])
    assert endian is not None and len(pcap) >= 24, "the capture is not a libpcap file"
    (link,) = struct.unpack(endian + "I", pcap[20:24])
    assert link == _ETHERNET, f"the capture's link type is {link}, not Ethernet"
    offset, count = 24, 0
    while offset + 16 <= len(pcap):
        (length,) = struct.unpack(endian + "I", pcap[offset + 8 : offset + 12])
        frame = pcap[offset + 16 : offset + 16 + length]
        offset += 16 + length
        if len(frame) == length and _is_flow(frame, payload, port):
            count += 1
    return count


def _is_flow(frame: bytes, payload: bytes, port: int) -> bool:
    if len(frame) < 34 or struct.unpack("!H", frame[12:14])[0] != _IPV4:
        return False
    ip = frame[14:]
    header = (ip[0] & 0x0F) * 4
    if ip[9] != _UDP or len(ip) < header + 8:
        return False
    udp = ip[header:]
    port_, length = struct.unpack("!HH", udp[2:6])
    return port_ == port and udp[8:length] == payload


def require_sysrq(text: str, before: int, after: int) -> None:
    """The capture holds the requested action's header then its output; the guest logged one."""
    header = text.find(SYSRQ_HEADER)
    assert header >= 0, f"the SysRq capture holds no {SYSRQ_HEADER!r}"
    assert SYSRQ_OUTPUT in text[header:], f"the capture holds no {SYSRQ_OUTPUT!r} after the header"
    assert after == before + 1, f"the guest logged {after - before} SysRq headers, not one"


def require_quiet(verdict: Mapping[str, object], deadline_s: float) -> None:
    """A watch with no signature: ``not_fired`` at its deadline, within the slack."""
    assert verdict.get("outcome") == "not_fired", f"the quiet watch ended {verdict.get('outcome')}"
    assert verdict.get("fired") is False, "the quiet watch reports fired"
    elapsed = float(str(verdict.get("elapsed_s")))
    assert deadline_s <= elapsed <= deadline_s + QUIET_SLACK_S, (
        f"the quiet watch ended after {elapsed:.1f} s, outside [{deadline_s}, "
        f"{deadline_s + QUIET_SLACK_S}]"
    )


def require_fired(verdict: Mapping[str, object], marker: str, deadline_s: float) -> None:
    """A watch that saw the written signature, with the cell's marker in its matched slice."""
    assert verdict.get("outcome") == "fired", f"the watch ended {verdict.get('outcome')}"
    assert verdict.get("signature") == SIGNATURE, f"the watch matched {verdict.get('signature')}"
    assert marker in str(verdict.get("matched")), "the matched slice is not the cell's signature"
    assert float(str(verdict.get("elapsed_s"))) < deadline_s, "the watch fired at its deadline"


def verdict_of(job: ToolResponse) -> dict[str, object]:
    raw = job.refs.get("result")
    assert raw, "the watch job carries no verdict"
    verdict = json.loads(raw)
    assert isinstance(verdict, dict), "the watch verdict is not an object"
    return verdict


def gateway_of(probe: Mapping[str, str]) -> str:
    """The guest's libvirt-network IPv4 default gateway from ``_ROUTE``'s answer.

    The user-mode SSH NIC's gateway is refused: that NIC is not the captured netdev.
    """
    value = probe.get("gateway", "")
    try:
        address = ipaddress.ip_address(value)
        assert address.version == 4 and address not in _SLIRP
    except AssertionError, ValueError:
        raise AssertionError(f"the guest has no libvirt-network gateway ({value!r})") from None
    return value


def capture_filters(system_id: str, connect: Connect) -> list[str]:
    """The QOM objects named ``kdive-dump-*`` on the System's domain."""
    conn = connect()
    try:
        domain = conn.lookupByName(domain_name_for(UUID(system_id)))
        command = json.dumps({"execute": "qom-list", "arguments": {"path": "/objects"}})
        reply = json.loads(libvirt_qemu.qemuMonitorCommand(domain, command, 0))
    finally:
        conn.close()
    return [o["name"] for o in reply["return"] if str(o.get("name", "")).startswith("kdive-dump-")]


async def _background(s: Session, command: str) -> str:
    pid = (await s.shell(command))["pid"]
    assert pid.isdigit(), "the guest reported no background pid"
    return pid


async def _stop(s: Session, pid: str) -> None:
    """Best effort: a guest that stopped answering must not mask the cell's own outcome.

    The System's teardown reclaims a writer this cannot reach.
    """
    with contextlib.suppress(subprocess.TimeoutExpired):
        await asyncio.to_thread(
            ssh, s.endpoint, s.guest.key, f"kill {pid}", deadline_s=0.0, timeout_s=30.0
        )


async def _download(s: Session, artifact_id: str) -> bytes:
    env = ok(
        await scalar(
            s.guest.op,
            "artifacts.fetch_raw",
            run_id=s.run_id,
            asset="pcap",
            artifact_id=artifact_id,
        ),
        "fetch-pcap",
    )
    url = env.refs.get("download_uri")
    assert url, "artifacts.fetch_raw returned no download URI"
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_S) as http:
        response = await http.get(url)
    response.raise_for_status()
    assert len(response.content) == env.data.get("size_bytes"), "the pcap is not its stated size"
    return response.content


async def capture_body(s: Session, _db_url: str) -> dict[str, object]:
    """A capture holds the guest's synthetic UDP flow; the domain keeps no capture filter."""
    marker = token()
    local = s.guest.lane.provider == "local-libvirt"
    gateway = _GATEWAY if local else gateway_of(await s.shell(_ROUTE))
    flow = _FLOW.format(token=marker, gateway=gateway, port=FLOW_PORT)
    # The spool the capture writes, which the frame's cleanup proves reclaimed: the System's
    # directory locally, owned before the call; the job's pool volume remotely, named once the
    # call returns the job id.
    spool = partial(s.guest.lane.spool, s.guest.system_id)
    if local:
        s.guest.owned.append(spool(""))
    pid = await _background(s, flow)
    try:
        env = await s.exposed(run_id=s.run_id, duration_s=_CAPTURE_S)
        if not local:
            s.guest.owned.append(await asyncio.to_thread(spool, env.object_id))
        job = await drained(s.guest.op, "capture", env.object_id)
    finally:
        await _stop(s, pid)
    artifact = job.refs.get("result")
    assert artifact, "the capture job carries no pcap artifact"
    pcap = await _download(s, artifact)
    records = flow_records(pcap, marker.encode())
    assert records >= _MIN_FLOW_RECORDS, f"the pcap holds {records} flow records"
    filters = await asyncio.to_thread(capture_filters, s.guest.system_id, s.guest.lane.connect)
    assert not filters, f"the domain still holds capture filters {filters}"
    return {"pcap_bytes": len(pcap), "flow_records": records, "filters_after": 0}


async def sysrq_body(s: Session, _db_url: str) -> dict[str, object]:
    """The captured console shows the requested action; the guest logged exactly one."""
    await s.shell(_SYSRQ_ENABLE)
    before = int((await s.shell(_SYSRQ_COUNT))["count"])
    env = await s.exposed(system_id=s.guest.system_id, command=SYSRQ_COMMAND)
    job = await drained(s.guest.op, "sysrq", env.object_id)
    artifact = job.refs.get("result")
    assert artifact, "the SysRq job carries no console artifact"
    text = await full_artifact_text(s.guest.op, artifact, "sysrq-capture")
    after = int((await s.shell(_SYSRQ_COUNT))["count"])
    require_sysrq(text, before, after)
    return {"command": SYSRQ_COMMAND, "header": SYSRQ_HEADER, "guest_headers_added": 1}


async def live_watches(db_url: str, system_id: str) -> int:
    """Queued or running watch jobs naming ``system_id``."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*) FROM jobs WHERE kind = 'watch_for_crash' "
            "AND state IN ('queued', 'running') AND payload->>'system_id' = %s",
            (system_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def watch_body(s: Session, db_url: str) -> dict[str, object]:
    """A quiet watch ends at its bound unfired; a written signature fires the next one."""
    system = s.guest.system_id
    started = time.monotonic()
    env = await s.exposed(system_id=system, deadline_s=QUIET_DEADLINE_S)
    quiet = verdict_of(await drained(s.guest.op, "quiet-watch", env.object_id))
    require_quiet(quiet, QUIET_DEADLINE_S)
    quiet_wall = time.monotonic() - started
    marker = token()
    env = await s.exposed(system_id=system, deadline_s=FIRE_DEADLINE_S)
    count = int(FIRE_DEADLINE_S // 2)
    pid = await _background(s, _KMSG.format(count=count, token=marker))
    try:
        fired = verdict_of(await drained(s.guest.op, "crash-watch", env.object_id))
    finally:
        await _stop(s, pid)
    require_fired(fired, marker, FIRE_DEADLINE_S)
    assert await live_watches(db_url, system) == 0, "a watch job is still queued or running"
    return {
        "quiet": {"outcome": "not_fired", "elapsed_s": quiet["elapsed_s"], "wall_s": quiet_wall},
        "fired": {"signature": SIGNATURE, "elapsed_s": fired["elapsed_s"]},
        "live_watches_after": 0,
    }


BODIES: dict[str, Callable[[Session, str], Awaitable[dict[str, object]]]] = {
    "control.capture_traffic": capture_body,
    "control.diagnostic_sysrq": sysrq_body,
    "control.watch_for_crash": watch_body,
}


async def functional(
    run: CellRun,
    caller: HttpCaller,
    tree: Path,
    manifest: Mapping[str, Any],
    db_url: str,
    guest: Guest,
) -> dict[str, object]:
    """Boot the fixture kernel, then run the tool's sequence as the tool's gate role."""
    run_id, endpoint = await boot(run, guest, tree, manifest)
    role = grants(guest.project, GATES[run.cell.operation])
    session = Session(run, guest, caller, role, manifest, run_id, endpoint)
    observed = await BODIES[run.cell.operation](session, db_url)
    return {"tool": run.cell.operation, **observed}


def rejection(tool: str, boundary: Boundary, target: CaptureTarget) -> Rejection:
    """The call ``boundary`` must reject, aimed at the capture target."""
    key = _ID[tool]
    ident = target.run_id if key == "run_id" else target.system_id
    args: dict[str, object] = {key: ident, **_EXTRA.get(tool, {})}
    gate = GATES[tool]
    if boundary == "validation":
        return Rejection({**args, key: 7}, grants(target.project, gate))
    if boundary == "authentication":
        # A viewer's issued-token call is refused by role, writing nothing.
        return Rejection(args, grants(target.project, "viewer"))
    if boundary == "authorization":
        return Rejection(args, grants(target.project, "viewer"))
    assert boundary == "project-isolation", f"no {boundary} cell exists for {tool}"
    stranger = grants(f"cov-{secrets.token_hex(4)}", "admin")
    twin = {**args, key: str(uuid4())}
    return Rejection(args, stranger, frozenset({_ISOLATION[tool].value}), absent_twin=twin)


# --- the remote rejection target (#3116) -----------------------------------------------------


def target_keys(observed: Mapping[str, object]) -> dict[str, object]:
    """The provider-host and lane context a rejection record carries; no kernel field."""
    return {key: observed[key] for key in _TARGET_KEYS if key in observed}


@dataclass(frozen=True)
class _Failed:
    error: Exception
    artifacts: tuple[str, ...]


_TARGETS: dict[tuple[str, str], CaptureTarget | _Failed] = {}


async def _prepare_run_target(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    fixture: Callable[[], tuple[Path, dict[str, Any]]],
    scratch: CellRun,
) -> CaptureTarget:
    tree, manifest = fixture()
    project = f"cov-{secrets.token_hex(4)}"
    ids: dict[str, str] = {}

    async def body(guest: Guest) -> dict[str, object]:
        run_id, _endpoint = await boot(scratch, guest, tree, manifest)
        ids.update(system=guest.system_id, run=run_id)
        return {"run_target": "prepared"}

    await on_lane_system(scratch, base_url, issuer, db_url, project=project, body=body)
    await settled(db_url, project)
    summary = run.writer.artifact(
        {
            "run_target": {"system": "torn_down", "run": "booted"},
            "cleanup": scratch.assertions["cleanup"],
        }
    )
    return CaptureTarget(
        project,
        ids["system"],
        ids["run"],
        "",
        target_keys(scratch.observed),
        (*scratch.artifacts, *scratch.assertions.values(), summary),
    )


async def run_target(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    fixture: Callable[[], tuple[Path, dict[str, Any]]],
) -> CaptureTarget:
    """A torn-down lane System and its booted Run, prepared once per stack and provider.

    The remote rejection target: no crash options and no debug session, which none of the
    three tools reads. A failed or blocked preparation is replayed, with its artifacts, for
    every later cell rather than retried.
    """
    key = (base_url, run.cell.provider)
    cached = _TARGETS.get(key)
    if cached is None:
        scratch = CellRun(run.cell, run.writer)
        try:
            cached = await _prepare_run_target(run, base_url, issuer, db_url, fixture, scratch)
        except Exception as exc:  # noqa: BLE001 - remembered and replayed for every cell
            cached = _Failed(exc, (*scratch.artifacts, *scratch.assertions.values()))
        _TARGETS[key] = cached
    if isinstance(cached, _Failed):
        run.artifacts.extend(cached.artifacts)
        if isinstance(cached.error, ScenarioStop):
            raise ScenarioStop(cached.error.outcome, str(cached.error))
        raise AssertionError(f"the run target could not be prepared: {cached.error!r}")
    run.observed |= cached.observed
    run.artifacts.extend(cached.artifacts)
    return cached


def target_state(db_url: str, target: CaptureTarget, connect: Connect) -> Snapshot:
    """The project's rows, the jobs naming the System or Run, and the domain's definedness."""

    async def state() -> dict[str, object]:
        return {
            "project": await project_state(db_url, target.project),
            "jobs": await named_jobs(db_url, target.system_id, target.run_id),
            "domain_defined": await asyncio.to_thread(domain_defined, target.system_id, connect),
        }

    return state
