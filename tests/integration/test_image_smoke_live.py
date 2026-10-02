"""Smoke every catalog image native to this host and record coverage evidence (#2808).

``live_stack``-marked. One parameter per ``image-smoke`` contract cell whose guest architecture
is the host's. Each drives the public MCP surface over HTTP: acquire the staged catalog image,
provision it, wait for first boot, authenticate over SSH, check OS/architecture identity (and
the build toolchain on build rows), power-cycle, then release and prove owned cleanup. Every
parameter writes one version-1 ``Evidence`` record under ``KDIVE_ARTIFACT_DIR`` (ADR-0715);
``docs/operating/runbooks/live-testing.md`` covers staging, bindings, assembly and
qualification.
"""

from __future__ import annotations

import asyncio
import os
import subprocess  # noqa: S404 - fixed ssh-keygen argv  # nosec B404
import time
import xml.etree.ElementTree as ET  # noqa: S405 - the worker's own domain XML  # nosec B405
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import libvirt
import pytest

from kdive.images.rootfs.catalog import RootfsCatalogEntry, load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.providers.shared.libvirt_xml import recorded_ssh_port
from kdive.providers.shared.runtime_paths import (
    console_log_path,
    domain_name_for,
    read_console_log,
)
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Context, Outcome
from tests.integration.live_stack.cleanup import (
    capacity_in_use,
    domain_disks,
    release_and_verify,
)
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.evidence import (
    EvidenceWriter,
    RunIdentity,
    build_record,
    evidence_root,
    identity_problems,
    run_identity,
)
from tests.integration.live_stack.image_smoke import (
    PROBE,
    native_cells,
    os_matches,
    parse_probe,
    ssh,
    toolchain_command,
)
from tests.integration.live_stack.spine import (
    LOCAL_ALLOCATION_DISK_GB,
    await_system_state,
    drain_job,
    mint_role_token,
    ok,
    provision_to_ready,
    scalar,
    seed_metering,
    worker_libvirt_uri,
)
from tests.mcp.json_data import data_mapping

pytestmark = pytest.mark.live_stack

_PROJECT = "image-smoke"
_SESSION = "image-smoke-sess"
_CATALOG = load_rootfs_catalog()
_ACCELERATORS = {"kvm": "kvm", "qemu": "tcg"}


class _Stop(Exception):  # noqa: N818 - control flow, not an error type
    """End the scenario with a recorded outcome other than an assertion failure."""

    def __init__(self, outcome: Outcome, reason: str) -> None:
        super().__init__(reason)
        self.outcome = outcome


@dataclass
class _Run:
    """What one cell's scenario has proven and observed so far."""

    cell: Cell
    writer: EvidenceWriter
    outcome: Outcome = Outcome.FAILURE
    reason: str = "scenario raised before completing"
    assertions: dict[str, str] = field(default_factory=dict)
    guest_os: str | None = None
    guest_arch: str | None = None
    accelerator: str = "none"
    image_sha256: str | None = None

    def prove(self, assertion: str, observation: dict[str, object]) -> None:
        self.assertions[assertion] = self.writer.artifact(
            {"cell": self.cell.id, "assertion": assertion, **observation}
        )

    def context(self, identity: RunIdentity) -> Context:
        return Context.model_validate(
            {
                "host_os": identity.host_os,
                "host_arch": identity.host_arch,
                "guest_os": self.guest_os,
                "guest_arch": self.guest_arch,
                "accelerator": self.accelerator,
                "image_sha256": self.image_sha256,
            }
        )


def _entry(cell: Cell) -> RootfsCatalogEntry:
    return _CATALOG[str(cell.image)]


def _prerequisites() -> tuple[OidcIssuer, str]:
    """The issuer and database a configured stack needs; missing ones are a blocked cell."""
    try:
        issuer = require_issuer()
    except pytest.skip.Exception as exc:
        raise _Stop(Outcome.BLOCKED, str(exc)) from None
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    if not db_url:
        raise _Stop(Outcome.BLOCKED, "KDIVE_DATABASE_URL unset; export the server DSN")
    return issuer, db_url


async def _acquire(op: LiveStackClient, run: _Run) -> None:
    entry, cursor = _entry(run.cell), None
    while True:
        args: dict[str, object] = {"request": {"cursor": cursor}} if cursor else {}
        listing = ok(await scalar(op, "images.list", **args), "acquire")
        match = next(
            (
                item
                for item in listing.items
                if item.data.get("provider") == "local-libvirt"
                and item.data.get("name") == run.cell.image
                and item.data.get("arch") == entry.arch
            ),
            None,
        )
        cursor = listing.data.get("next_cursor") if listing.data.get("truncated") else None
        if match is not None or not cursor:
            break
    if match is None:
        raise _Stop(
            Outcome.BLOCKED,
            f"{run.cell.image} is not a registered local-libvirt image; stage it with "
            "examples/local-libvirt/build-image.sh",
        )
    described = ok(await scalar(op, "images.describe", image_id=match.object_id), "acquire")
    digest = str(described.data.get("digest", ""))
    assert described.data.get("state") == "registered", f"image state {described.data.get('state')}"
    assert digest.startswith("sha256:") and len(digest) == 71, f"image digest {digest!r}"
    run.image_sha256 = digest.removeprefix("sha256:")
    run.prove(
        "acquire",
        {"digest": digest, "arch": described.data.get("arch"), "os": described.data.get("os")},
    )


def _profile(entry: RootfsCatalogEntry, name: str) -> dict[str, object]:
    # direct-kernel boots the rootfs's own baseline kernel; kernel_source_ref is required by the
    # profile schema but never read on provision (ADR-0272).
    return {
        "schema_version": 1,
        "arch": entry.arch,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "image-smoke-unread",
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "catalog", "provider": "local-libvirt", "name": name}
            }
        },
    }


def _domain_xml(system_id: str) -> str:
    conn = libvirt.open(worker_libvirt_uri())
    try:
        return conn.lookupByName(domain_name_for(UUID(system_id))).XMLDesc(0)
    finally:
        conn.close()


def _probe(port: int, key: Path) -> dict[str, str]:
    result = ssh(port, key, PROBE)
    assert result.returncode == 0, f"ssh probe exit {result.returncode}: {result.stderr[-500:]}"
    return parse_probe(result.stdout)


async def _authorize(op: LiveStackClient, system_id: str, tmp_path: Path) -> tuple[int, Path]:
    key = tmp_path / "id_ed25519"
    subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "image-smoke", "-f", str(key)],
        check=True,
        timeout=30.0,
    )
    public = (tmp_path / "id_ed25519.pub").read_text(encoding="utf-8").strip()
    env = ok(
        await scalar(op, "systems.authorize_ssh_key", system_id=system_id, public_key=public),
        "authorize",
    )
    await drain_job(op, "authorize", env.object_id)
    ssh_info = data_mapping(
        ok(await scalar(op, "systems.ssh_info", system_id=system_id), "ssh_info"), "ssh"
    )
    port = ssh_info.get("port")
    assert ssh_info.get("host_scope") == "worker_loopback" and isinstance(port, int), ssh_info
    return port, key


async def _scenario(run: _Run, op: LiveStackClient, system_id: str, tmp_path: Path) -> None:
    entry = _entry(run.cell)
    xml = _domain_xml(system_id)
    run.accelerator = _ACCELERATORS.get(ET.fromstring(xml).get("type", ""), "none")  # noqa: S314
    assert recorded_ssh_port(xml) is not None, "no loopback SSH forward in the domain XML"
    marker = b"kdive-ready" in read_console_log(console_log_path(UUID(system_id)))
    assert marker, "System reached ready without the kdive-ready first-boot marker"
    run.prove("first-boot", {"state": "ready", "console_marker": "kdive-ready"})
    port, key = await asyncio.wait_for(_authorize(op, system_id, tmp_path), timeout=900)
    first = await asyncio.to_thread(_probe, port, key)
    assert first.get("uid") == "0", f"ssh as root reported uid {first.get('uid')!r}"
    run.prove("authenticated-access", {"user": "root", "uid": first["uid"]})
    os_release = {k: first.get(k) for k in ("ID", "VERSION_ID", "machine")}
    assert os_matches(entry, first), f"guest {os_release} is not catalog {entry.distro}"
    run.guest_os, run.guest_arch = f"{entry.distro}:{entry.version}", entry.arch
    run.prove("os-architecture", {"observed": os_release})
    if "build-toolchain" in run.cell.assertions:
        result = await asyncio.to_thread(ssh, port, key, toolchain_command(entry))
        assert result.returncode == 0, f"toolchain check exit {result.returncode}"
        run.prove("build-toolchain", {"packages_and_build": "ok"})
    env = ok(await scalar(op, "control.power", system_id=system_id, action="cycle"), "reboot")
    await drain_job(op, "reboot", env.object_id)
    await await_system_state(op, "reboot", system_id, "ready")
    second = await asyncio.to_thread(_probe, port, key)
    assert second.get("boot_id") and second["boot_id"] != first.get("boot_id"), (
        "boot_id did not change across control.power cycle"
    )
    assert second.get("uid") == "0", "ssh after reboot did not authenticate as root"
    run.prove("reboot", {"action": "cycle", "boot_id_changed": True})


async def _smoke(run: _Run, base_url: str, issuer: OidcIssuer, db_url: str, tmp: Path) -> None:
    token = mint_role_token(issuer, project=_PROJECT, agent_session=_SESSION, role="operator")
    async with LiveStackClient.over_http(base_url, token) as op:
        await seed_metering(db_url, _PROJECT)
        await _acquire(op, run)
        in_use_before = await capacity_in_use(op)
        allocation = ok(
            await scalar(
                op,
                "allocations.request",
                project=_PROJECT,
                vcpus=2,
                memory_gb=2,
                disk_gb=LOCAL_ALLOCATION_DISK_GB,
                resource={"mode": "kind"},
            ),
            "allocate",
        ).object_id
        system_id: str | None = None
        disks: list[str] = []
        cleaned = False
        try:
            profile = _profile(_entry(run.cell), str(run.cell.image))
            system_id = await provision_to_ready(
                op, allocation_id=allocation, profile=profile, phase_name="provision"
            )
            disks = domain_disks(_domain_xml(system_id))
            await _scenario(run, op, system_id, tmp)
            run.prove(
                "cleanup",
                await _cleanup(op, allocation, system_id, disks, in_use_before),
            )
            cleaned = True
        finally:
            if not cleaned:
                await _cleanup_attempt(run, op, allocation, system_id, disks, in_use_before)


async def _cleanup(
    op: LiveStackClient, allocation: str, system_id: str, disks: list[str], in_use: int
) -> dict[str, object]:
    return await release_and_verify(
        op,
        allocation_id=allocation,
        system_id=system_id,
        domain=domain_name_for(UUID(system_id)),
        disks=disks,
        in_use_before=in_use,
    )


async def _cleanup_attempt(
    run: _Run,
    op: LiveStackClient,
    allocation: str,
    system_id: str | None,
    disks: list[str],
    in_use: int,
) -> None:
    """Best-effort release after a failed scenario; the attempt is evidence, not an assertion."""
    try:
        if system_id is None:
            env = await scalar(op, "allocations.release", allocation_id=allocation)
            result: dict[str, object] = {"released": env.status}
        else:
            result = await _cleanup(op, allocation, system_id, disks, in_use)
    except Exception as exc:  # noqa: BLE001 - recorded; the scenario already failed
        result = {"error": type(exc).__name__, "detail": str(exc)[:300]}
    run.writer.artifact({"cell": run.cell.id, "cleanup-attempt": result})


@pytest.mark.parametrize("cell", native_cells(), ids=lambda cell: str(cell.image))
def test_image_smoke(cell: Cell, tmp_path: Path) -> None:
    """Acquire → first boot → authenticate → OS/arch → (toolchain) → reboot → cleanup."""
    base_url = require_stack()
    run = _Run(cell, EvidenceWriter(evidence_root()))
    started = time.monotonic()
    identity = run_identity(base_url)
    problems = identity_problems(identity, cell.roles)
    try:
        if any(not problem.startswith("missing:") for problem in problems):
            raise _Stop(Outcome.FAILURE, f"stack or checkout is not the candidate: {problems}")
        issuer, db_url = _prerequisites()
        asyncio.run(_smoke(run, base_url, issuer, db_url, tmp_path))
        run.outcome, run.reason = Outcome.SUCCESS, ""
    except _Stop as stop:
        run.outcome, run.reason = stop.outcome, str(stop)
    finally:
        run.writer.record(
            build_record(
                cell,
                identity,
                outcome=run.outcome,
                context=run.context(identity),
                duration_s=round(time.monotonic() - started, 1),
                assertions=run.assertions,
                impediments=["missing-prerequisite"] if run.outcome is Outcome.BLOCKED else [],
            )
        )
    assert run.outcome is Outcome.SUCCESS, f"{cell.id}: {run.outcome.value}: {run.reason}"
    assert not problems, f"{cell.id}: deployed identity incomplete: {problems}"
