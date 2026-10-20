"""Resource-boundary carriers: minimum sizing, insufficient disk and slow boot (#3118, ADR-0715).

Each ``failure-resource/<provider>/x86_64/{minimum-cpu-ram,insufficient-disk,slow-boot}`` cell
proves its operation-specific outcome plus ``no-duplicates``, ``protected-state`` and ``cleanup``
on a real stack (``docs/workflow/specs/2026-10-08-resource-boundary-proofs-design.md``).

- ``minimum-cpu-ram`` provisions the seeded ``small`` shape and boots the verified longterm
  fixture kernel on it.
- ``insufficient-disk`` mounts a test-owned loop filesystem, smaller than the upload's staging
  need, at the owned investigation's exact upload directory, so the provision fails its staging
  precheck with ``infrastructure_failure``.
- ``slow-boot`` arms a guest delay unit, keyed to the fixture kernel's release, before
  ``kdive-ready`` and the guest agent: inside the boot window the forced boot succeeds; past it,
  every attempt and the job end ``boot_timeout``.

Host fixtures run as fixed ``sudo -n`` argv on paths the test created, and a fixture whose
ownership cannot be re-proven is left in place and reported. The live node is
``tests/integration/test_resource_limits_live.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import time
import xml.etree.ElementTree as ET  # noqa: S405 - the worker's own domain XML  # nosec B405
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import UUID

import httpx
import libvirt
import psycopg
from psycopg.rows import dict_row

from kdive.images.rootfs.catalog import RootfsCatalogEntry, load_rootfs_catalog
from kdive.jobs.queue import DEFAULT_MAX_ATTEMPTS
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.providers.remote_libvirt.lifecycle.install import (
    _DEFAULT_BOOT_TIMEOUT_S as REMOTE_BOOT_WINDOW_S,
)
from kdive.providers.shared.runtime_paths import UPLOADS_DIR, domain_name_for
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings, Outcome
from tests.integration.live_stack.cleanup import (
    capacity_in_use,
    disk_absent,
    domain_disks,
    release_and_verify,
    system_failed,
)
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    KERNEL_PROBE,
    Fixture,
    InstalledKernel,
    bound_kernel,
    deep_body,
    file_sha256,
    kernel_inputs,
    load_fixture,
)
from tests.integration.live_stack.evidence import os_identity
from tests.integration.live_stack.image_smoke import Endpoint, GuestIdentity, ssh, staged_image
from tests.integration.live_stack.remote_lifecycle import (
    REMOTE_REPRESENTATIVES,
    RemoteHost,
    guest_boot_kernel,
    observe_host,
    observer,
    remote_host,
    remote_kdive_domains,
    remote_profile,
    remote_volume_absent,
    remote_xml,
    staged_base_volume,
    staged_volume,
    volume_sha256,
)
from tests.integration.live_stack.scenario import (
    ACCELERATORS,
    DEFAULT_SIZING,
    CellRun,
    ScenarioStop,
    Sizing,
    acquire_image,
    authorize_ssh,
    catalog_profile,
    cleanup_attempt,
    domain_xml,
    probe_new_boot,
    ssh_probe,
)
from tests.integration.live_stack.spine import (
    await_system_state,
    build_and_upload_kernel,
    build_profile,
    drain_job,
    mint_role_token,
    ok,
    scalar,
    seed_metering,
    worker_libvirt_uri,
)

OWNER = 3118
PROJECT = "resource-limits"
# The enterprise-family representative, the same image the deep-lifecycle cells boot.
IMAGE = "rocky-kdive-ready-10"
REMOTE_FAMILY = "enterprise"
SMALL = Sizing(vcpu=1, memory_mb=1024, disk_gb=10, shape="small")
# The bounded filesystem: far below any real rootfs plus the 1 GiB staging floor.
BOUND_MIB = 64
GIB = 1024**3
PROTECTED = frozenset({"granted", "active"})
# The local readiness window's documented default (KDIVE_LIBVIRT_BOOT_WINDOW_S); a configured
# value in the test environment wins.
LOCAL_BOOT_WINDOW_S = 900.0
DELAY_UNIT = "kdive-q3118-delay.service"
DELAY_MARK = "q3118-delay"
_FAILED_LINE = re.compile(r"job ([0-9a-f-]{36}) failed: ([a-z_]+)")
_SAMPLE_S = 15.0
_TERMINAL_JOB = frozenset({"succeeded", "failed", "canceled"})


def cells(provider: str | None = None, *, owner: int = OWNER) -> list[Cell]:
    """The six x86_64 ``failure-resource`` cells ``owner`` holds, optionally for one provider."""
    return [
        cell
        for cell in build_contract().cells
        if cell.owner == owner and (provider is None or cell.provider == provider)
    ]


def failure(cell: Cell) -> str:
    """The resource condition a cell proves: its scenario's last segment."""
    return cell.scenario_id.rsplit("/", 1)[1]


# --- per-attempt evidence (spec: "Per-attempt evidence") -----------------------------------


def attempt_categories(journal: str, job_id: str) -> list[str]:
    """The category of each failed attempt of ``job_id``, in order, from worker log lines."""
    return [m[2] for m in _FAILED_LINE.finditer(journal) if m[1] == job_id]


def attempt_record(lines: list[str], attempts: int, terminal: str | None) -> list[str]:
    """Per-attempt categories, or every one ``unknown`` when the log cannot be reconciled.

    The worker's failure line carries no attempt number, so attempt N is the Nth line. That
    holds only when the line count equals the job's attempt counter and the last line is the
    terminal category; an attempt lost to a lease lapse logs nothing and would shift the rest.
    """
    if len(lines) == attempts and (not lines or lines[-1] == terminal):
        return lines
    return ["unknown"] * attempts


def worker_journal(since_epoch: float) -> str:
    """Every fixed live-worker slot's journal since ``since_epoch``, message text only."""
    result = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        [
            "journalctl",
            "--no-pager",
            "-o",
            "cat",
            "-u",
            "kdive-live-worker@*",
            "--since",
            f"@{int(since_epoch)}",
        ],
        capture_output=True,
        text=True,
        timeout=120.0,
        check=False,
    )
    assert result.returncode == 0, f"journalctl exited {result.returncode}"
    return result.stdout


_JOB_ROW: LiteralString = (
    "SELECT state, attempt, max_attempts, error_category FROM jobs WHERE id = %s"
)


async def job_row(db_url: str, job_id: str) -> dict[str, object]:
    """The job's state, attempt counter, attempt budget and terminal category (read-only)."""
    async with (
        await psycopg.AsyncConnection.connect(db_url) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(_JOB_ROW, (job_id,))
        row = await cur.fetchone()
    assert row is not None, f"job {job_id} has no row"
    return dict(row)


@dataclass(frozen=True)
class Attempts:
    """A job's attempt counter and budget, terminal category and per-attempt categories."""

    count: int
    budget: int
    terminal: str | None
    per_attempt: list[str]


async def attempts(db_url: str, job_id: str, since: float) -> Attempts:
    """Reconcile the job row with the worker journal since ``since`` (see ``attempt_record``)."""
    row = await job_row(db_url, job_id)
    journal = await asyncio.to_thread(worker_journal, since)
    terminal = None if row["error_category"] is None else str(row["error_category"])
    count = cast(int, row["attempt"])
    lines = attempt_categories(journal, job_id)
    if count and not lines:
        # No line at all is an unreadable journal, not the lease-lapse gap "unknown" covers.
        raise ScenarioStop(
            Outcome.BLOCKED, "the worker journal shows no attempt; grant systemd-journal access"
        )
    per = attempt_record(lines, count, terminal)
    return Attempts(count, cast(int, row["max_attempts"]), terminal, per)


# --- protected state (spec: "Protected state and cleanup") ---------------------------------


@dataclass(frozen=True)
class Sample:
    """One observation: wall time, Allocation status, summed ``in_use`` and owned domains."""

    t: float
    allocation: str
    in_use: int
    domains: tuple[str, ...]


@dataclass
class Sampler:
    """In-flight samples of the Allocation, capacity and owned domains while a job runs."""

    allocation: str
    in_use_before: int
    domains: Callable[[], set[str]]
    samples: list[Sample] = field(default_factory=list)

    async def sample(self, op: LiveStackClient) -> None:
        env = await scalar(op, "allocations.wait", allocation_id=self.allocation, timeout_s=0)
        owned = await asyncio.to_thread(self.domains)
        in_use = await capacity_in_use(op)
        self.samples.append(Sample(round(time.time(), 1), env.status, in_use, tuple(sorted(owned))))

    def problems(self) -> list[str]:
        """Every sample that released protected state early or showed a duplicate domain."""
        found = []
        for s in self.samples:
            if s.allocation not in PROTECTED:
                found.append(f"allocation {s.allocation} at {s.t}")
            if s.in_use <= self.in_use_before:
                found.append(f"capacity {s.in_use} not above {self.in_use_before} at {s.t}")
            if len(s.domains) > 1:
                found.append(f"{len(s.domains)} owned domains at {s.t}")
        return found


async def wait_terminal(
    op: LiveStackClient, job_id: str, sampler: Sampler, *, deadline_s: float
) -> ToolResponse:
    """Sample protected state until ``job_id`` is terminal; a carrier deadline is incomplete."""
    deadline = time.monotonic() + deadline_s
    while True:
        await sampler.sample(op)
        env = await scalar(op, "jobs.wait", job_id=job_id, timeout_s=_SAMPLE_S)
        if env.status in _TERMINAL_JOB:
            await sampler.sample(op)
            return env
        if time.monotonic() >= deadline:
            raise ScenarioStop(
                Outcome.FAILURE, f"job {job_id} not terminal within {deadline_s:.0f} s (incomplete)"
            )


def kdive_domains() -> set[str]:
    """Every ``kdive-`` domain the worker's libvirt defines."""
    conn = libvirt.open(worker_libvirt_uri())
    try:
        return {d.name() for d in conn.listAllDomains(0) if d.name().startswith("kdive-")}
    finally:
        conn.close()


# --- minimum sizing ------------------------------------------------------------------------


def domain_sizing(xml: str) -> dict[str, int]:
    """The vCPU count and memory (MiB) a domain's XML gives its guest."""
    root = ET.fromstring(xml)  # noqa: S314 - trusted worker-rendered XML  # nosec B314
    memory = root.find("./memory")
    assert memory is not None and memory.text, "domain XML has no <memory>"
    scale = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[memory.get("unit", "KiB")]
    return {
        "vcpu": int(root.findtext("./vcpu", "0")),
        "memory_mib": int(memory.text) * scale // 1024**2,
    }


def virtual_size(path: str) -> int:
    """A disk image's virtual size in bytes; a worker-owned image is read with ``sudo -n``."""
    result = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["sudo", "-n", "qemu-img", "info", "--force-share", "--output=json", "--", path],
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
    )
    assert result.returncode == 0, f"qemu-img info exited {result.returncode}"
    return int(json.loads(result.stdout)["virtual-size"])


def prove_minimum(
    run: CellRun,
    shape: dict[str, object],
    xml: str,
    disks: list[str],
    size: Callable[[str], int] = virtual_size,
) -> None:
    """Prove the System runs at exactly the seeded ``small`` floor (1 vCPU, 1024 MiB, 10 GB)."""
    want = {"vcpus": SMALL.vcpu, "memory_mb": SMALL.memory_mb, "disk_gb": SMALL.disk_gb}
    assert {k: shape.get(k) for k in want} == want, f"shape small is {shape}, not {want}"
    sizing = domain_sizing(xml)
    assert sizing == {"vcpu": SMALL.vcpu, "memory_mib": SMALL.memory_mb}, sizing
    assert len(disks) == 1, f"expected one owned overlay, found {len(disks)}"
    capacity = size(disks[0])
    assert capacity == SMALL.disk_gb * GIB, f"overlay virtual size {capacity}"
    run.prove("minimum-cpu-ram", {"shape": want, "domain": sizing, "overlay_bytes": capacity})


async def small_shape(op: LiveStackClient) -> dict[str, object]:
    """The ``small`` row of ``shapes.list``; a missing seed is a blocked cell."""
    listing = ok(await scalar(op, "shapes.list"), "shapes")
    for item in listing.items:
        if item.data.get("name") == "small" or item.object_id == "small":
            return dict(item.data)
    raise ScenarioStop(Outcome.BLOCKED, "no seeded small shape")


# --- bounded storage (local insufficient-disk) ---------------------------------------------


def _sudo(*argv: str, timeout: float = 120.0) -> str:
    result = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["sudo", "-n", *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, f"sudo {argv[0]} exited {result.returncode}"
    return result.stdout


@dataclass(frozen=True)
class BoundedMount:
    """A test-owned loop filesystem mounted at one owned upload directory."""

    image: Path
    loop: str
    target: Path


def _selinux_context(path: Path) -> str | None:
    """The SELinux label to mount a new filesystem with, so its root reads as ``path`` does."""
    try:
        mode = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
            ["getenforce"], capture_output=True, text=True, timeout=10.0, check=False
        ).stdout.strip()
    except FileNotFoundError:
        return None
    if mode not in {"Enforcing", "Permissive"}:
        return None
    return _sudo("stat", "-c", "%C", "--", str(path)).strip()


def statvfs(target: Path) -> dict[str, int]:
    """The mounted filesystem's block size, free blocks and unprivileged-available blocks."""
    size, free, avail = _sudo("stat", "-f", "-c", "%S %f %a", "--", str(target)).split()
    return {"block": int(size), "f_bfree": int(free), "f_bavail": int(avail)}


def _exists(path: Path) -> bool:
    status = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["sudo", "-n", "test", "-e", str(path)], timeout=10.0, check=False
    ).returncode
    assert status in (0, 1), f"cannot observe {path}: sudo test exited {status}"
    return status == 0


def mount_bounded(investigation_id: str, workdir: Path, size_mib: int) -> BoundedMount:
    """Mount a fresh ``size_mib`` ext4 loop filesystem at the investigation's upload directory.

    The directory must not exist yet: it belongs to this new investigation, and mounting over
    a directory with content would hide it. The filesystem has no root reserve (``-m 0``), so
    a root writer cannot exceed the calibrated bound, and no ``lost+found``, so it is empty.
    A failed step undoes the earlier ones, so a refused fixture leaves nothing behind.
    """
    if not _exists(Path(UPLOADS_DIR)):
        raise ScenarioStop(
            Outcome.BLOCKED, f"host preparation did not create {UPLOADS_DIR} for the workers"
        )
    target = Path(UPLOADS_DIR) / str(UUID(investigation_id))
    if _exists(target):
        raise ScenarioStop(Outcome.FAILURE, "the new investigation's upload directory exists")
    image = workdir / "bounded.img"
    subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["fallocate", "-l", f"{size_mib}M", str(image)], check=True, timeout=60.0
    )
    options = "nodev,nosuid,noexec"
    context = _selinux_context(Path(UPLOADS_DIR))
    if context:
        options += f",context={context}"
    loop = _sudo("losetup", "--find", "--show", "--", str(image)).strip()
    try:
        _sudo("mkfs.ext4", "-q", "-m", "0", loop)
        _sudo("mkdir", "--", str(target))
        _sudo("mount", "-t", "ext4", "-o", options, loop, str(target))
        _sudo("rmdir", "--", str(target / "lost+found"))
        _sudo("chown", f"--reference={UPLOADS_DIR}", "--", str(target))
        _sudo("chmod", f"--reference={UPLOADS_DIR}", "--", str(target))
        assert mounted_source(target) == loop, "the bounded filesystem is not at its directory"
    except BaseException:
        if mounted_source(target) == loop:
            _sudo("umount", "--", str(target))
        if _exists(target):
            _sudo("rmdir", "--", str(target))
        _sudo("losetup", "-d", loop)
        raise
    return BoundedMount(image, loop, target)


def mounted_source(target: Path) -> str | None:
    """The device mounted exactly at ``target``, or ``None``."""
    result = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["findmnt", "-n", "-o", "SOURCE", "--mountpoint", str(target)],
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )
    return result.stdout.strip() or None


def entries(target: Path) -> list[str]:
    """The names directly under ``target`` (read as root; the worker owns it)."""
    out = _sudo("find", str(target), "-mindepth", "1", "-maxdepth", "1", "-printf", "%f\\n")
    return sorted(line for line in out.splitlines() if line)


def unmount_bounded(mount: BoundedMount) -> dict[str, object]:
    """Unmount, detach and remove the fixture; refuse anything whose ownership changed."""
    assert mounted_source(mount.target) == mount.loop, "the upload directory's mount changed"
    left = entries(mount.target)
    assert not left, f"the bounded filesystem still holds {left}"
    _sudo("umount", "--", str(mount.target))
    backing = _sudo("losetup", "--noheadings", "-O", "BACK-FILE", mount.loop).strip()
    assert backing == str(mount.image), "the loop device no longer backs the owned image"
    _sudo("losetup", "-d", mount.loop)
    underlying = entries(mount.target)
    assert not underlying, f"the upload directory held {underlying} under the mount"
    _sudo("rmdir", "--", str(mount.target))
    mount.image.unlink()
    return {"unmounted": True, "loop_detached": True, "directory_removed": True}


# --- uploaded rootfs -----------------------------------------------------------------------


def _b64_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return base64.b64encode(hashlib.file_digest(stream, "sha256").digest()).decode()


def _put_file(url: str, path: Path, headers: dict[str, str]) -> int:
    """Stream ``path`` to a presigned PUT with its exact length; return the HTTP status.

    A synchronous client, because an async one refuses a file-object body and the image is
    too large to read into memory.
    """
    sized = {**headers, "Content-Length": str(path.stat().st_size)}
    with path.open("rb") as body, httpx.Client(timeout=1800.0) as http:
        return http.put(url, content=body, headers=sized).status_code


async def upload_rootfs(op: LiveStackClient, investigation_id: str, image: Path) -> str:
    """Upload ``image`` as the investigation's rootfs; return its ``checksum_sha256`` handle."""
    declared = {"name": "rootfs", "sha256": _b64_sha256(image), "size_bytes": image.stat().st_size}
    env = ok(
        await scalar(
            op,
            "artifacts.create_investigation_upload",
            investigation_id=investigation_id,
            artifacts=[declared],
        ),
        "upload",
    )
    item = env.items[0]
    headers = cast(dict[str, str], item.data["required_headers"])
    status = await asyncio.to_thread(_put_file, item.refs["upload_url"], image, headers)
    # The status alone: an error would echo the presigned URL and its signature.
    assert status == 200, f"presigned PUT answered HTTP {status}"
    done = ok(
        await scalar(
            op, "investigations.complete_rootfs_upload", investigation_id=investigation_id
        ),
        "complete-upload",
    )
    assert done.data.get("checksum_sha256") == declared["sha256"], "upload handle mismatch"
    return str(declared["sha256"])


def upload_profile(arch: str, checksum: str) -> dict[str, object]:
    """A direct-kernel profile booting the investigation's uploaded rootfs."""
    return {
        "schema_version": 1,
        "arch": arch,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 10,
        "boot_method": "direct-kernel",
        "kernel_source_ref": f"{PROJECT}-unread",
        "provider": {"local-libvirt": {"rootfs": {"kind": "upload", "checksum_sha256": checksum}}},
    }


# --- slow boot -----------------------------------------------------------------------------


def delay_unit(release: str, delay_s: int) -> str:
    """A systemd unit holding ``kdive-ready`` and the guest agent back on ``release`` boots.

    ``oneshot`` with no start timeout so systemd cannot cut the delay short; the condition
    keeps the delay off every other kernel, so the baseline kernel and shutdown are unaffected.
    Each mark carries the guest's own uptime when it was written: the journal stamps a line
    when it receives it, which can shorten the measured span below the sleep. ``$$`` is
    systemd's escape for a literal ``$``.
    """
    stamp = "read u _ < /proc/uptime; echo"
    return (
        "[Unit]\n"
        f"Description=kdive #3118 slow-boot delay ({delay_s}s)\n"
        f"ConditionKernelVersion={release}\n"
        "Before=kdive-ready.service qemu-guest-agent.service\n"
        "\n[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=infinity\n"
        f"ExecStart=/bin/sh -c '{stamp} {DELAY_MARK}-start $$u; sleep {delay_s}; "
        f"{stamp} {DELAY_MARK}-end $$u'\n"
        "\n[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def arm_command(release: str, delay_s: int) -> str:
    """The guest command (run as root over SSH) installing and enabling the delay unit."""
    unit = shlex.quote(delay_unit(release, delay_s))
    return (
        f"printf '%s' {unit} > /etc/systemd/system/{DELAY_UNIT} && "
        f"systemctl daemon-reload && systemctl enable {DELAY_UNIT} >/dev/null 2>&1 && "
        f"systemctl is-enabled {DELAY_UNIT}"
    )


# One journal line per unit event of the current boot: "<usec> <unit> <message>".
JOURNAL_PROBE = (
    "journalctl -b 0 -o json --no-pager "
    f"_SYSTEMD_UNIT={DELAY_UNIT} + UNIT={DELAY_UNIT} + UNIT=qemu-guest-agent.service "
    "+ UNIT=kdive-ready.service"
)


def delay_ordering(journal_json: str) -> dict[str, float]:
    """Seconds since boot at which the delay ended and the agent and ``kdive-ready`` started.

    Read from the guest's own journal of one boot: the delay unit's ``-end`` mark, and
    systemd's first "Starting" line for each dependent unit.
    """
    marks: dict[str, float] = {}
    for line in journal_json.splitlines():
        entry = json.loads(line)
        t = int(entry["__MONOTONIC_TIMESTAMP"]) / 1e6
        message = str(entry.get("MESSAGE", ""))
        unit = entry.get("UNIT") or entry.get("_SYSTEMD_UNIT")
        mark, _, uptime = message.partition(" ")
        if mark in (f"{DELAY_MARK}-start", f"{DELAY_MARK}-end"):
            kind = mark.rsplit("-", 1)[1]
            if f"delay_{kind}" not in marks:
                marks[f"delay_{kind}"] = t
                marks[f"uptime_{kind}"] = float(uptime)
        elif unit == "qemu-guest-agent.service" and message.startswith("Start"):
            marks.setdefault("agent_start", t)
        elif unit == "kdive-ready.service" and message.startswith("Start"):
            marks.setdefault("ready_start", t)
    return marks


def delay_held(marks: dict[str, float], delay_s: int, readiness: str = "ready_start") -> list[str]:
    """Why the journal does not show the delay running in full before both dependents.

    ``readiness`` is the dependent the provider's readiness waits on, which must be present.
    """
    missing = [k for k in ("delay_start", "delay_end", readiness) if k not in marks]
    if missing:
        return [f"journal lacks {missing}"]
    problems = []
    lasted = marks["uptime_end"] - marks["uptime_start"]
    if lasted < delay_s:
        problems.append(f"delay lasted {lasted:.0f} s")
    for dependent in ("ready_start", "agent_start"):
        if dependent in marks and marks[dependent] < marks["delay_end"]:
            problems.append(f"{dependent} preceded the delay's end")
    return problems


def inside_delay(baseline_s: float, window_s: float) -> int | None:
    """A delay at least 60 s beyond the baseline boot whose total stays under half the window."""
    delay = max(60, int(baseline_s) + 60)
    return delay if baseline_s + delay < window_s / 2 else None


def beyond_delay(window_s: float) -> int:
    """A per-boot delay longer than the boot window plus 60 s."""
    return int(window_s) + 120


def local_window_s() -> float:
    """The local readiness window: a configured value, else the documented default."""
    return float(os.environ.get("KDIVE_LIBVIRT_BOOT_WINDOW_S", LOCAL_BOOT_WINDOW_S))


async def timed_boot(op: LiveStackClient, run_id: str, *, force: bool) -> tuple[str, float]:
    """Issue ``runs.boot``; return its job id and the monotonic time it was issued."""
    started = time.monotonic()
    env = ok(await scalar(op, "runs.boot", run_id=run_id, force=force), "boot")
    return env.object_id, started


# --- providers ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    """Where a cell's domains and volumes live: the worker's libvirt or a remote provider host."""

    resource: dict[str, object]
    domains: Callable[[], set[str]]
    xml: Callable[[str], str]
    size: Callable[[str], int]
    connect: Callable[[], Any]
    absent: Callable[[str], bool]
    host: RemoteHost | None = None


LOCAL = Provider(
    {"mode": "kind"},
    kdive_domains,
    domain_xml,
    virtual_size,
    lambda: libvirt.open(worker_libvirt_uri()),
    disk_absent,
)


def remote_provider(host: RemoteHost) -> Provider:
    """The provider host, observed through the operator's own SSH libvirt connection."""
    return Provider(
        {"mode": "kind", "kind": "remote-libvirt"},
        partial(remote_kdive_domains_of, host.dest),
        partial(remote_xml, host.dest),
        partial(remote_capacity, host.dest),
        partial(observer, host.dest),
        partial(remote_volume_absent, host.dest),
        host,
    )


def remote_kdive_domains_of(dest: str) -> set[str]:
    conn = observer(dest)
    try:
        return remote_kdive_domains(conn)
    finally:
        conn.close()


def remote_capacity(dest: str, path: str) -> int:
    """A provider-host volume's capacity in bytes."""
    conn = observer(dest)
    try:
        return int(conn.storageVolLookupByPath(path).info()[1])
    finally:
        conn.close()


# --- the frame -----------------------------------------------------------------------


@dataclass
class Held:
    """One owned Allocation of a local cell and what the cell made it own."""

    run: CellRun
    op: LiveStackClient
    provider: Provider
    allocation: str
    in_use_before: int
    domains_before: set[str]
    system_id: str | None = None
    disks: list[str] = field(default_factory=list)
    overlays: list[str] = field(default_factory=list)
    sampler: Sampler | None = None

    async def provision(self, profile: dict[str, object], **args: object) -> str:
        """Enqueue the provision; return its job id (the System id is kept on ``self``)."""
        env = ok(
            await scalar(
                self.op, "systems.provision", allocation_id=self.allocation, profile=profile, **args
            ),
            "provision",
        )
        self.system_id = str(env.data["system_id"])
        before = self.domains_before
        self.sampler = Sampler(
            self.allocation, self.in_use_before, lambda: self.provider.domains() - before
        )
        return env.object_id

    async def provision_ready(self, profile: dict[str, object]) -> str:
        """Provision to ``ready`` and record the System's one owned overlay."""
        await self.provision(profile)
        assert self.system_id is not None
        await await_system_state(self.op, "provision", self.system_id, "ready")
        xml = await asyncio.to_thread(self.provider.xml, self.system_id)
        accelerator = ET.fromstring(xml).get("type", "")  # noqa: S314  # nosec B314
        self.run.observed["accelerator"] = ACCELERATORS.get(accelerator, "none")
        self.overlays = domain_disks(xml)
        self.disks = list(self.overlays)
        assert self.sampler is not None
        await self.sampler.sample(self.op)
        return self.system_id

    async def prove_held(self, extra: dict[str, object] | None = None) -> None:
        """Prove ``no-duplicates`` and ``protected-state`` from the samples taken so far."""
        assert self.sampler is not None and self.system_id is not None
        await self.sampler.sample(self.op)
        listing = ok(
            await scalar(self.op, "systems.list", request={"allocation_id": self.allocation}),
            "systems",
        )
        systems = sorted(item.object_id for item in listing.items)
        added = await asyncio.to_thread(self.provider.domains) - self.domains_before
        assert systems == [self.system_id], f"Systems under the allocation: {systems}"
        assert added <= {domain_name_for(UUID(self.system_id))}, f"domains added: {sorted(added)}"
        overlays = (
            domain_disks(await asyncio.to_thread(self.provider.xml, self.system_id))
            if added
            else []
        )
        assert overlays == self.overlays and len(overlays) <= 1, f"owned overlays: {overlays}"
        self.run.prove(
            "no-duplicates",
            {
                "systems": 1,
                "domains_added": len(added),
                "overlays": len(overlays),
                **(extra or {}),
            },
        )
        problems = self.sampler.problems()
        assert not problems, f"protected state released early: {problems}"
        self.run.prove(
            "protected-state",
            {
                "samples": [asdict(x) for x in self.sampler.samples],
                "in_use_before": self.in_use_before,
            },
        )

    async def release(self) -> dict[str, object]:
        """Release and prove the System, domain, overlay and capacity reclaimed."""
        assert self.system_id is not None
        state = await scalar(self.op, "systems.get", system_id=self.system_id)
        return await release_and_verify(
            self.op,
            allocation_id=self.allocation,
            system_id=self.system_id,
            domain=domain_name_for(UUID(self.system_id)),
            disks=self.disks,
            in_use_before=self.in_use_before,
            # Only a System already failed before the release may pass through `failed`.
            from_failed=system_failed(state),
            connect=self.provider.connect,
            absent=self.provider.absent,
        )


Body = Callable[[Held], Awaitable[None]]


async def on_allocation(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    sizing: Sizing,
    body: Body,
    provider: Provider = LOCAL,
    project: str = PROJECT,
) -> None:
    """Hold one Allocation of ``sizing`` in ``project`` for ``body``; record a cleanup attempt.

    ``body`` proves ``cleanup`` itself, because its owned fixtures are removed after release.
    """
    token = mint_role_token(
        issuer, project=project, agent_session=f"{project}-sess", role="operator"
    )
    async with LiveStackClient.over_http(base_url, token) as op:
        await seed_metering(db_url, project)
        in_use = await capacity_in_use(op)
        domains = await asyncio.to_thread(provider.domains)
        allocation = ok(
            await scalar(
                op,
                "allocations.request",
                project=project,
                resource=provider.resource,
                **sizing.request(),
            ),
            "allocate",
        ).object_id
        held = Held(run, op, provider, allocation, in_use, domains)
        try:
            await body(held)
        finally:
            if "cleanup" not in run.assertions:
                await cleanup_attempt(
                    run, op, allocation, None if held.system_id is None else held.release
                )


Kernel = tuple[Path, dict[str, Any]]


def fixture() -> Kernel:
    """The verified longterm fixture kernel; absent or changed is a blocked cell."""
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the longterm fixture")
    try:
        return load_fixture(Path(root), "longterm", "x86_64")
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"longterm fixture: {exc}") from None


async def registered(
    op: LiveStackClient, run: CellRun, kernel: tuple[Path, dict[str, Any]]
) -> RootfsCatalogEntry:
    """The registered representative image; record it and the fixture kernel as cell inputs.

    Every cell binds the guest and kernel it was set up with, including a cell whose provision
    is refused before that kernel could boot.
    """
    entry = load_rootfs_catalog()[IMAGE]
    acquired = await acquire_image(op, IMAGE, entry.arch)
    run.observed |= {
        "image_sha256": str(acquired["digest"]).removeprefix("sha256:"),
        "guest_os": f"{entry.distro}:{entry.version}",
        "guest_arch": entry.arch,
        **kernel_inputs(*kernel),
    }
    return entry


def domain_kernel(
    system_id: str, _endpoint: Endpoint, _key: Path, _release: str
) -> tuple[str, str]:
    """The direct-kernel file the local domain boots (the staged install) and its digest."""
    kernel = ET.fromstring(domain_xml(system_id)).findtext("./os/kernel")  # noqa: S314  # nosec B314
    assert kernel, "the booted domain names no direct kernel"
    return file_sha256(kernel), kernel


def demote_substeps(run: CellRun) -> None:
    """Keep proofs outside the cell's assertion set as retained artifacts (qualify is exact)."""
    for name in [a for a in run.assertions if a not in run.cell.assertions]:
        run.artifacts.append(run.assertions.pop(name))


# --- local scenarios -----------------------------------------------------------------------


@dataclass(frozen=True)
class Guest:
    """The guest a boot cell runs: its image, profile, installed-kernel probe and boot window.

    ``readiness`` is the journal mark that shows the delay held what the provider waits on:
    ``kdive-ready`` for local-libvirt, the guest agent for remote-libvirt.
    """

    profile: Callable[[Sizing], dict[str, object]]
    entry: GuestIdentity
    installed_kernel: InstalledKernel
    window_s: float
    readiness: str


async def local_guest(op: LiveStackClient, run: CellRun, kernel: Kernel) -> Guest:
    entry = await registered(op, run, kernel)
    return Guest(
        lambda sizing: catalog_profile(entry, IMAGE, f"{PROJECT}-unread", sizing),
        entry,
        domain_kernel,
        local_window_s(),
        "ready_start",
    )


def remote_guest(run: CellRun, host: RemoteHost, kernel: Kernel) -> Guest:
    """The enterprise remote representative on its staged base volume."""
    image = REMOTE_REPRESENTATIVES[REMOTE_FAMILY]
    volume = staged_base_volume(image)
    run.observed |= {
        "image_sha256": volume_sha256(host.dest, host.pool, volume),
        "guest_os": f"{image.distro}:{image.version}",
        "guest_arch": image.arch,
        **kernel_inputs(*kernel),
    }

    def profile(sizing: Sizing) -> dict[str, object]:
        base = remote_profile(image.arch, volume)
        return {
            **base,
            "vcpu": sizing.vcpu,
            "memory_mb": sizing.memory_mb,
            "disk_gb": sizing.disk_gb,
        }

    return Guest(
        profile,
        image,
        partial(guest_boot_kernel, family=REMOTE_FAMILY),
        REMOTE_BOOT_WINDOW_S,
        "agent_start",
    )


async def boot_cell(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    sizing: Sizing,
    remote: bool,
    body: Callable[[Held, Guest, Kernel], Awaitable[None]],
) -> None:
    """Set up the provider and guest for a boot cell and hold one Allocation for ``body``."""
    kernel = fixture()
    if not remote:

        async def local(held: Held) -> None:
            await body(held, await local_guest(held.op, run, kernel), kernel)

        await on_allocation(run, base_url, issuer, db_url, sizing, local)
        return
    host = remote_host()
    observe_host(run, host)
    guest = await asyncio.to_thread(remote_guest, run, host, kernel)

    async def on_remote(held: Held) -> None:
        await body(held, guest, kernel)

    await on_allocation(run, base_url, issuer, db_url, sizing, on_remote, remote_provider(host))


async def minimum(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path, remote: bool
) -> None:
    """``small`` provisions, boots the fixture kernel, answers SSH and is cleaned up."""

    async def body(held: Held, guest: Guest, kernel: Kernel) -> None:
        shape = await small_shape(held.op)
        system_id = await held.provision_ready(guest.profile(SMALL))
        xml = await asyncio.to_thread(held.provider.xml, system_id)
        await asyncio.to_thread(prove_minimum, run, shape, xml, held.overlays, held.provider.size)
        await deep_body(
            run,
            held.op,
            system_id,
            held.disks,
            project=PROJECT,
            entry=guest.entry,
            tree=kernel[0],
            manifest=kernel[1],
            tmp=tmp,
            installed_kernel=guest.installed_kernel,
        )
        demote_substeps(run)
        await held.prove_held()
        run.prove("cleanup", await held.release())

    await boot_cell(run, base_url, issuer, db_url, sizing=SMALL, remote=remote, body=body)


async def local_insufficient_disk(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path
) -> None:
    """A provision whose upload cannot be staged fails ``infrastructure_failure``, leaks nothing."""
    tree, manifest = fixture()
    image = staged_image(IMAGE)
    if image is None:
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"no staged {IMAGE} in KDIVE_SYSTEMS_TOML; build it and export the inventory path",
        )

    async def body(held: Held) -> None:
        entry = await registered(held.op, run, (tree, manifest))
        investigation = ok(
            await scalar(
                held.op, "investigations.open", project=PROJECT, title="insufficient disk"
            ),
            "open",
        ).object_id
        mount = await asyncio.to_thread(mount_bounded, investigation, tmp, BOUND_MIB)
        unmounted = False
        try:
            checksum = await upload_rootfs(held.op, investigation, image)
            need = image.stat().st_size
            free = await asyncio.to_thread(statvfs, mount.target)
            assert free["f_bavail"] * free["block"] < need, f"bound {free} holds {need} bytes"
            since = time.time()
            job = await held.provision(
                upload_profile(entry.arch, checksum), investigation_id=investigation
            )
            assert held.sampler is not None
            env = await wait_terminal(held.op, job, held.sampler, deadline_s=1800.0)
            terminal = await attempts(db_url, job, since)
            # No domain ever existed; the accelerator is the one admission resolved for it.
            got = await scalar(held.op, "systems.get", system_id=held.system_id)
            accel = got.data.get("accel")
            run.observed["accelerator"] = accel if accel in {"kvm", "tcg"} else "none"
            journal = await asyncio.to_thread(worker_journal, since)
            assert (env.status, env.error_category) == ("failed", "infrastructure_failure"), (
                f"provision {env.status} {env.error_category}"
            )
            assert set(terminal.per_attempt) <= {"infrastructure_failure", "unknown"}, terminal
            assert f"refusing to stage an uploaded rootfs under {mount.target}" in journal, (
                "no capacity-shortfall refusal for the owned upload directory"
            )
            left = await asyncio.to_thread(entries, mount.target)
            assert not left, f"staging left {left}"
            added = await asyncio.to_thread(held.provider.domains)
            assert not added - held.domains_before, "a domain was defined"
            run.prove(
                "insufficient-disk",
                {
                    "category": env.error_category,
                    **asdict(terminal),
                    "bound_mib": BOUND_MIB,
                    "statvfs": free,
                    "needed": need,
                },
            )
            await held.prove_held({"staged_entries": 0})
            released = await held.release()
            removed = await asyncio.to_thread(unmount_bounded, mount)
            unmounted = True
            closed = await scalar(
                held.op, "investigations.close", investigation_id=investigation, summary="done"
            )
            assert closed.status == "closed", f"investigation {closed.status}"
            run.prove("cleanup", {**released, **removed, "investigation": "closed"})
        finally:
            if not unmounted:
                # Never unmount under a live operation: the attempt is evidence only.
                run.artifacts.append(
                    run.writer.artifact(
                        {
                            "cell": run.cell.id,
                            "bounded_mount": "retained",
                            "investigation": investigation,
                            **{k: str(v) for k, v in asdict(mount).items()},
                        }
                    )
                )

    await on_allocation(run, base_url, issuer, db_url, DEFAULT_SIZING, body)


# The remote provider's overlay-create refusal (remote_libvirt/lifecycle/storage.py).
OVERLAY_REFUSAL = "could not create the per-System overlay volume"
# The free space a full pool keeps: less than a qcow2 overlay's header and refcount tables.
_BALLAST_KEEP_BYTES = 64 * 1024
# Test-only input, unprefixed so env-docs-check does not require it in the product catalog.
POOL_IMAGE_ENV = "RESOURCE_LIMIT_POOL_IMAGE"


def host_run(dest: str, *argv: str, timeout: float = 120.0) -> str:
    """Run fixed ``argv`` on the provider host over the operator's SSH; a non-zero exit fails."""
    result = subprocess.run(  # noqa: S603,S607 - fixed argv, validated destination  # nosec B603 B607
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", dest, shlex.join(argv)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, f"provider-host {argv[0]} exited {result.returncode}"
    return result.stdout


def pool_target(dest: str, pool: str) -> Path:
    """The directory the provider host's storage pool ``pool`` keeps its volumes in."""
    conn = observer(dest)
    try:
        xml = conn.storagePoolLookupByName(pool).XMLDesc(0)
    finally:
        conn.close()
    path = ET.fromstring(xml).findtext("./target/path")  # noqa: S314  # nosec B314
    assert path, f"pool {pool} names no target path"
    return Path(path)


def bounded_pool(dest: str, target: Path, image: str) -> None:
    """Refuse any pool whose directory is not the test-owned loop filesystem backed by ``image``.

    Only that filesystem may be filled: a pool on a shared filesystem is never a fixture.
    """
    try:
        source = host_run(dest, "findmnt", "-n", "-o", "SOURCE,FSTYPE", "--mountpoint", str(target))
    except AssertionError:  # findmnt exits 1 when the directory is not a mount point
        source = ""
    device, _, fstype = source.strip().partition(" ")
    if not device.startswith("/dev/loop") or fstype.strip() != "ext4":
        raise ScenarioStop(Outcome.BLOCKED, f"pool directory is not a loop ext4 mount: {source!r}")
    backing = host_run(dest, "sudo", "-n", "losetup", "--noheadings", "-O", "BACK-FILE", device)
    if backing.strip() != image:
        raise ScenarioStop(Outcome.BLOCKED, "the pool's loop device is not the owned image")


def provider_accelerator(dest: str) -> str:
    """``kvm`` when the provider host's libvirt offers KVM guests, else ``tcg``."""
    result = subprocess.run(  # noqa: S603,S607 - fixed argv, validated destination  # nosec B603 B607
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            dest,
            "virsh -q -c qemu:///system domcapabilities --virttype kvm --arch x86_64",
        ],
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
    )
    return "kvm" if result.returncode == 0 and "<domainCapabilities>" in result.stdout else "tcg"


def pool_free(dest: str, target: Path) -> dict[str, int]:
    size, free, avail = host_run(dest, "stat", "-f", "-c", "%S %f %a", str(target)).split()
    return {"block": int(size), "f_bfree": int(free), "f_bavail": int(avail)}


def pool_volumes(dest: str, pool: str) -> set[str]:
    conn = observer(dest)
    try:
        handle = conn.storagePoolLookupByName(pool)
        handle.refresh(0)
        return set(handle.listVolumes())
    finally:
        conn.close()


async def remote_insufficient_disk(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path
) -> None:
    """A provision whose overlay cannot be created fails ``provisioning_failure``, leaks nothing.

    The provider host's pool sits on a test-owned loop filesystem; one owned ballast file
    leaves less free space than a qcow2 overlay's metadata needs.
    """
    del tmp
    image = os.environ.get(POOL_IMAGE_ENV)
    if not image:
        raise ScenarioStop(Outcome.BLOCKED, f"{POOL_IMAGE_ENV} unset; name the bounded pool image")

    async def body(held: Held, guest: Guest, _kernel: Kernel) -> None:
        host = held.provider.host
        assert host is not None
        target = await asyncio.to_thread(pool_target, host.dest, host.pool)
        await asyncio.to_thread(bounded_pool, host.dest, target, image)
        volumes = await asyncio.to_thread(pool_volumes, host.dest, host.pool)
        before = await asyncio.to_thread(pool_free, host.dest, target)
        ballast = target / f"q3118-ballast-{os.urandom(4).hex()}"
        size = before["f_bavail"] * before["block"] - _BALLAST_KEEP_BYTES
        try:
            # Inside the try: a fallocate that fails can still leave its file behind.
            await asyncio.to_thread(
                host_run, host.dest, "sudo", "-n", "fallocate", "-l", str(size), str(ballast)
            )
            bound = await asyncio.to_thread(pool_free, host.dest, target)
            since = time.time()
            job = await held.provision(guest.profile(DEFAULT_SIZING))
            assert held.sampler is not None
            env = await wait_terminal(held.op, job, held.sampler, deadline_s=1800.0)
            terminal = await attempts(db_url, job, since)
            assert (env.status, env.error_category) == ("failed", "provisioning_failure"), (
                f"provision {env.status} {env.error_category}"
            )
            assert set(terminal.per_attempt) <= {"provisioning_failure", "unknown"}, terminal
            # A define or start failure is also provisioning_failure; only the overlay
            # create refusal is the storage outcome this cell proves.
            journal = await asyncio.to_thread(worker_journal, since)
            assert OVERLAY_REFUSAL in journal, "the provision did not fail creating the overlay"
            left = await asyncio.to_thread(pool_volumes, host.dest, host.pool)
            assert left - volumes == {ballast.name}, f"pool gained {sorted(left - volumes)}"
            added = await asyncio.to_thread(held.provider.domains)
            assert not added - held.domains_before, "a domain was defined"
            # Remote admission resolves no accelerator and no domain ever existed, so record
            # the one the provider host would have run it under.
            run.observed["accelerator"] = await asyncio.to_thread(provider_accelerator, host.dest)
            run.prove(
                "insufficient-disk",
                {
                    "category": env.error_category,
                    **asdict(terminal),
                    "statvfs": {"before": before, "bounded": bound},
                    "ballast_bytes": size,
                },
            )
            await held.prove_held({"partial_volumes": 0})
        finally:
            await asyncio.to_thread(
                host_run, host.dest, "sudo", "-n", "rm", "-f", "--", str(ballast)
            )
        released = await held.release()
        after = await asyncio.to_thread(pool_volumes, host.dest, host.pool)
        assert after == volumes, f"pool volumes changed: {sorted(after ^ volumes)}"
        run.prove("cleanup", {**released, "ballast": "removed", "pool_volumes": "unchanged"})

    await boot_cell(run, base_url, issuer, db_url, sizing=DEFAULT_SIZING, remote=True, body=body)


async def slow_boot(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path, remote: bool
) -> None:
    """A delayed boot inside the window is ready; past it, every attempt is ``boot_timeout``."""

    async def body(held: Held, guest: Guest, kernel: Kernel) -> None:
        tree, manifest = kernel
        release, window, entry = str(manifest["release"]), guest.window_s, guest.entry
        system_id = await held.provision_ready(guest.profile(DEFAULT_SIZING))
        endpoint, key = await authorize_ssh(held.op, system_id, tmp, "slow-boot")
        investigation = ok(
            await scalar(held.op, "investigations.open", project=PROJECT, title="slow boot"), "open"
        ).object_id
        try:
            run_id = ok(
                await scalar(
                    held.op,
                    "runs.create",
                    investigation_id=investigation,
                    system_id=system_id,
                    build_profile=build_profile(entry.arch),
                ),
                "create-run",
            ).object_id
            await build_and_upload_kernel(
                held.op,
                run_id=run_id,
                arch=entry.arch,
                kernel_tree=tree,
                evidence_dir=tmp / "upload",
                with_vmlinux=True,
                require_network=True,
                root_fs="ext4",
            )
            await drain_job(
                held.op,
                "install",
                ok(await scalar(held.op, "runs.install", run_id=run_id), "install").object_id,
            )
            digest, path = await asyncio.to_thread(
                guest.installed_kernel, system_id, endpoint, key, release
            )
            if path is not None:  # a host-staged kernel the release must remove
                held.disks.append(path)
            assert digest == run.observed["kernel_sha256"], "installed kernel differs"
            job, started = await timed_boot(held.op, run_id, force=False)
            await drain_job(held.op, "baseline-boot", job)
            baseline = time.monotonic() - started
            inside = inside_delay(baseline, window)
            if inside is None:
                raise ScenarioStop(
                    Outcome.BLOCKED,
                    f"baseline boot {baseline:.0f} s leaves no margin in {window:.0f} s",
                )
            delays = {"baseline_s": round(baseline, 1), "window_s": window, "inside_s": inside}
            proof = await delayed_boot(held, run_id, endpoint, key, release, inside, window, guest)
            assert proof["status"] == "succeeded", f"inside-window boot {proof}"
            beyond = beyond_delay(window)
            delays["beyond_s"] = beyond
            since = time.time()
            late = await delayed_boot(held, run_id, endpoint, key, release, beyond, window, guest)
            per = await attempts(db_url, str(late["job"]), since)
            assert (late["status"], late["category"]) == ("failed", "boot_timeout"), late
            assert per.count == per.budget, per
            assert set(per.per_attempt) <= {"boot_timeout", "unknown"}, per
            run.prove(
                "slow-boot", {"delays": delays, "inside": proof, "beyond": {**late, **asdict(per)}}
            )
            await held.prove_held()
        except BaseException:
            # Best effort, so a failed close cannot replace the fault the cell recorded.
            with contextlib.suppress(Exception):
                await scalar(held.op, "investigations.close", investigation_id=investigation)
            raise
        closed = await scalar(
            held.op, "investigations.close", investigation_id=investigation, summary="done"
        )
        assert closed.status == "closed", f"investigation {closed.status}"
        run.prove("cleanup", await held.release())

    await boot_cell(run, base_url, issuer, db_url, sizing=DEFAULT_SIZING, remote=remote, body=body)


async def delayed_boot(
    held: Held,
    run_id: str,
    endpoint: Endpoint,
    key: Path,
    release: str,
    delay: int,
    window: float,
    guest: Guest,
) -> dict[str, object]:
    """Arm ``delay`` and force a boot; return its outcome and the guest journal's proof.

    The journal is read from the boot the delay held, once the guest answers on it.
    """
    before = await asyncio.to_thread(ssh_probe, endpoint, key)
    armed = await asyncio.to_thread(ssh, endpoint, key, arm_command(release, delay))
    assert armed.returncode == 0 and "enabled" in armed.stdout, "delay unit not enabled"
    assert held.sampler is not None
    job, started = await timed_boot(held.op, run_id, force=True)
    bound = (DEFAULT_MAX_ATTEMPTS + 1) * (window + delay + 600)
    env = await wait_terminal(held.op, job, held.sampler, deadline_s=bound)
    elapsed = time.monotonic() - started
    after = await asyncio.to_thread(probe_new_boot, endpoint, key, before["boot_id"], KERNEL_PROBE)
    assert after.get("release") == release, f"booted {after.get('release')!r}, not {release!r}"
    marks = await held_journal(endpoint, key, guest.readiness, deadline_s=delay + 600.0)
    problems = delay_held(marks, delay, guest.readiness)
    assert not problems, f"the delay did not hold the boot: {problems}"
    return {
        "job": job,
        "status": env.status,
        "category": env.error_category,
        "elapsed_s": round(elapsed, 1),
        "journal": marks,
    }


async def held_journal(
    endpoint: Endpoint, key: Path, readiness: str, *, deadline_s: float
) -> dict[str, float]:
    """The current boot's delay marks, once the delay has ended and ``kdive-ready`` started.

    SSH is not behind the delay, so a guest can answer while the delay still runs.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        result = await asyncio.to_thread(ssh, endpoint, key, JOURNAL_PROBE)
        assert result.returncode == 0, f"guest journal read exited {result.returncode}"
        marks = delay_ordering(result.stdout)
        if {"delay_end", readiness} <= marks.keys() or time.monotonic() >= deadline:
            return marks
        await asyncio.sleep(_SAMPLE_S)


def bindings(
    candidate: str,
    *,
    root: Path | None,
    host_os: str,
    host_arch: str,
    matrix: str,
    staged: Callable[[str], Path | None] = staged_image,
    fixture_at: Fixture = load_fixture,
    owner: int = OWNER,
) -> InputBindings:
    """The expected ``Context`` of every local ``owner`` cell; null where unstaged or unbuilt."""
    entry = load_rootfs_catalog()[IMAGE]
    image = staged(IMAGE)
    kernel = bound_kernel(root, "longterm", host_arch, fixture_at)
    contexts = {
        cell.id: Context.model_validate(
            {
                "host_os": host_os,
                "host_arch": host_arch,
                "guest_os": f"{entry.distro}:{entry.version}",
                "guest_arch": entry.arch,
                "accelerator": cell.accelerator,
                "image_sha256": file_sha256(image) if image is not None else None,
                **kernel,
            }
        )
        for cell in cells("local-libvirt", owner=owner)
    }
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=contexts)


def remote_bindings(
    candidate: str,
    *,
    root: Path | None,
    matrix: str,
    host: RemoteHost,
    digest: Callable[[str, str, str], str] = volume_sha256,
    staged: Callable[[str], str | None] = staged_volume,
    fixture_at: Fixture = load_fixture,
    owner: int = OWNER,
) -> InputBindings:
    """The expected ``Context`` of every bound remote ``owner`` cell, on ``host``."""
    image = REMOTE_REPRESENTATIVES[REMOTE_FAMILY]
    volume = staged(image.name)
    kernel = bound_kernel(root, "longterm", host.host_arch, fixture_at)
    contexts = {
        cell.id: Context.model_validate(
            {
                "host_os": host.host_os,
                "host_arch": host.host_arch,
                "guest_os": f"{image.distro}:{image.version}",
                "guest_arch": image.arch,
                "accelerator": cell.accelerator,
                "image_sha256": digest(host.dest, host.pool, volume) if volume else None,
                **kernel,
            }
        )
        for cell in cells("remote-libvirt", owner=owner)
        if cell.node_id is not None
    }
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=contexts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write resource-frame cell input bindings.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("bindings")
    write.add_argument("--candidate", required=True)
    write.add_argument("--out", type=Path, required=True)
    write.add_argument("--remote", action="store_true", help="bind the remote-libvirt cells")
    write.add_argument("--owner", type=int, default=OWNER, help="the issue owning the cells")
    args = parser.parse_args(argv)
    root = Path(os.environ[FIXTURE_ROOT_ENV]) if os.environ.get(FIXTURE_ROOT_ENV) else None
    matrix = build_contract().matrix_sha256
    if args.remote:
        try:
            host = remote_host()
        except ScenarioStop as stop:
            print(f"cannot bind the remote cells: {stop}")
            return 2
        inputs = remote_bindings(
            args.candidate, root=root, matrix=matrix, host=host, owner=args.owner
        )
    else:
        inputs = bindings(
            args.candidate,
            root=root,
            host_os=os_identity(Path("/etc/os-release").read_text(encoding="utf-8")),
            host_arch=platform.machine(),
            matrix=matrix,
            owner=args.owner,
        )
    args.out.write_text(inputs.model_dump_json(indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(inputs.cells)} binding(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
