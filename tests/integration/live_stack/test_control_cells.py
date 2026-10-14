"""Unit tests of the local traffic, SysRq and crash-watch cell oracles (no stack needed)."""

from __future__ import annotations

import struct
from collections.abc import Mapping
from typing import cast

import pytest

from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import build_contract
from scripts.coverage_campaign.gridgen import generate_rows
from tests.integration.live_stack import control_cells
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
from tests.integration.live_stack.tool_cells import Boundary

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
