"""Unit tests of the local capture cell oracles, options and rejections (no stack needed)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import struct
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.capture_cells import (
    PANIC,
    TOOLS,
    CaptureTarget,
    Session,
    crash_evidence,
    crash_profile,
    kdump_armed,
    lane_keys,
    options,
    panic_seen,
    rejection,
    require_core,
    require_tool,
    script_facts,
    transcript_names,
    vmcoreinfo,
)
from tests.integration.live_stack.image_smoke import Endpoint
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import Grants, Guest, HttpCaller, boundary_of

_MANIFEST = {"build_id": "ab" * 20, "release": "6.18.54-kdive"}
_INFO = f"OSRELEASE={_MANIFEST['release']}\nPAGESIZE=4096\nBUILD-ID={'ab' * 20}\n".encode()
_TARGET = CaptureTarget("cov-t", "sys-1", "run-1", "ses-1", {}, ())


def _note(name: bytes, desc: bytes) -> bytes:
    padded = name + b"\0" * (-len(name) % 4)
    return struct.pack("<III", len(name), len(desc), 0) + padded + desc + b"\0" * (-len(desc) % 4)


def _elf(notes: bytes, *, load: bytes = b"", xnum: bool = False) -> bytes:
    """A 64-bit LE ELF core: one PT_NOTE and one PT_LOAD segment, after the headers."""
    phoff, phentsize, phnum = 64, 56, 2
    shoff = phoff + phnum * phentsize
    data_at = shoff + 64
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<QQ", header, 0x20, phoff, shoff if xnum else 0)
    struct.pack_into("<HH", header, 0x36, phentsize, 0xFFFF if xnum else phnum)
    note = struct.pack("<IIQQQQQQ", 4, 0, data_at, 0, 0, len(notes), len(notes), 4)
    pload = struct.pack("<IIQQQQQQ", 1, 0, data_at + len(notes), 0, 0, len(load), len(load), 0)
    section = bytearray(64)
    struct.pack_into("<I", section, 44, phnum)
    return bytes(header) + note + pload + bytes(section) + notes + load


def _kdump(info: bytes, *, block: int = 4096) -> bytes:
    header = bytearray(block)
    header[:8] = b"KDUMP   "
    struct.pack_into("<i", header, 428, block)
    sub = bytearray(block)
    offset = 2 * block
    struct.pack_into("<qQ", sub, 32, offset, len(info))
    return bytes(header) + bytes(sub) + info


def test_vmcoreinfo_reads_the_elf_note_and_ignores_strings_elsewhere() -> None:
    notes = _note(b"CORE", b"\0" * 16) + _note(b"VMCOREINFO", _INFO)
    core = _elf(notes, load=b"OSRELEASE=%s\nBUILD-ID=%20phN\n")
    info = vmcoreinfo(io.BytesIO(core))
    assert info == _INFO
    require_core(info, _MANIFEST)


def test_vmcoreinfo_reads_the_extended_program_header_count() -> None:
    core = _elf(_note(b"VMCOREINFO", _INFO), xnum=True)
    assert vmcoreinfo(io.BytesIO(core)) == _INFO


def test_vmcoreinfo_reads_the_kdump_compressed_block() -> None:
    info = vmcoreinfo(io.BytesIO(_kdump(_INFO)))
    assert info == _INFO
    require_core(info, _MANIFEST)


@pytest.mark.parametrize(
    "core",
    [b"\0" * 64, _elf(_note(b"CORE", b"x" * 8)), _kdump(b"")],
    ids=["unknown-format", "no-elf-note", "no-kdump-block"],
)
def test_vmcoreinfo_fails_without_a_block(core: bytes) -> None:
    with pytest.raises(AssertionError):
        vmcoreinfo(io.BytesIO(core))


@pytest.mark.parametrize(
    "info",
    [
        _INFO.replace(b"ab" * 20, b"cd" * 20),
        _INFO.replace(b"6.18.54-kdive", b"6.17.0"),
        _INFO + b"BUILD-ID=" + b"ab" * 20 + b"\n",
        _INFO.replace(b"OSRELEASE", b"XSRELEASE"),
    ],
    ids=["build-id", "release", "duplicate", "missing"],
)
def test_require_core_rejects_another_identity(info: bytes) -> None:
    with pytest.raises(AssertionError):
        require_core(info, _MANIFEST)


def _profile() -> dict[str, object]:
    return {
        "arch": "x86_64",
        "provider": {"local-libvirt": {"rootfs": {"kind": "catalog", "name": "img"}}},
    }


@pytest.mark.parametrize(
    ("crash", "expected"),
    [
        ("kdump", {"destructive_ops": ["force_crash"], "crashkernel": "256M"}),
        ("host_dump", {"destructive_ops": ["force_crash"], "debug": {"preserve_on_crash": True}}),
        ("target", {"destructive_ops": ["force_crash"]}),
        ("live", {}),
    ],
)
def test_crash_profile_adds_only_its_options(crash: str, expected: dict[str, object]) -> None:
    profile = _profile()
    edited = crash_profile(profile, crash)
    local = cast(dict[str, object], edited["provider"])["local-libvirt"]
    assert local == {"rootfs": {"kind": "catalog", "name": "img"}, **expected}
    assert profile == _profile(), "the lane profile was changed in place"


def test_crash_profile_refuses_unknown_options() -> None:
    with pytest.raises(AssertionError):
        crash_profile(_profile(), "fadump")


def _cells(arch: str = "x86_64") -> list[Cell]:
    return [
        c
        for c in build_contract().cells
        if c.operation in TOOLS and c.provider == "local-libvirt" and c.guest_arch == arch
    ]


def test_options_follow_each_functional_and_unsupported_cell() -> None:
    expected = {
        "control.force_crash": "kdump",
        "introspect.from_vmcore": "kdump",
        "introspect.run": "live",
        "introspect.script": "live",
        "postmortem.crash": "kdump",
        "vmcore.fetch/kdump": "kdump",
        "vmcore.fetch/host_dump": "host_dump",
        "vmcore.fetch/fadump": "live",
    }
    seen = {}
    for cell in _cells():
        if cell.kind == "rejection":
            continue
        variant = cell.scenario_id.split("/")[2]
        key = f"{cell.operation}/{variant}" if cell.operation == "vmcore.fetch" else cell.operation
        seen[key] = options(cell)
    assert seen == expected


def test_options_stop_a_ppc64le_cell_as_blocked() -> None:
    cell = next(c for c in _cells("ppc64le") if c.kind == "functional")
    with pytest.raises(ScenarioStop) as stop:
        options(cell)
    assert stop.value.outcome is Outcome.BLOCKED


def test_rejection_covers_every_boundary_of_every_tool() -> None:
    cells = [c for c in _cells() if c.kind == "rejection" and not c.id.endswith("/capability")]
    pairs = {(c.operation, boundary_of(c)) for c in cells}
    assert {tool for tool, _ in pairs} == set(TOOLS)
    for tool, boundary in pairs:
        call = rejection(tool, boundary, _TARGET)
        key = "system_id" if tool == "control.force_crash" else "run_id"
        key = "session_id" if tool.startswith("introspect.r") or tool.endswith("script") else key
        if boundary == "validation":
            assert call.args[key] == 7
        else:
            assert call.args[key] == {"system_id": "sys-1", "run_id": "run-1"}.get(key, "ses-1")
        role = call.grants.roles.get("cov-t")
        if boundary == "authentication":
            assert role == "viewer"
        if boundary == "authorization":
            # One rank below each tool's gate: admin for force_crash, viewer for from_vmcore.
            below = {"control.force_crash": "operator", "introspect.from_vmcore": None}
            assert role == below.get(tool, "viewer")
            assert call.categories == {"authorization_denied"}
        if boundary == "project-isolation":
            assert "cov-t" not in call.grants.projects
            assert set(call.grants.roles.values()) == {"admin"}
            assert call.absent_twin is not None and call.absent_twin[key] != call.args[key]
            expected = (
                "configuration_error"
                if tool in ("control.force_crash", "introspect.run", "introspect.script")
                else "not_found"
            )
            assert call.categories == {expected}


def test_vmcore_fetch_rejection_names_the_cell_method() -> None:
    assert rejection("vmcore.fetch", "authorization", _TARGET).args["method"] == "kdump"
    call = rejection("vmcore.fetch", "validation", _TARGET, method="fadump")
    assert call.args["method"] == "fadump"
    assert "method" not in rejection("postmortem.crash", "authorization", _TARGET).args


def test_force_crash_accepts_the_crash_kernel_part(tmp_path: Path) -> None:
    # The observer feeds the combined predicate, and the result names the form that matched.
    from unittest.mock import AsyncMock

    import tests.integration.live_stack.capture_cells as capture
    from tests.integration.live_stack.evidence import EvidenceWriter

    text = _console("kdump-crash-kernel-part.txt")
    old = ToolResponse.success(
        "old", "ready", refs={"object": "local/systems/system-1/console-part-1-000000"}
    )
    new = ToolResponse.success(
        "new", "ready", refs={"object": "local/systems/system-1/console-part-1-000001"}
    )
    read = ToolResponse.success(
        "new",
        "ready",
        data={"content": text, "content_truncated": False, "size_bytes": len(text.encode())},
        refs={"object": new.refs["object"]},
    )
    responses = [
        ToolResponse.collection("system-1", "ready", [old]),
        ToolResponse.collection("system-1", "ready", [old, new]),
        read,
    ]
    run = SimpleNamespace(
        writer=EvidenceWriter(tmp_path), artifacts=[], cell=SimpleNamespace(id="force-crash")
    )
    session = SimpleNamespace(
        armed=AsyncMock(),
        crash=AsyncMock(),
        guest=SimpleNamespace(
            op=SimpleNamespace(call_tool=AsyncMock(side_effect=responses)), system_id="system-1"
        ),
        run=run,
        manifest={**_MANIFEST, "release": _FIXTURE_RELEASE},
    )

    result = asyncio.run(capture.force_crash_body(cast(Session, session)))

    assert result["crash_evidence"] == "crash_kernel"
    assert result["console_part_sha256"] == hashlib.sha256(b"new").hexdigest()


def test_panic_seen_needs_the_release_on_the_panic_record() -> None:
    line = f"{PANIC}: NMI: Not continuing\nCPU: 0 UID: 0 PID: 0 Comm: swapper Not tainted "
    assert panic_seen(line + "6.18.54-kdive #1", "6.18.54-kdive")
    assert not panic_seen(line + "6.17.0 #1", "6.18.54-kdive")
    assert not panic_seen("6.18.54-kdive booted", "6.18.54-kdive")
    # The release elsewhere in the part is not the panic's.
    banner = "Fedora Linux 44 (Kernel 6.18.54-kdive on x86_64)\n"
    assert not panic_seen(banner + line + "6.17.0 #1", "6.18.54-kdive")
    assert not panic_seen(line + "6.17.0 #1\n" + banner, "6.18.54-kdive")
    assert not panic_seen(line + "6.18.54-kdive.1 #1", "6.18.54-kdive")


_CONSOLE_FIXTURES = Path(__file__).parent / "fixtures" / "console"
# The fixture kernel the live runs booted; it also loads itself as its kdump crash kernel.
_FIXTURE_RELEASE = "6.18.54-g1b357ecb3213"


def _console(name: str) -> str:
    """A sanitized excerpt of real console bytes; the serial stream ends lines in CRLF."""
    return (_CONSOLE_FIXTURES / name).read_text().replace("\n", "\r\n")


def test_crash_evidence_accepts_the_kdump_crash_kernel_boot() -> None:
    # #3254: on a kdump-armed guest the crashing kernel's panic never reaches the serial
    # console; the sealed post-crash part holds the crash kernel's banner and elfcorehdr= line.
    part = _console("kdump-crash-kernel-part.txt")

    assert not panic_seen(part, _FIXTURE_RELEASE)
    assert crash_evidence(part, _FIXTURE_RELEASE) == "crash_kernel"


def test_crash_evidence_rejects_a_normal_fixture_boot() -> None:
    # The same fixture banner on a normal boot carries no elfcorehdr= on its command line.
    assert crash_evidence(_console("fixture-boot-snapshot.txt"), _FIXTURE_RELEASE) is None


def test_crash_evidence_prefers_the_console_panic() -> None:
    panic = f"{PANIC}: sysrq triggered crash\r\nCPU: 0 PID: 1 Comm: sh {_FIXTURE_RELEASE} #1\r\n"
    part = _console("kdump-crash-kernel-part.txt")

    assert crash_evidence(panic + part, _FIXTURE_RELEASE) == "console_panic"


def test_crash_evidence_fails_closed_on_incomplete_or_foreign_records() -> None:
    part = _console("kdump-crash-kernel-part.txt")
    banner_end = part.index("\r\n", part.index("Linux version")) + 2
    record_end = part.index("\r\n", banner_end) + 2
    banner, record = part[:banner_end], part[banner_end:record_end]

    # A part sealed inside the crash kernel's command line record is not yet evidence.
    assert crash_evidence(part[: part.index("elfcorehdr=") + 20], _FIXTURE_RELEASE) is None
    # Another release, or the fixture release as a prefix of a longer one.
    assert crash_evidence(part, "6.17.0") is None
    assert (
        crash_evidence(part.replace(_FIXTURE_RELEASE, f"{_FIXTURE_RELEASE}.1"), _FIXTURE_RELEASE)
        is None
    )
    # A crash-kernel command line with no banner before it belongs to no bound boot.
    assert crash_evidence(record + banner, _FIXTURE_RELEASE) is None
    # The banner's next boot record must be the crash kernel's: a later boot does not count.
    other = banner.replace(_FIXTURE_RELEASE, "6.17.0")
    assert crash_evidence(banner + other + record, _FIXTURE_RELEASE) is None
    # kdump service text alone is not a crash-kernel boot.
    saved = "[    3.211476] kdump[408]: saving vmcore-dmesg.txt complete\r\n"
    assert crash_evidence(saved, _FIXTURE_RELEASE) is None


def test_transcript_names_whole_words_only() -> None:
    transcript = "RELEASE: 6.18.54-kdive\n  4242  1  0  kcov0a1b  IN\nffff loop 40960\n"
    assert transcript_names(transcript, "6.18.54-kdive", "kcov0a1b", "loop") == []
    assert transcript_names("RELEASE: 6.18.54-kdive.1\nloop0", "6.18.54-kdive", "loop") == [
        "6.18.54-kdive",
        "loop",
    ]


def test_script_facts_parse_the_release_and_sentinels() -> None:
    assert script_facts("release=6.18\ntask=41:kcov1\n") == ("6.18", ["41:kcov1"])
    with pytest.raises(AssertionError):
        script_facts("task=41:kcov1\n")


def test_lane_keys_drop_kernel_fields() -> None:
    observed = {
        "accelerator": "kvm",
        "guest_os": "fedora:44",
        "guest_arch": "x86_64",
        "image_sha256": "0" * 64,
        "kernel_sha256": "1" * 64,
        "kernel_build_id": "ab" * 20,
        "kernel_source_sha": "2" * 40,
        "kernel_config_sha256": "3" * 64,
        "compiler_id": "gcc",
    }
    assert lane_keys(observed) == {
        "accelerator": "kvm",
        "guest_os": "fedora:44",
        "guest_arch": "x86_64",
        "image_sha256": "0" * 64,
    }


def test_missing_dependency_blocks() -> None:
    missing = ToolResponse.failure(
        "r", ErrorCategory.MISSING_DEPENDENCY, detail="crash(8) is not installed"
    )
    with pytest.raises(ScenarioStop) as stop:
        require_tool(missing, "crash")
    assert stop.value.outcome is Outcome.BLOCKED
    with pytest.raises(AssertionError):
        require_tool(ToolResponse.failure("r", ErrorCategory.NOT_FOUND, detail="x"), "crash")
    done = ToolResponse.success("r", "ok")
    assert require_tool(done, "crash") is done


class _Caller:
    def __init__(self, answer: ToolResponse) -> None:
        self.answer = answer

    def token(self, grants: Grants) -> str:
        return "t"

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse:
        return self.answer


def _session(answer: ToolResponse) -> Session:
    run = cast(CellRun, SimpleNamespace(cell=SimpleNamespace(operation="postmortem.crash")))
    caller = cast(HttpCaller, _Caller(answer))
    role = Grants("s", ("p",))
    return Session(run, cast(Guest, None), caller, role, _MANIFEST, "run-1", Endpoint("h", 22))


def test_exposed_call_missing_a_dependency_is_blocked() -> None:
    missing = ToolResponse.failure("r", ErrorCategory.MISSING_DEPENDENCY, detail="no crash(8)")
    with pytest.raises(ScenarioStop) as stop:
        asyncio.run(_session(missing).exposed(run_id="run-1"))
    assert stop.value.outcome is Outcome.BLOCKED
    other = ToolResponse.failure("r", ErrorCategory.NOT_FOUND, detail="x")
    with pytest.raises(AssertionError):
        asyncio.run(_session(other).exposed(run_id="run-1"))


def test_kdump_armed_reads_the_crash_kernel_and_the_service() -> None:
    assert kdump_armed({"loaded": "1", "kdump": "active"})
    assert not kdump_armed({"loaded": "0", "kdump": "activating"})


def test_kdump_service_failure_blocks_instead_of_waiting() -> None:
    # A guest kernel missing a kdump prerequisite fails kdump.service; waiting cannot help.
    with pytest.raises(ScenarioStop) as stop:
        kdump_armed({"loaded": "0", "kdump": "failed"})
    assert stop.value.outcome is Outcome.BLOCKED


@pytest.mark.parametrize("failure_stage", [None, "crash", "read"])
@pytest.mark.parametrize("writer_fails", [False, True])
def test_force_crash_preserves_evidence_and_original_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str | None,
    writer_fails: bool,
) -> None:
    from unittest.mock import AsyncMock

    import tests.integration.live_stack.capture_cells as capture
    from tests.integration.live_stack.evidence import EvidenceWriter
    from tests.integration.live_stack.spine import SpinePhaseError

    events: list[str] = []
    original = SpinePhaseError("crash-proof", "original failure")
    part = ToolResponse.success(
        "old", "ready", refs={"object": "local/systems/system-1/console-part-1-000008"}
    )
    new = ToolResponse.success(
        "new", "ready", refs={"object": "local/systems/system-1/console-part-1-000009"}
    )
    text = PANIC + "\nCPU: 0 PID: 1 6.18.54-kdive #1\n"
    responses: list[object] = [ToolResponse.collection("system-1", "ready", [part])]
    if failure_stage != "crash":
        responses += [ToolResponse.collection("system-1", "ready", [new])]
        responses += [
            original
            if failure_stage == "read"
            else ToolResponse.success(
                "new",
                "ready",
                data={
                    "content": text,
                    "content_truncated": False,
                    "size_bytes": len(text.encode()),
                },
                refs={
                    "object": new.refs["object"],
                    "download_uri": "https://example.invalid/private",
                },
            )
        ]
    writer = EvidenceWriter(tmp_path)
    real_artifact = writer.artifact

    def artifact(payload: object) -> str:
        events.append("persist")
        if writer_fails:
            raise OSError("private path omitted from note")
        return real_artifact(payload)

    monkeypatch.setattr(writer, "artifact", artifact)
    run = SimpleNamespace(writer=writer, artifacts=[], cell=SimpleNamespace(id="force-crash"))
    session = SimpleNamespace(
        armed=AsyncMock(),
        crash=AsyncMock(side_effect=original if failure_stage == "crash" else None),
        guest=SimpleNamespace(
            op=SimpleNamespace(call_tool=AsyncMock(side_effect=responses)),
            system_id="system-1",
            key="do-not-copy-private-key",
        ),
        run=run,
        manifest=_MANIFEST,
    )

    async def invoke() -> dict[str, object]:
        try:
            return await capture.force_crash_body(cast(Session, session))
        finally:
            events.append("cleanup")

    if failure_stage is not None:
        with pytest.raises(SpinePhaseError) as failure:
            asyncio.run(invoke())
        assert failure.value is original
        if writer_fails:
            assert original.__notes__ == ["console diagnostic persistence failed: OSError"]
    elif writer_fails:
        with pytest.raises(OSError):
            asyncio.run(invoke())
    else:
        assert asyncio.run(invoke())["crash_evidence"] == "console_panic"
    assert events == ["persist", "cleanup"]
    if not writer_fails:
        payload = json.loads((tmp_path / "artifacts" / run.artifacts[0]).read_text())
        assert payload["complete"] is (failure_stage is None)
        assert payload["console_observation"][0]["tool"] == "artifacts.list"
        if failure_stage is None:
            content = payload["console_observation"][-1]
            assert content["data"]["content"] == text
            assert content["content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert "do-not-copy-private-key" not in json.dumps(payload)
        assert "download_uri" not in json.dumps(payload)
