"""Unit tests of the local traffic, SysRq and crash-watch cell oracles (no stack needed)."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Outcome
from scripts.coverage_campaign.gridgen import generate_rows
from tests.integration.live_stack import capture_cells, control_cells
from tests.integration.live_stack.capture_cells import CaptureTarget
from tests.integration.live_stack.control_cells import (
    FLOW_PORT,
    QUIET_DEADLINE_S,
    SIGNATURE,
    SYSRQ_HEADER,
    flow_records,
    rejection,
    require_fired,
    require_quiet,
    require_sysrq,
    verdict_of,
)
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import Boundary, Guest

_TOKEN = b"kdivecov0123456789abcdef"


def _udp(payload: bytes, port: int = FLOW_PORT, ethertype: int = 0x0800, proto: int = 17) -> bytes:
    udp = struct.pack("!HHHH", 40000, port, 8 + len(payload), 0) + payload
    ip = bytes([0x45, 0]) + struct.pack("!H", 20 + len(udp)) + bytes(5) + bytes([proto]) + bytes(10)
    frame = bytes(12) + struct.pack("!H", ethertype) + ip + udp
    return frame + bytes(max(0, 60 - len(frame)))  # Ethernet pads a short frame


def _pcap(frames: list[bytes], *, link: int = 1, endian: str = "<", cut: int = 0) -> bytes:
    data = struct.pack(endian + "IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, link)
    for frame in frames:
        data += struct.pack(endian + "IIII", 0, 0, len(frame), len(frame)) + frame
    return data[: len(data) - cut]


def test_flow_records_count_only_the_token_flow() -> None:
    frames = [
        _udp(_TOKEN),
        _udp(_TOKEN),
        _udp(b"short"),  # padded to the Ethernet minimum; the UDP length bounds the payload
        _udp(_TOKEN, port=FLOW_PORT + 1),
        _udp(b"kdivecovffffffffffffffff"),
        _udp(_TOKEN, ethertype=0x86DD),
        _udp(_TOKEN, proto=6),
        _udp(_TOKEN),
    ]
    assert flow_records(_pcap(frames), _TOKEN) == 3
    assert flow_records(_pcap([_udp(b"short")]), b"short") == 1
    # A capture cut inside its last record does not count that record.
    assert flow_records(_pcap(frames, cut=4), _TOKEN) == 2
    # Even when only the cut record's padding is missing.
    assert flow_records(_pcap([_udp(b"short")], cut=4), b"short") == 0


def test_flow_records_reads_a_big_endian_capture() -> None:
    assert flow_records(_pcap([_udp(_TOKEN)], endian=">"), _TOKEN) == 1


def test_flow_records_refuses_a_non_ethernet_or_non_pcap_file() -> None:
    with pytest.raises(AssertionError, match="not Ethernet"):
        flow_records(_pcap([_udp(_TOKEN)], link=101), _TOKEN)
    with pytest.raises(AssertionError, match="not a libpcap"):
        flow_records(b"\x00" * 64, _TOKEN)
    with pytest.raises(AssertionError, match="not a libpcap"):
        flow_records(b"\xd4\xc3\xb2\xa1", _TOKEN)


_CONSOLE = f"[ 9.1] {SYSRQ_HEADER}\n[ 9.1] Mem-Info:\n[ 9.1] active_anon:1\n"


def test_sysrq_needs_the_header_its_output_and_one_guest_header() -> None:
    require_sysrq(_CONSOLE, 2, 3)
    with pytest.raises(AssertionError, match="holds no 'sysrq"):
        require_sysrq("[ 9.1] sysrq: Show State\n[ 9.1] Mem-Info:\n", 0, 1)
    with pytest.raises(AssertionError, match="no 'Mem-Info:' after"):
        require_sysrq(f"[ 1.0] Mem-Info:\n[ 9.1] {SYSRQ_HEADER}\n", 0, 1)
    with pytest.raises(AssertionError, match="logged 0 SysRq headers"):
        require_sysrq(_CONSOLE, 3, 3)


def _verdict(outcome: str, elapsed: float, **extra: object) -> dict[str, object]:
    return {"outcome": outcome, "fired": outcome == "fired", "elapsed_s": elapsed, **extra}


def test_quiet_watch_ends_unfired_at_its_bound() -> None:
    require_quiet(_verdict("not_fired", QUIET_DEADLINE_S + 0.4), QUIET_DEADLINE_S)
    with pytest.raises(AssertionError, match="ended fired"):
        require_quiet(_verdict("fired", 3.0), QUIET_DEADLINE_S)
    with pytest.raises(AssertionError, match="outside"):
        require_quiet(_verdict("not_fired", QUIET_DEADLINE_S - 1), QUIET_DEADLINE_S)
    with pytest.raises(AssertionError, match="outside"):
        require_quiet(_verdict("not_fired", QUIET_DEADLINE_S + 11), QUIET_DEADLINE_S)


def test_fired_watch_names_the_written_signature() -> None:
    hit = _verdict("fired", 4.0, signature=SIGNATURE, matched="Kernel panic - not syncing: tok1")
    require_fired(hit, "tok1", 180.0)
    with pytest.raises(AssertionError, match="ended not_fired"):
        require_fired(_verdict("not_fired", 180.0), "tok1", 180.0)
    with pytest.raises(AssertionError, match="not the cell's signature"):
        require_fired(hit, "tok2", 180.0)
    with pytest.raises(AssertionError, match="matched BUG:"):
        require_fired({**hit, "signature": "BUG:"}, "tok1", 180.0)
    with pytest.raises(AssertionError, match="at its deadline"):
        require_fired({**hit, "elapsed_s": 180.0}, "tok1", 180.0)


def test_verdict_is_the_job_result_json() -> None:
    job = ToolResponse.success("job-1", "succeeded", refs={"result": '{"outcome":"not_fired"}'})
    assert verdict_of(job) == {"outcome": "not_fired"}
    with pytest.raises(AssertionError, match="no verdict"):
        verdict_of(ToolResponse.success("job-1", "succeeded"))


_TARGET = CaptureTarget(
    "cov-t",
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
    "33333333-3333-3333-3333-333333333333",
    {},
    (),
)
_REQUIRED: Mapping[str, set[str]] = {
    row.tool: {str(name) for name in cast(list[object], row.parameters.get("required", []))}
    for row in generate_rows()
    if row.tool in control_cells.TOOLS
}


@pytest.mark.parametrize("tool", control_cells.TOOLS)
@pytest.mark.parametrize("boundary", ["authentication", "authorization", "project-isolation"])
def test_rejections_carry_every_required_argument(tool: str, boundary: Boundary) -> None:
    call = rejection(tool, boundary, _TARGET)
    assert _REQUIRED[tool] and _REQUIRED[tool] <= set(call.args), (
        f"{tool} {boundary} omits {_REQUIRED[tool] - set(call.args)}"
    )


@pytest.mark.parametrize("tool", control_cells.TOOLS)
def test_rejections_aim_at_the_target_with_the_gate_below(tool: str) -> None:
    key = "run_id" if tool == "control.capture_traffic" else "system_id"
    ident = _TARGET.run_id if key == "run_id" else _TARGET.system_id
    auth = rejection(tool, "authentication", _TARGET)
    assert auth.args[key] == ident and auth.grants.roles == {"cov-t": "viewer"}
    assert rejection(tool, "authorization", _TARGET).grants.roles == {"cov-t": "viewer"}
    invalid = rejection(tool, "validation", _TARGET)
    assert invalid.args[key] == 7 and invalid.grants.roles == {"cov-t": "contributor"}
    assert set(invalid.args) == set(auth.args)
    isolated = rejection(tool, "project-isolation", _TARGET)
    assert "cov-t" not in isolated.grants.projects
    assert isolated.absent_twin is not None and isolated.absent_twin.keys() == isolated.args.keys()
    assert isolated.absent_twin[key] != ident
    category = "not_found" if tool == "control.capture_traffic" else "configuration_error"
    assert isolated.categories == {category}


def test_no_capability_rejection_cell_is_carried() -> None:
    cells = [
        c
        for c in build_contract().cells
        if c.operation in control_cells.TOOLS and c.provider == "local-libvirt"
    ]
    boundaries = {c.scenario_id.rsplit("/", 1)[1] for c in cells if c.kind == "rejection"}
    assert boundaries == {"authentication", "authorization", "project-isolation", "validation"}
    with pytest.raises(AssertionError, match="no capability cell"):
        rejection("control.watch_for_crash", "capability", _TARGET)


def _functional_cell(provider: str) -> Cell:
    return next(
        c
        for c in build_contract().cells
        if c.operation == "control.watch_for_crash"
        and c.kind == "functional"
        and c.provider == provider
        and c.guest_arch == "x86_64"
    )


def test_boot_uses_the_lane_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def kernel(*_: object) -> tuple[str, None]:
        return "d" * 64, None

    async def deep(*_: object, installed_kernel: object, **__: object) -> str:
        seen.append(installed_kernel)
        return "run-1"

    async def endpoint(*_: object) -> object:
        return "endpoint"

    monkeypatch.setattr(capture_cells, "deep_body", deep)
    monkeypatch.setattr(capture_cells, "ssh_endpoint", endpoint)
    lane = type("L", (), {"entry": None, "installed_kernel": staticmethod(kernel)})()
    guest = type(
        "G",
        (),
        {
            "op": None,
            "system_id": "s",
            "owned": [],
            "project": "p",
            "lane": lane,
            "scratch": tmp_path,
        },
    )()
    run = CellRun(_functional_cell("remote-libvirt"), EvidenceWriter(tmp_path))
    booted = asyncio.run(capture_cells.boot(run, cast(Guest, guest), tmp_path, {}))
    assert booted == ("run-1", "endpoint")
    assert seen == [kernel]


@pytest.mark.parametrize(
    ("probe", "ok"),
    [
        ({"gateway": "192.168.122.1"}, True),
        ({"gateway": ""}, False),
        ({"gateway": "fe80::1"}, False),
        ({"gateway": "10.0.2.2"}, False),
        ({}, False),
    ],
)
def test_gateway_is_the_libvirt_network_ipv4_gateway(probe: dict[str, str], ok: bool) -> None:
    if ok:
        assert control_cells.gateway_of(probe) == probe["gateway"]
    else:
        with pytest.raises(AssertionError, match="libvirt-network gateway"):
            control_cells.gateway_of(probe)


_RUN_TARGET = CaptureTarget("cov-t", "sys", "run", "", {"host_os": "rocky:10.2"}, ("a" * 64,))


def test_run_target_prepares_once_per_provider_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    issuer = cast(OidcIssuer, object())

    async def prepare(run: CellRun, *rest: object) -> CaptureTarget:
        calls.append(run.cell.provider)
        if run.cell.provider == "local-libvirt":
            cast(CellRun, rest[-1]).artifacts.append("b" * 64)  # the scratch record
            raise ScenarioStop(Outcome.BLOCKED, "no lane")
        return _RUN_TARGET

    def fixture() -> tuple[Path, dict[str, Any]]:
        raise AssertionError("the stub preparation reads no fixture")

    monkeypatch.setattr(control_cells, "_TARGETS", {})
    monkeypatch.setattr(control_cells, "_prepare_run_target", prepare)
    remote = CellRun(_functional_cell("remote-libvirt"), EvidenceWriter(tmp_path))
    for _ in range(2):
        target = asyncio.run(control_cells.run_target(remote, "u", issuer, "db", fixture))
        assert target is _RUN_TARGET
    local = CellRun(_functional_cell("local-libvirt"), EvidenceWriter(tmp_path))
    for _ in range(2):
        with pytest.raises(ScenarioStop):
            asyncio.run(control_cells.run_target(local, "u", issuer, "db", fixture))
    assert calls == ["remote-libvirt", "local-libvirt"]
    assert local.artifacts.count("b" * 64) == 2
    assert remote.observed["host_os"] == "rocky:10.2"
    assert remote.artifacts.count("a" * 64) == 2


def test_run_target_keeps_host_and_lane_keys() -> None:
    observed = {
        "host_os": "rocky:10.2",
        "host_arch": "x86_64",
        "guest_os": "fedora:43",
        "guest_arch": "x86_64",
        "accelerator": "kvm",
        "image_sha256": "f" * 64,
        "kernel_sha256": "c" * 64,
    }
    kept = control_cells.target_keys(observed)
    assert kept == {k: v for k, v in observed.items() if k != "kernel_sha256"}


def test_target_state_reads_jobs_and_domain_through_the_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = object()
    seen: list[object] = []

    async def project_state(_db: str, project: str) -> dict[str, list[object]]:
        seen.append(project)
        return {}

    async def named_jobs(_db: str, *ids: str) -> list[object]:
        seen.append(ids)
        return [0, ""]

    def defined(system_id: str, conn: object) -> bool:
        seen.append((system_id, conn))
        return False

    monkeypatch.setattr(control_cells, "project_state", project_state)
    monkeypatch.setattr(control_cells, "named_jobs", named_jobs)
    monkeypatch.setattr(control_cells, "domain_defined", defined)
    state = asyncio.run(control_cells.target_state("db", _TARGET, cast(Any, connect))())
    assert state == {"project": {}, "jobs": [0, ""], "domain_defined": False}
    assert seen == ["cov-t", (_TARGET.system_id, _TARGET.run_id), (_TARGET.system_id, connect)]


@dataclass
class _CaptureLane:
    provider: str
    events: list[str] = field(default_factory=list)

    def spool(self, system_id: str) -> Callable[[str], str]:
        self.events.append("spool-resolved")
        return lambda job_id: f"spool:{system_id}:{job_id}"

    def connect(self) -> object:
        raise AssertionError("capture_filters is stubbed")


@dataclass
class _CaptureSession:
    guest: Any
    run_id: str = "run-1"
    commands: list[str] = field(default_factory=list)
    owned_at_call: list[str] = field(default_factory=list)

    async def shell(self, command: str) -> dict[str, str]:
        self.commands.append(command)
        return {"gateway": "192.168.122.1"} if "route" in command else {"pid": "42"}

    async def exposed(self, **_args: object) -> ToolResponse:
        self.guest.lane.events.append("called")
        self.owned_at_call.extend(self.guest.owned)
        return ToolResponse.success("job-9", "queued")


@pytest.mark.parametrize(
    ("provider", "gateway", "owned_before_call"),
    [("local-libvirt", "10.0.2.2", True), ("remote-libvirt", "192.168.122.1", False)],
)
def test_capture_owns_the_lane_spool_and_floods_the_lane_gateway(
    monkeypatch: pytest.MonkeyPatch, provider: str, gateway: str, owned_before_call: bool
) -> None:
    lane = _CaptureLane(provider)
    guest = type("G", (), {"lane": lane, "owned": [], "system_id": "sys", "op": None})()
    session = _CaptureSession(guest)

    async def drained(*_: object) -> ToolResponse:
        return ToolResponse.success("job-9", "succeeded", refs={"result": "art-1"})

    async def download(*_: object) -> bytes:
        return _pcap([_udp(_TOKEN)] * 5)

    async def stop(*_: object) -> None:
        return None

    monkeypatch.setattr(control_cells, "token", lambda: _TOKEN.decode())
    monkeypatch.setattr(control_cells, "drained", drained)
    monkeypatch.setattr(control_cells, "_download", download)
    monkeypatch.setattr(control_cells, "_stop", stop)
    monkeypatch.setattr(control_cells, "capture_filters", lambda *_: [])
    observed = asyncio.run(control_cells.capture_body(cast(Any, session), "db"))
    assert observed["flow_records"] == 5
    flow = next(c for c in session.commands if "/dev/udp/" in c)
    assert f"/dev/udp/{gateway}/{FLOW_PORT}" in flow
    expected = "spool:sys:" if owned_before_call else "spool:sys:job-9"
    assert guest.owned == [expected]
    assert session.owned_at_call == ([expected] if owned_before_call else [])
    assert lane.events == ["spool-resolved", "called"]
