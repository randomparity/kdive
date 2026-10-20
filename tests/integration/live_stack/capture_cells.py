"""Crash capture and introspection tool cells over HTTP on both providers (#2814, #3114).

A functional cell provisions the lane image with its tool's crash options through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, boots the verified ``longterm``
kernel fixture (:func:`~tests.integration.live_stack.deep_lifecycle.deep_body`) and starts a
sentinel process whose command name is a known workload fact. It then crashes and captures, or
opens a drgn-live session, as its tool needs. Only the tool under test goes through the cell's
exposure; the supporting calls use the frame's operator client, or an admin client of the same
project for ``control.force_crash``. A captured core is read back through ``artifacts.fetch_raw``
and identified by its VMCOREINFO block alone.

A rejection cell aims at the stack's :func:`capture_target`: a torn-down fixture System, its Run
and an ended drgn-live session. A ``capability`` cell (the x86_64 ``fadump`` refusal, ADR-0722
2026-10-13) crashes its own Run-bound System. Neither records a kernel field, since neither
declares kernel inputs.

A remote cell runs the same bodies on the remote lane (ADR-0722): its profile edits the
``remote-libvirt`` section, its rejection target is the remote one of the stack and keeps the
provider host's identity, and a ``host_dump`` capture owns its pool dump volume. Its
``console``/``gdbstub`` ``vmcore.fetch`` cells prove the handler's refusal (2026-10-20
amendment). Its live drgn cells stay blocked on #3124 (:data:`REMOTE_DEBUGINFO_BLOCKED`).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import re
import secrets
import struct
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import httpx
import psycopg

from kdive.domain.errors import ErrorCategory
from kdive.domain.platform.arch_traits import arch_traits
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import host_dump_volume_name
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.deep_lifecycle import deep_body
from tests.integration.live_stack.image_smoke import Endpoint, parse_probe, ssh
from tests.integration.live_stack.power_snapshots import domain_defined, grants
from tests.integration.live_stack.scenario import (
    CellRun,
    Provision,
    ScenarioStop,
    on_catalog_system,
    provision_catalog,
    ssh_endpoint,
)
from tests.integration.live_stack.spine import (
    ConsoleObservation,
    SpinePhaseError,
    await_system_state,
    build_profile,
    captured_vmcore_refs,
    drain_job,
    mint_role_token,
    ok,
    poll_for_new_console_part,
    raw_vmcore_refs,
    scalar,
)
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Connect,
    Grants,
    Guest,
    HttpCaller,
    Rejection,
    Snapshot,
    lane_image,
    observe_guest,
    on_lane_system,
    one,
    project_state,
    prove_rejection,
    settled,
    worker_connect,
)

TOOLS = (
    "control.force_crash",
    "introspect.from_vmcore",
    "introspect.run",
    "introspect.script",
    "postmortem.crash",
    "vmcore.fetch",
)
# Each tool's project gate (src/kdive/mcp/exposure.py).
GATES = {tool: "contributor" for tool in TOOLS} | {
    "control.force_crash": "admin",
    "introspect.from_vmcore": "viewer",
}
_BELOW: dict[str, str | None] = {"admin": "operator", "contributor": "viewer", "viewer": None}
# A foreign or absent id: force_crash and the session tools answer configuration_error, the Run
# tools not_found.
_ISOLATION = {
    tool: ErrorCategory.CONFIGURATION_ERROR
    if tool in ("control.force_crash", "introspect.run", "introspect.script")
    else ErrorCategory.NOT_FOUND
    for tool in TOOLS
}
_ID = {tool: "run_id" for tool in TOOLS} | {
    "control.force_crash": "system_id",
    "introspect.run": "session_id",
    "introspect.script": "session_id",
}
_EXTRA: dict[str, dict[str, object]] = {
    "introspect.run": {"helper": "sysinfo"},
    "introspect.script": {"script": "print(1)"},
}
# Crash options per functional cell: kdump arms the guest's crash kernel; host_dump preserves the
# crashed domain for a memory dump (no crash kernel, which would power it off); target lets only
# the admin role deny force_crash on the rejection target; live adds nothing.
_CRASH_OPTIONS = frozenset({"kdump", "host_dump", "target", "live"})
_LANE_KEYS = ("guest_os", "guest_arch", "accelerator", "image_sha256")
_HOST_KEYS = ("host_os", "host_arch")
# vmcore.fetch's refusal of a method outside its core-producing set (handlers.py).
REFUSED_REASON = "method does not produce a vmcore"
# Remote drgn-live runs in the guest, which never receives the Run's vmlinux: these cells stay
# blocked on the open defect, not failed (#3124, blocked by #3121). Drop an entry when its issue
# closes.
REMOTE_DEBUGINFO_BLOCKED = {
    tool: "remote guests do not receive the uploaded vmlinux (#3124)"
    for tool in ("introspect.run", "introspect.script")
}
PANIC = "Kernel panic - not syncing"
# The panic's dump_stack ``CPU:`` record follows it within a few lines.
_PANIC_LINES = 8
# kexec hands a crash-dump kernel its ELF core header address on the command line. Only x86's
# setup_arch prints a `Command line:` record; other arches would need `Kernel command line:`.
_CRASH_KERNEL_CMDLINE = re.compile(r"Command line:(?:.*\s)?elfcorehdr=\S")
# VMCOREINFO sits right after a core's headers: QEMU's ELF note, makedumpfile's sub-header block.
_CORE_PREFIX = 16 << 20
_ARM_BUDGET_S = 600.0
_CONSOLE_DEADLINE_S = 600.0
_DOWNLOAD_TIMEOUT_S = 600.0
_KDUMP_SIGNATURE = b"KDUMP   "
_PT_NOTE = 4
_PN_XNUM = 0xFFFF
SCRIPT = """\
from drgn.helpers.linux.pid import for_each_task

print("release=" + prog["init_uts_ns"].name.release.string_().decode())
for task in for_each_task(prog):
    comm = task.comm.string_().decode()
    if comm.startswith("kcov"):
        print(f"task={task.pid.value_()}:{comm}")
"""


def variant_of(cell: Cell) -> str:
    """The tool variant of ``cell``, read from a local or a provider-qualified scenario id."""
    return cell.scenario_id.split("/")[-2]


def options(cell: Cell) -> str:
    """The crash options a functional or unsupported cell provisions with."""
    if cell.guest_arch != "x86_64":
        raise ScenarioStop(Outcome.BLOCKED, f"{cell.id} is the POWER lane's (#2818)")
    if cell.provider == "remote-libvirt" and cell.operation in REMOTE_DEBUGINFO_BLOCKED:
        raise ScenarioStop(Outcome.BLOCKED, REMOTE_DEBUGINFO_BLOCKED[cell.operation])
    variant = variant_of(cell)
    if cell.operation == "vmcore.fetch" and variant in ("kdump", "host_dump"):
        return variant
    if cell.operation == "vmcore.fetch" and variant in ("console", "gdbstub"):
        return "host_dump"  # a crashed System the handler refuses to fetch from
    if cell.operation in ("introspect.run", "introspect.script") or variant == "fadump":
        return "live"
    return "kdump"


def crash_profile(profile: Mapping[str, object], crash: str) -> dict[str, object]:
    """``profile`` with ``crash`` options in its provider section; the input is unchanged."""
    assert crash in _CRASH_OPTIONS, f"unknown crash options {crash!r}"
    edited = copy.deepcopy(dict(profile))
    sections = cast(dict[str, Any], edited["provider"])
    assert len(sections) == 1, f"the profile names {len(sections)} provider sections"
    ((provider, section),) = sections.items()
    if crash != "live":
        section["destructive_ops"] = ["force_crash"]
    if crash == "kdump":
        section["crashkernel"] = arch_traits(str(edited["arch"])).default_crashkernel
    if crash == "host_dump" and provider == "remote-libvirt":
        section["host_dump"] = True
    elif crash == "host_dump":
        section["debug"] = {"preserve_on_crash": True}
    return edited


def provision_with(crash: str) -> Provision:
    """The lane provision, with ``crash`` options added to its profile."""

    async def provision(op: LiveStackClient, allocation: str, profile: dict[str, object]) -> str:
        return await provision_catalog(op, allocation, crash_profile(profile, crash))

    return provision


def lane_keys(observed: Mapping[str, object], *, remote: bool = False) -> dict[str, object]:
    """The lane context of ``observed``: no kernel field, which no rejection cell declares.

    On the remote lane it keeps the provider host's keys, which the frame observed there.
    """
    keys = (*_LANE_KEYS, *_HOST_KEYS) if remote else _LANE_KEYS
    return {key: observed[key] for key in keys if key in observed}


def require_tool(env: ToolResponse, phase: str) -> ToolResponse:
    """``env`` when it succeeded; ``blocked`` when the stack lacks a dependency."""
    if env.error_category == ErrorCategory.MISSING_DEPENDENCY.value:
        raise ScenarioStop(Outcome.BLOCKED, f"{phase}: the stack lacks a dependency")
    return ok(env, phase)


async def drained(op: LiveStackClient, phase: str, job_id: str) -> ToolResponse:
    """The job's terminal success; ``blocked`` when it failed for a missing dependency."""
    try:
        return await drain_job(op, phase, job_id)
    except SpinePhaseError as exc:
        if exc.error_category == ErrorCategory.MISSING_DEPENDENCY.value:
            raise ScenarioStop(Outcome.BLOCKED, f"{phase}: the stack lacks a dependency") from None
        raise


def kdump_armed(probe: Mapping[str, str]) -> bool:
    """Whether the guest's crash kernel is loaded; ``blocked`` once its kdump service failed.

    A failed service means the guest cannot build its kdump initramfs for the booted kernel, a
    missing prerequisite that no wait resolves.
    """
    if probe.get("kdump") == "failed":
        raise ScenarioStop(
            Outcome.BLOCKED, "the guest's kdump.service failed to arm the fixture kernel"
        )
    return probe.get("loaded") == "1"


def panic_seen(text: str, release: str) -> bool:
    """A console panic whose own ``CPU:`` record, among the next lines, names ``release``."""
    _, found, after = text.partition(PANIC)
    record = re.compile(rf"\bCPU: \d+ .*(?<![\w.-]){re.escape(release)}(?![\w.-])")
    return bool(found) and any(record.search(line) for line in after.splitlines()[:_PANIC_LINES])


def crash_evidence(text: str, release: str) -> str | None:
    """Which accepted form of a force crash ``text`` shows, or ``None`` (#3254).

    ``console_panic`` is :func:`panic_seen`. ``crash_kernel`` covers kdump-armed guests, whose
    crashing kernel enters the crash kernel before flushing its panic to the serial console:
    a ``Linux version <release>`` boot banner whose first newline-terminated ``Command line:``
    record carries ``elfcorehdr=``, which only kexec into a crash-dump kernel passes. A normal
    boot of the same kernel lacks it, and a record belonging to a later banner does not count.
    """
    if panic_seen(text, release):
        return "console_panic"
    banner = re.compile(rf"\[\s*0\.000000\] Linux version {re.escape(release)}(?![\w.-])")
    for found in banner.finditer(text):
        record = text.find("Command line:", found.end())
        later_banner = text.find("Linux version", found.end())
        if record < 0 or 0 <= later_banner < record:
            continue
        end = text.find("\n", record)
        if end >= 0 and _CRASH_KERNEL_CMDLINE.search(text, record, end):
            return "crash_kernel"
    return None


def transcript_names(transcript: str, *facts: str) -> list[str]:
    """The ``facts`` a crash(8) transcript does not name as a whole word."""
    return [f for f in facts if not re.search(rf"(?<![\w.-]){re.escape(f)}(?![\w.-])", transcript)]


def section(report: object, key: str) -> dict[str, Any]:
    """The ``key`` section of an introspection report."""
    value = cast(dict[str, Any], report).get(key) if isinstance(report, dict) else None
    assert isinstance(value, dict), f"no {key} section"
    return cast(dict[str, Any], value)


def module_names(modules: Mapping[str, Any]) -> set[str]:
    """The names in an introspection ``modules`` section."""
    listed = modules.get("modules")
    assert isinstance(listed, list), "the modules section lists no modules"
    return {str(m.get("name")) for m in listed if isinstance(m, dict)}


def script_facts(output: str) -> tuple[str, list[str]]:
    """The release and ``pid:comm`` sentinel tasks :data:`SCRIPT` printed."""
    lines = output.splitlines()
    releases = [line.removeprefix("release=") for line in lines if line.startswith("release=")]
    assert len(releases) == 1, f"the script printed {len(releases)} release lines"
    return releases[0], [line.removeprefix("task=") for line in lines if line.startswith("task=")]


# --- the captured core's identity -----------------------------------------------------------


def _elf_vmcoreinfo(core: BinaryIO) -> bytes | None:
    header = core.read(64)
    assert len(header) == 64 and header[4] == 2 and header[5] == 1, "not a 64-bit LE ELF core"
    phoff, shoff = struct.unpack_from("<QQ", header, 0x20)
    phentsize, phnum = struct.unpack_from("<HH", header, 0x36)
    if phnum == _PN_XNUM:  # the real count is section 0's sh_info
        core.seek(shoff + 44)
        (phnum,) = struct.unpack("<I", core.read(4))
    for index in range(phnum):
        core.seek(phoff + index * phentsize)
        entry = core.read(56)
        p_type, _flags, p_offset = struct.unpack_from("<IIQ", entry, 0)
        (p_filesz,) = struct.unpack_from("<Q", entry, 32)
        if p_type != _PT_NOTE:
            continue
        core.seek(p_offset)
        notes = core.read(p_filesz)
        at = 0
        while at + 12 <= len(notes):
            namesz, descsz, _kind = struct.unpack_from("<III", notes, at)
            name_at = at + 12
            desc_at = name_at + (namesz + 3) // 4 * 4
            at = desc_at + (descsz + 3) // 4 * 4
            if notes[name_at : name_at + namesz].rstrip(b"\0") == b"VMCOREINFO":
                return notes[desc_at : desc_at + descsz]
    return None


def _kdump_vmcoreinfo(core: BinaryIO) -> bytes | None:
    core.seek(428)
    (block_size,) = struct.unpack("<i", core.read(4))
    core.seek(block_size + 32)  # kdump_sub_header.offset_vmcoreinfo, then size_vmcoreinfo
    offset, size = struct.unpack("<qQ", core.read(16))
    if not offset or not size:
        return None
    core.seek(offset)
    return core.read(size)


def vmcoreinfo(core: BinaryIO) -> bytes:
    """The core's VMCOREINFO block: the ELF note or the kdump-compressed sub-header block."""
    magic = core.read(8)
    core.seek(0)
    if magic.startswith(b"\x7fELF"):
        info = _elf_vmcoreinfo(core)
    elif magic == _KDUMP_SIGNATURE:
        info = _kdump_vmcoreinfo(core)
    else:
        raise AssertionError("the core is neither an ELF nor a kdump-compressed dump")
    assert info, "the core carries no VMCOREINFO block"
    return info


def require_core(info: bytes, manifest: Mapping[str, Any]) -> None:
    """VMCOREINFO names the fixture: exactly one ``BUILD-ID`` and one ``OSRELEASE`` line."""
    lines = info.decode("ascii", "replace").splitlines()
    builds = [line.removeprefix("BUILD-ID=") for line in lines if line.startswith("BUILD-ID=")]
    releases = [line.removeprefix("OSRELEASE=") for line in lines if line.startswith("OSRELEASE=")]
    assert builds == [manifest["build_id"]], "the core's build ID is not the fixture's"
    assert releases == [manifest["release"]], "the core's release is not the fixture's"


async def _download(op: LiveStackClient, run_id: str) -> tuple[str, bytes]:
    """One fresh ``artifacts.fetch_raw`` read of the core: its SHA-256 and its first bytes."""
    env = ok(await scalar(op, "artifacts.fetch_raw", run_id=run_id, asset="vmcore"), "raw")
    url = env.refs.get("download_uri")
    assert url, "artifacts.fetch_raw returned no download URI"
    digest, size, prefix = hashlib.sha256(), 0, bytearray()
    async with (
        httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_S) as http,
        http.stream("GET", url) as response,
    ):
        response.raise_for_status()
        async for chunk in response.aiter_bytes(1 << 20):
            digest.update(chunk)
            size += len(chunk)
            if len(prefix) < _CORE_PREFIX:
                prefix += chunk[: _CORE_PREFIX - len(prefix)]
    assert size == env.data.get("size_bytes"), "the core's size is not the advertised size"
    return digest.hexdigest(), bytes(prefix)


async def raw_core(op: LiveStackClient, run_id: str) -> tuple[str, bytes]:
    """Two independent reads of the raw core with equal bytes, and its VMCOREINFO.

    Nothing is written to disk: the reads are hashed as they stream, and VMCOREINFO is read
    from the first read's leading bytes.
    """
    first, prefix = await _download(op, run_id)
    second, _ = await _download(op, run_id)
    assert first == second, "two reads of the stored core returned different bytes"
    return first, vmcoreinfo(io.BytesIO(prefix))


# --- functional cells -----------------------------------------------------------------------

_SENTINEL = (
    "cp /usr/bin/sleep /root/{name} && "
    "(setsid /root/{name} 3600 </dev/null >/dev/null 2>&1 & echo pid=$!)"
)
_ARM_PROBE = (
    'printf "loaded=%s\\nkdump=%s\\n" "$(cat /sys/kernel/kexec_crash_loaded)" '
    '"$(systemctl is-active kdump || true)"'
)
_FACTS = 'printf "release=%s\\nnodename=%s\\n" "$(uname -r)" "$(uname -n)"'


@dataclass
class Session:
    """A functional cell's booted fixture System and who issues each call."""

    run: CellRun
    guest: Guest
    caller: HttpCaller
    grants: Grants
    manifest: Mapping[str, Any]
    run_id: str
    endpoint: Endpoint

    async def exposed(self, **args: object) -> ToolResponse:
        """The tool under test, through the cell's exposure and token."""
        tool = self.run.cell.operation
        # Minted per call: a functional sequence can outlast a token's lifetime.
        token = self.caller.token(self.grants)
        result = await self.caller.call(tool, args, token, discover=True)
        # Classified before any success assertion, so a missing dependency stays ``blocked``.
        return require_tool(result, tool) if isinstance(result, ToolResponse) else one(result)

    async def shell(self, command: str) -> dict[str, str]:
        result = await asyncio.to_thread(ssh, self.endpoint, self.guest.key, command)
        assert result.returncode == 0, f"guest command exited {result.returncode}"
        return parse_probe(result.stdout)

    async def sentinel(self) -> tuple[str, str]:
        """Start ``kcov<hex>``; its command name and pid are the controlled workload."""
        name = f"kcov{secrets.token_hex(4)}"
        pid = (await self.shell(_SENTINEL.format(name=name)))["pid"]
        assert pid.isdigit(), "the sentinel reported no pid"
        return name, pid

    async def facts(self) -> dict[str, str]:
        facts = await self.shell(_FACTS)
        assert facts["release"] == self.manifest["release"], "the guest runs another kernel"
        return facts

    async def armed(self) -> None:
        """Wait until the guest's kdump service has loaded the crash kernel."""
        deadline = time.monotonic() + _ARM_BUDGET_S
        while not kdump_armed(await self.shell(_ARM_PROBE)):
            assert time.monotonic() < deadline, f"kdump not armed within {_ARM_BUDGET_S:.0f} s"
            await asyncio.sleep(5.0)

    async def crash(self, *, exposed: bool = False) -> None:
        """Crash the System and wait for the job's success and the durable ``crashed`` state."""
        project, op = self.guest.project, self.guest.op
        if exposed:
            env = await self.exposed(system_id=self.guest.system_id)
            await drained(op, "crash", env.object_id)
        else:
            token = mint_role_token(
                self.caller.issuer, project=project, agent_session=f"{project}-sess", role="admin"
            )
            async with LiveStackClient.over_http(self.caller.base_url, token) as admin:
                crash = await scalar(admin, "control.force_crash", system_id=self.guest.system_id)
                env = ok(crash, "crash")
                await drained(admin, "crash", env.object_id)
        await await_system_state(op, "crash", self.guest.system_id, "crashed")

    async def capture(self, method: str, *, exposed: bool = False) -> list[str]:
        """Capture with ``method``; the job, Run and artifact refs agree and expose no raw core."""
        args = {"run_id": self.run_id, "method": method}
        if exposed:
            env = await self.exposed(**args)
        else:
            env = require_tool(await scalar(self.guest.op, "vmcore.fetch", **args), "capture")
        job = await drained(self.guest.op, "capture", env.object_id)
        refs = await captured_vmcore_refs(self.guest.op, "capture", job, run_id=self.run_id)
        assert not raw_vmcore_refs(refs), "a capture ref exposes the raw core"
        return refs

    async def drgn_session(self) -> str:
        env = require_tool(
            await scalar(
                self.guest.op, "debug.start_session", run_id=self.run_id, transport="drgn-live"
            ),
            "attach",
        )
        assert "missing_debuginfo" not in env.data, "drgn-live attached without debuginfo"
        return env.object_id

    async def end(self, session_id: str) -> None:
        env = ok(await scalar(self.guest.op, "debug.end_session", session_id=session_id), "end")
        assert env.status == "detached", f"the session ended {env.status}"


async def force_crash_body(s: Session) -> dict[str, object]:
    """The exposed crash: job success, durable ``crashed``, and crash evidence on the console."""
    await s.armed()
    observation = ConsoleObservation()
    failure: BaseException | None = None
    try:
        before = await observation.snapshot(s.guest.op, s.guest.system_id)
        await s.crash(exposed=True)
        part, text = await poll_for_new_console_part(
            s.guest.op,
            s.guest.system_id,
            before,
            PANIC,
            deadline_s=_CONSOLE_DEADLINE_S,
            interval_s=10,
            predicate=lambda value: crash_evidence(value, s.manifest["release"]) is not None,
            observation=observation,
        )
        form = crash_evidence(text, s.manifest["release"])
        assert form is not None, (
            "the console shows neither the fixture kernel's panic nor its crash kernel"
        )
        return {
            "system": "crashed",
            "crash_evidence": form,
            "console_part_sha256": hashlib.sha256(part.encode()).hexdigest(),
        }
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            s.run.artifacts.append(
                s.run.writer.artifact(
                    {
                        "cell": s.run.cell.id,
                        "console_observation": observation.records,
                        "complete": failure is None,
                        "incomplete": observation.incomplete,
                        "failure_class": type(failure).__name__ if failure is not None else None,
                    }
                )
            )
        except Exception as diagnostic_error:
            if failure is None:
                raise
            failure.add_note(
                f"console diagnostic persistence failed: {type(diagnostic_error).__name__}"
            )


async def vmcore_body(s: Session) -> dict[str, object]:
    """Crash, capture through the exposure, then read the stored core back twice."""
    method = variant_of(s.run.cell)
    if method == "kdump":
        await s.armed()
    await s.crash()
    path = None
    if method == "host_dump" and s.guest.lane.pool_volume is not None:
        # Owned before the capture: a failed capture can leave the deterministic volume.
        path = s.guest.lane.pool_volume(host_dump_volume_name(UUID(s.guest.system_id)))
        s.guest.owned.append(path)
    refs = await s.capture(method, exposed=True)
    dump: dict[str, object] = {}
    if path is not None:
        # The remote capture reclaims its per-System dump volume in its own finally.
        assert await asyncio.to_thread(s.guest.lane.absent, path), (
            "the host_dump volume outlived its capture"
        )
        dump = {"dump_volume": "absent"}
    sha, info = await raw_core(s.guest.op, s.run_id)
    require_core(info, s.manifest)
    return {
        **dump,
        "method": method,
        "job": "succeeded",
        "refs_agree": True,
        "raw_refs_exposed": 0,
        "artifact_id_sha256": hashlib.sha256(refs[0].encode()).hexdigest(),
        "core_sha256": sha,
        "size_matches": True,
        "reads_identical": True,
        "build_id": s.manifest["build_id"],
        "release": s.manifest["release"],
    }


async def _captured(s: Session) -> tuple[dict[str, str], str]:
    """The guest facts and sentinel, then a kdump crash and capture by the operator."""
    facts = await s.facts()
    name, _pid = await s.sentinel()
    await s.armed()
    await s.crash()
    await s.capture("kdump")
    return facts, name


async def from_vmcore_body(s: Session) -> dict[str, object]:
    """Offline drgn names the fixture release, the guest's node and the loaded ``loop``."""
    facts, _name = await _captured(s)
    report = (await s.exposed(run_id=s.run_id)).data.get("report")
    sysinfo = section(report, "sysinfo")
    assert sysinfo["release"] == s.manifest["release"], "offline release is not the fixture's"
    assert sysinfo["machine"] == "x86_64", f"offline machine {sysinfo['machine']!r}"
    assert sysinfo["nodename"] == facts["nodename"], "offline nodename is not the guest's"
    assert "loop" in module_names(section(report, "modules")), "offline modules omit loop"
    return {"release": sysinfo["release"], "nodename": True, "module": "loop", "build_id": True}


async def postmortem_body(s: Session) -> dict[str, object]:
    """crash(8) over the core names the fixture release, the sentinel and ``loop``."""
    _facts, name = await _captured(s)
    env = await s.exposed(run_id=s.run_id, commands=["sys", "ps", "mod"])
    transcript = env.data.get("transcript")
    assert isinstance(transcript, str), "postmortem.crash returned no transcript"
    missing = transcript_names(transcript, s.manifest["release"], name, "loop")
    assert not missing, f"the transcript does not name {len(missing)} expected fact(s)"
    return {"release": s.manifest["release"], "sentinel": True, "module": "loop"}


async def run_body(s: Session) -> dict[str, object]:
    """Live drgn helpers match the running guest's release, node and ``loop``."""
    facts = await s.facts()
    session = await s.drgn_session()
    try:
        reports = {}
        for helper in ("sysinfo", "modules"):
            env = await s.exposed(session_id=session, helper=helper)
            assert "missing_debuginfo" not in env.data, f"{helper} ran without debuginfo"
            reports[helper] = section(env.data.get("report"), helper)
    finally:
        await s.end(session)
    sysinfo = reports["sysinfo"]
    assert (sysinfo["release"], sysinfo["nodename"]) == (facts["release"], facts["nodename"])
    assert "loop" in module_names(reports["modules"]), "the live modules omit loop"
    return {"helpers": ["sysinfo", "modules"], "release": facts["release"], "session": "detached"}


async def script_body(s: Session) -> dict[str, object]:
    """A drgn script finds exactly the sentinel by pid and name in the live kernel."""
    await s.facts()
    name, pid = await s.sentinel()
    session = await s.drgn_session()
    try:
        env = await s.exposed(session_id=session, script=SCRIPT)
    finally:
        await s.end(session)
    output = env.data.get("output")
    assert isinstance(output, str) and env.data.get("truncated") is False, "no whole output"
    release, tasks = script_facts(output)
    assert release == s.manifest["release"], "the live release is not the fixture's"
    assert tasks == [f"{pid}:{name}"], f"the script found {len(tasks)} sentinel task(s)"
    return {"release": release, "sentinel": True, "session": "detached"}


BODIES: dict[str, Callable[[Session], Awaitable[dict[str, object]]]] = {
    "control.force_crash": force_crash_body,
    "introspect.from_vmcore": from_vmcore_body,
    "introspect.run": run_body,
    "introspect.script": script_body,
    "postmortem.crash": postmortem_body,
    "vmcore.fetch": vmcore_body,
}


def refusal_of(env: ToolResponse, method: str) -> dict[str, object]:
    """``vmcore.fetch``'s refusal of a non-core ``method``, with no job (ADR-0722)."""
    refused = {
        "error_category": env.error_category,
        "reason": env.data.get("reason"),
        "method": env.data.get("method"),
    }
    expected = {
        "error_category": ErrorCategory.CONFIGURATION_ERROR.value,
        "reason": REFUSED_REASON,
        "method": method,
    }
    assert refused == expected, f"vmcore.fetch({method}) was not refused: {refused}"
    assert not env.refs, f"the refused vmcore.fetch({method}) returned refs"
    return refused


async def boot(
    run: CellRun, guest: Guest, tree: Path, manifest: Mapping[str, Any]
) -> tuple[str, Endpoint]:
    """Boot the fixture kernel on ``guest``; its proofs become ``run``'s artifacts."""
    deep = CellRun(run.cell, run.writer, observed=run.observed)
    tmp = guest.scratch / "deep"
    tmp.mkdir()
    run_id = await deep_body(
        deep,
        guest.op,
        guest.system_id,
        guest.owned,
        project=guest.project,
        entry=guest.lane.entry,
        tree=tree,
        manifest=dict(manifest),
        tmp=tmp,
        installed_kernel=guest.lane.installed_kernel,
    )
    run.artifacts.extend(deep.assertions.values())
    return run_id, await ssh_endpoint(guest.op, guest.system_id)


async def functional(
    run: CellRun,
    caller: HttpCaller,
    tree: Path,
    manifest: Mapping[str, Any],
    guest: Guest,
) -> dict[str, object]:
    """Boot the fixture kernel, then run the tool's sequence as the tool's gate role."""
    run_id, endpoint = await boot(run, guest, tree, manifest)
    role = grants(guest.project, GATES[run.cell.operation])
    session = Session(run, guest, caller, role, manifest, run_id, endpoint)
    observed = await BODIES[run.cell.operation](session)
    return {"tool": run.cell.operation, **observed}


async def unsupported(
    run: CellRun, caller: HttpCaller, tree: Path, manifest: Mapping[str, Any], guest: Guest
) -> dict[str, object]:
    """The fixture System's advertisement omits fadump and lists the x86_64 methods (ADR-0757).

    ``systems.get`` goes through the cell's exposure, so a gateway cell reads it through
    ``tools.invoke``.
    """
    await boot(run, guest, tree, manifest)
    token = caller.token(grants(guest.project, "viewer"))
    answer = await caller.call("systems.get", {"system_id": guest.system_id}, token, discover=True)
    env = one(answer)
    methods = env.data.get("supported_capture_methods")
    assert isinstance(methods, list), "systems.get does not report supported_capture_methods"
    assert "fadump" not in methods, "the x86_64 System advertises fadump"
    assert {"kdump", "host_dump"} <= set(methods), f"the System advertises only {methods}"
    return {"tool": run.cell.operation, "advertised": sorted(map(str, methods))}


async def refused_method(
    run: CellRun,
    caller: HttpCaller,
    tree: Path,
    manifest: Mapping[str, Any],
    db_url: str,
    guest: Guest,
) -> dict[str, object]:
    """A crashed fixture System's Run refuses a non-core capture method, writing no job.

    The handler checks the Run, role, binding and CRASHED state before the method, so the
    System is booted and crashed first (ADR-0722, 2026-10-20). The fetch goes through the
    cell's exposure as a contributor.
    """
    method = variant_of(run.cell)
    run_id, endpoint = await boot(run, guest, tree, manifest)
    role = grants(guest.project, GATES["vmcore.fetch"])
    await Session(run, guest, caller, role, manifest, run_id, endpoint).crash()
    jobs = partial(named_jobs, db_url, guest.system_id, run_id)
    before = await jobs()
    args = {"run_id": run_id, "method": method}
    answer = await caller.call("vmcore.fetch", args, caller.token(role), discover=True)
    refused = refusal_of(one(answer), method)
    assert await jobs() == before, "the refused fetch changed the jobs of its System or Run"
    await await_system_state(guest.op, "refused", guest.system_id, "crashed")
    return {"tool": run.cell.operation, **refused, "jobs_unchanged": True, "system": "crashed"}


# --- rejection cells ------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureTarget:
    """A torn-down fixture System, its Run and an ended drgn-live session.

    ``connect`` opens the provider libvirt its domain is read through.
    """

    project: str
    system_id: str
    run_id: str
    session_id: str
    observed: dict[str, object]
    artifacts: tuple[str, ...]
    connect: Connect = worker_connect

    def id_for(self, tool: str) -> str:
        key = _ID[tool]
        return {"system_id": self.system_id, "run_id": self.run_id}.get(key, self.session_id)


@dataclass(frozen=True)
class _Failed:
    error: Exception
    artifacts: tuple[str, ...]
    observed: dict[str, object]


_TARGETS: dict[tuple[str, str], CaptureTarget | _Failed] = {}


async def _prepare_target(
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
    connects: list[Connect] = []

    async def body(guest: Guest) -> dict[str, object]:
        run_id, endpoint = await boot(scratch, guest, tree, manifest)
        caller = HttpCaller("direct", base_url, issuer)
        s = Session(scratch, guest, caller, grants(project, "viewer"), manifest, run_id, endpoint)
        session = await s.drgn_session()
        await s.end(session)
        ids.update(system=guest.system_id, run=run_id, session=session)
        connects.append(guest.lane.connect)
        return {"capture_target": "prepared"}

    await on_lane_system(
        scratch,
        base_url,
        issuer,
        db_url,
        project=project,
        body=body,
        provision=provision_with("target"),
    )
    await settled(db_url, project)
    summary = run.writer.artifact(
        {
            "capture_target": {"system": "torn_down", "run": "booted", "session": "detached"},
            "cleanup": scratch.assertions["cleanup"],
        }
    )
    return CaptureTarget(
        project,
        ids["system"],
        ids["run"],
        ids["session"],
        lane_keys(scratch.observed, remote=run.cell.provider == "remote-libvirt"),
        (*scratch.artifacts, *scratch.assertions.values(), summary),
        connects[0],
    )


async def capture_target(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    fixture: Callable[[], tuple[Path, dict[str, Any]]],
) -> CaptureTarget:
    """The stack's rejection target, prepared on first use; a failure is replayed, not retried.

    The fixture boot runs on a scratch record, so the cached context keeps only the lane keys,
    with the provider host's on the remote lane, also for a failed preparation that reached it.
    Each provider of a stack has its own target.
    """
    key = (base_url, run.cell.provider)
    cached = _TARGETS.get(key)
    if cached is None:
        scratch = CellRun(run.cell, run.writer)
        try:
            cached = await _prepare_target(run, base_url, issuer, db_url, fixture, scratch)
        except Exception as exc:  # noqa: BLE001 - remembered and replayed for every cell
            remote = run.cell.provider == "remote-libvirt"
            cached = _Failed(
                exc,
                (*scratch.artifacts, *scratch.assertions.values()),
                lane_keys(scratch.observed, remote=remote),
            )
        _TARGETS[key] = cached
    if isinstance(cached, _Failed):
        run.observed |= cached.observed
        run.artifacts.extend(cached.artifacts)
        if isinstance(cached.error, ScenarioStop):
            raise ScenarioStop(cached.error.outcome, str(cached.error))
        raise AssertionError(f"the capture target could not be prepared: {cached.error!r}")
    run.observed |= cached.observed
    run.artifacts.extend(cached.artifacts)
    return cached


def _stranger() -> Any:
    other = f"cov-{secrets.token_hex(4)}"
    return grants(other, "admin")


def rejection(
    tool: str, boundary: Boundary, target: CaptureTarget, *, method: str = "kdump"
) -> Rejection:
    """The call ``boundary`` must reject, aimed at the capture target.

    ``method`` is the capture method a ``vmcore.fetch`` call names: the cell's variant.
    """
    key = _ID[tool]
    args: dict[str, object] = {key: target.id_for(tool), **_EXTRA.get(tool, {})}
    if tool == "vmcore.fetch":
        args["method"] = method
    gate = GATES[tool]
    if boundary == "validation":
        return Rejection({**args, key: 7}, grants(target.project, gate))
    if boundary == "authentication":
        # A viewer's issued-token call is refused by role, or only reads.
        return Rejection(args, grants(target.project, "viewer"))
    if boundary == "authorization":
        return Rejection(args, grants(target.project, _BELOW[gate]))
    assert boundary == "project-isolation", f"no {boundary} cell exists for {tool}"
    twin = {**args, key: str(uuid4())}
    return Rejection(args, _stranger(), frozenset({_ISOLATION[tool].value}), absent_twin=twin)


async def named_jobs(db_url: str, *object_ids: str) -> list[object]:
    """Row count and row-text SHA-256 of the jobs naming a System or Run in their payload."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*), coalesce(string_agg(t::text, ',' ORDER BY t::text), '') FROM jobs t "
            "WHERE t.payload->>'system_id' = ANY(%s) OR t.payload->>'run_id' = ANY(%s)",
            (list(object_ids), list(object_ids)),
        )
        row = await cursor.fetchone()
    assert row is not None
    return [row[0], hashlib.sha256(str(row[1]).encode()).hexdigest()]


async def protected_state(
    db_url: str, project: str, system_id: str, run_id: str, *, connect: Connect = worker_connect
) -> dict[str, object]:
    """The project's rows, the jobs naming the System or Run, and whether its domain exists.

    ``connect`` opens the provider libvirt that defines the domain: the worker's by default.
    """
    return {
        "project": await project_state(db_url, project),
        "jobs": await named_jobs(db_url, system_id, run_id),
        "domain_defined": await asyncio.to_thread(domain_defined, system_id, connect),
    }


def snapshot_of(db_url: str, target: CaptureTarget) -> Snapshot:
    return partial(
        protected_state,
        db_url,
        target.project,
        target.system_id,
        target.run_id,
        connect=target.connect,
    )


async def capability(
    run: CellRun, caller: HttpCaller, base_url: str, issuer: OidcIssuer, db_url: str
) -> None:
    """A crashed, Run-bound System refuses ``fadump`` as ``capability_unsupported``."""
    name, entry = lane_image()
    project = f"cov-{secrets.token_hex(4)}"

    async def body(op: LiveStackClient, system_id: str, _owned: list[str]) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            await observe_guest(run, op, system_id, Path(scratch), entry)
        investigation = ok(
            await scalar(op, "investigations.open", project=project, title="capability"), "open"
        ).object_id
        try:
            run_id = ok(
                await scalar(
                    op,
                    "runs.create",
                    investigation_id=investigation,
                    system_id=system_id,
                    build_profile=build_profile(entry.arch),
                ),
                "create-run",
            ).object_id
            token = mint_role_token(
                issuer, project=project, agent_session=f"{project}-sess", role="admin"
            )
            async with LiveStackClient.over_http(base_url, token) as admin:
                env = ok(await scalar(admin, "control.force_crash", system_id=system_id), "crash")
                await drained(admin, "crash", env.object_id)
            await await_system_state(op, "crash", system_id, "crashed")
            await settled(db_url, project)
            refusal = Rejection(
                {"run_id": run_id, "method": "fadump"}, grants(project, "contributor")
            )
            snapshot = partial(protected_state, db_url, project, system_id, run_id)
            await prove_rejection(run, caller, "capability", refusal, snapshot)
        finally:
            await scalar(
                op, "investigations.close", investigation_id=investigation, summary="capability"
            )

    await on_catalog_system(
        run,
        base_url,
        issuer,
        db_url,
        project=project,
        image=name,
        body=body,
        provision=provision_with("host_dump"),
    )
