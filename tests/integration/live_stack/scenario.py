"""The shared per-cell frame of the live coverage carriers (#2808, #2809, ADR-0715).

A carrier's scenario coroutine proves assertions on a :class:`CellRun`. :func:`run_cell` reads the
deployed identity, refuses a non-candidate stack before any mutation, runs the scenario and
always writes the cell's one evidence record. :func:`on_catalog_system` is the local-libvirt
frame: acquire a registered catalog image, provision it to ``ready``, run a body, then prove
``cleanup``. The SSH helpers and :class:`CellRun` are provider-neutral, so the remote carrier
(#2810) wraps the same bodies in its own allocation frame.
"""

from __future__ import annotations

import asyncio
import os
import subprocess  # noqa: S404 - fixed ssh-keygen argv  # nosec B404
import time
import xml.etree.ElementTree as ET  # noqa: S405 - the worker's own domain XML  # nosec B405
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from uuid import UUID

import libvirt
import pytest

from kdive.images.rootfs.catalog import RootfsCatalogEntry, load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.providers.shared.runtime_paths import domain_name_for
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
from tests.integration.live_stack.image_smoke import PROBE, Endpoint, parse_probe, ssh
from tests.integration.live_stack.spine import (
    LOCAL_ALLOCATION_DISK_GB,
    drain_job,
    mint_role_token,
    ok,
    provision_to_ready,
    scalar,
    seed_metering,
    worker_libvirt_uri,
)
from tests.mcp.json_data import data_mapping

ACCELERATORS = {"kvm": "kvm", "qemu": "tcg"}
_REBOOT_DEADLINE_S = 300.0


class ScenarioStop(Exception):  # noqa: N818 - control flow, not an error type
    """End the scenario with a recorded outcome other than an assertion failure."""

    def __init__(self, outcome: Outcome, reason: str) -> None:
        super().__init__(reason)
        self.outcome = outcome


@dataclass
class CellRun:
    """What one cell's scenario has proven and observed so far."""

    cell: Cell
    writer: EvidenceWriter
    outcome: Outcome = Outcome.FAILURE
    reason: str = "scenario raised before completing"
    assertions: dict[str, str] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    observed: dict[str, object] = field(default_factory=lambda: {"accelerator": "none"})

    def prove(self, assertion: str, observation: dict[str, object]) -> None:
        self.assertions[assertion] = self.writer.artifact(
            {"cell": self.cell.id, "assertion": assertion, **observation}
        )

    def context(self, identity: RunIdentity) -> Context:
        """The observed ``Context``: the host identity plus every field the scenario observed."""
        return Context.model_validate(
            {"host_os": identity.host_os, "host_arch": identity.host_arch, **self.observed}
        )


Scenario = Callable[[CellRun, str, OidcIssuer, str], Awaitable[None]]
CatalogBody = Callable[[LiveStackClient, str, list[str]], Awaitable[None]]


def prerequisites() -> tuple[OidcIssuer, str]:
    """The issuer and database a configured stack needs; missing ones are a blocked cell."""
    try:
        issuer = require_issuer()
    except pytest.skip.Exception as exc:
        raise ScenarioStop(Outcome.BLOCKED, str(exc)) from None
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    if not db_url:
        raise ScenarioStop(Outcome.BLOCKED, "KDIVE_DATABASE_URL unset; export the server DSN")
    return issuer, db_url


def run_cell(cell: Cell, scenario: Scenario, *, proves: Outcome = Outcome.SUCCESS) -> None:
    """Identity → prerequisites → ``scenario`` → one record; fail pytest unless ``proves``.

    ``proves`` is what a completed scenario records: ``success`` for a functional or native cell,
    ``rejection`` for a rejection cell, the outcome ``qualify`` requires of its kind (ADR-0722).
    """
    base_url = require_stack()
    run = CellRun(cell, EvidenceWriter(evidence_root()))
    started = time.monotonic()
    identity = run_identity(base_url)
    problems = identity_problems(identity, cell.roles)
    try:
        if any(not problem.startswith("missing:") for problem in problems):
            raise ScenarioStop(
                Outcome.FAILURE, f"stack or checkout is not the candidate: {problems}"
            )
        issuer, db_url = prerequisites()
        asyncio.run(scenario(run, base_url, issuer, db_url))
        run.outcome, run.reason = proves, ""
    except ScenarioStop as stop:
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
                artifacts=run.artifacts,
            )
        )
    assert run.outcome is proves, f"{cell.id}: {run.outcome.value}: {run.reason}"
    assert not problems, f"{cell.id}: deployed identity incomplete: {problems}"


async def acquire_image(op: LiveStackClient, name: str, arch: str) -> dict[str, object]:
    """``images.describe`` facts of registered local-libvirt image ``name``; blocked if absent."""
    cursor = None
    while True:
        args: dict[str, object] = {"request": {"cursor": cursor}} if cursor else {}
        listing = ok(await scalar(op, "images.list", **args), "acquire")
        match = next(
            (
                item
                for item in listing.items
                if item.data.get("provider") == "local-libvirt"
                and item.data.get("name") == name
                and item.data.get("arch") == arch
            ),
            None,
        )
        cursor = listing.data.get("next_cursor") if listing.data.get("truncated") else None
        if match is not None or not cursor:
            break
    if match is None:
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"{name} is not a registered local-libvirt image; stage it with "
            "examples/local-libvirt/build-image.sh",
        )
    described = ok(await scalar(op, "images.describe", image_id=match.object_id), "acquire")
    digest = str(described.data.get("digest", ""))
    assert described.data.get("state") == "registered", f"image state {described.data.get('state')}"
    assert digest.startswith("sha256:") and len(digest) == 71, f"image digest {digest!r}"
    return {"digest": digest, "arch": described.data.get("arch"), "os": described.data.get("os")}


def catalog_profile(entry: RootfsCatalogEntry, name: str, unread_ref: str) -> dict[str, object]:
    """A direct-kernel provisioning profile booting catalog image ``name``.

    ``kernel_source_ref`` is required by the profile schema but never read on provision
    (ADR-0272); ``unread_ref`` only names the carrier.
    """
    return {
        "schema_version": 1,
        "arch": entry.arch,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": unread_ref,
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "catalog", "provider": "local-libvirt", "name": name}
            }
        },
    }


Provision = Callable[[LiveStackClient, str, dict[str, object]], Awaitable[str]]


async def provision_catalog(
    op: LiveStackClient, allocation_id: str, profile: dict[str, object]
) -> str:
    """Provision ``profile`` on ``allocation_id`` with the frame's client and wait for ``ready``."""
    return await provision_to_ready(
        op, allocation_id=allocation_id, profile=profile, phase_name="provision"
    )


def domain_xml(system_id: str) -> str:
    conn = libvirt.open(worker_libvirt_uri())
    try:
        return conn.lookupByName(domain_name_for(UUID(system_id))).XMLDesc(0)
    finally:
        conn.close()


def ssh_probe(endpoint: Endpoint, key: Path, command: str = PROBE) -> dict[str, str]:
    """Run ``command`` as root and parse its ``KEY=VALUE`` output; a non-zero exit fails."""
    result = ssh(endpoint, key, command)
    assert result.returncode == 0, f"ssh probe exit {result.returncode}: {result.stderr[-500:]}"
    return parse_probe(result.stdout)


def probe_new_boot(
    endpoint: Endpoint, key: Path, boot_id: str, command: str = PROBE
) -> dict[str, str]:
    """Probe until the guest reports a ``boot_id`` other than ``boot_id``.

    A drained reboot job (``control.power`` ``cycle``, ``runs.boot``) can precede the old boot
    going away, so the old boot can still answer SSH for a while afterwards.
    ``command`` must be a read-only, repeatable probe: transport failures and per-attempt
    timeouts retry within the existing reboot readiness budget.
    """
    deadline = time.monotonic() + _REBOOT_DEADLINE_S
    last = "no probe completed"
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"reboot SSH readiness exceeded {_REBOOT_DEADLINE_S:.0f} s: {last}"
        try:
            result = ssh(endpoint, key, command, deadline_s=0, timeout_s=min(120.0, remaining))
        except subprocess.TimeoutExpired:
            last = "SSH probe timed out"
        else:
            if result.returncode == 255:
                last = "SSH transport unavailable"
            else:
                assert result.returncode == 0, (
                    f"ssh probe exit {result.returncode}: {result.stderr[-500:]}"
                )
                probe = parse_probe(result.stdout)
                assert probe.get("boot_id"), "SSH probe returned no boot_id"
                if probe["boot_id"] != boot_id:
                    return probe
                last = "boot_id unchanged"
        time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))


async def ssh_endpoint(op: LiveStackClient, system_id: str) -> Endpoint:
    """The SSH host and port ``systems.ssh_info`` returns for ``system_id``."""
    info = data_mapping(
        ok(await scalar(op, "systems.ssh_info", system_id=system_id), "ssh_info"), "ssh"
    )
    host, port = info.get("host"), info.get("port")
    assert info.get("host_scope") == "worker_loopback", info
    assert isinstance(host, str) and isinstance(port, int), info
    return Endpoint(host, port)


async def authorize_ssh(
    op: LiveStackClient, system_id: str, directory: Path, comment: str
) -> tuple[Endpoint, Path]:
    """Authorize a fresh ed25519 key for root; return the SSH endpoint and private key path."""
    key = directory / "id_ed25519"
    subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key)],
        check=True,
        timeout=30.0,
    )
    public = (directory / "id_ed25519.pub").read_text(encoding="utf-8").strip()
    env = ok(
        await scalar(op, "systems.authorize_ssh_key", system_id=system_id, public_key=public),
        "authorize",
    )
    await drain_job(op, "authorize", env.object_id)
    return await ssh_endpoint(op, system_id), key


async def on_catalog_system(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    image: str,
    body: CatalogBody,
    provision: Provision = provision_catalog,
) -> None:
    """Acquire ``image``, provision it to ``ready``, run ``body``, then prove ``cleanup``.

    ``body(op, system_id, owned)`` may append host paths it made the System own; cleanup proves
    them absent with the domain's disks. On failure the cleanup attempt is recorded instead.
    ``provision`` creates the System; a cell whose tool under test is the provision passes its
    own.
    """
    entry = load_rootfs_catalog()[image]
    token = mint_role_token(
        issuer, project=project, agent_session=f"{project}-sess", role="operator"
    )
    async with LiveStackClient.over_http(base_url, token) as op:
        await seed_metering(db_url, project)
        acquired = await acquire_image(op, image, entry.arch)
        run.observed["image_sha256"] = str(acquired["digest"]).removeprefix("sha256:")
        if "acquire" in run.cell.assertions:  # image smoke proves acquisition itself
            run.prove("acquire", acquired)
        in_use_before = await capacity_in_use(op)
        allocation = ok(
            await scalar(
                op,
                "allocations.request",
                project=project,
                vcpus=2,
                memory_gb=2,
                disk_gb=LOCAL_ALLOCATION_DISK_GB,
                resource={"mode": "kind"},
            ),
            "allocate",
        ).object_id
        system_id: str | None = None
        owned: list[str] = []
        cleaned = False
        try:
            system_id = await provision(
                op, allocation, catalog_profile(entry, image, f"{project}-unread")
            )
            xml = domain_xml(system_id)
            accelerator = ET.fromstring(xml).get("type", "")  # noqa: S314  # nosec B314
            run.observed["accelerator"] = ACCELERATORS.get(accelerator, "none")
            owned = domain_disks(xml)
            await body(op, system_id, owned)
            run.prove("cleanup", await _cleanup(op, allocation, system_id, owned, in_use_before))
            cleaned = True
        finally:
            if not cleaned:
                await cleanup_attempt(
                    run,
                    op,
                    allocation,
                    None
                    if system_id is None
                    else partial(_cleanup, op, allocation, system_id, owned, in_use_before),
                )


async def _cleanup(
    op: LiveStackClient, allocation: str, system_id: str, owned: list[str], in_use: int
) -> dict[str, object]:
    return await release_and_verify(
        op,
        allocation_id=allocation,
        system_id=system_id,
        domain=domain_name_for(UUID(system_id)),
        disks=owned,
        in_use_before=in_use,
    )


async def cleanup_attempt(
    run: CellRun,
    op: LiveStackClient,
    allocation: str,
    cleanup: Callable[[], Awaitable[dict[str, object]]] | None,
) -> None:
    """Best-effort release after a failed scenario; the attempt is evidence, not an assertion.

    ``cleanup`` is the frame's owned-resource proof, or ``None`` before a System existed.
    """
    try:
        if cleanup is None:
            env = await scalar(op, "allocations.release", allocation_id=allocation)
            result: dict[str, object] = {"released": env.status}
        else:
            result = await cleanup()
    except Exception as exc:  # noqa: BLE001 - recorded; the scenario already failed
        # The type only: a message can carry host paths or a libvirt URI (ADR-0715 evidence).
        result = {"error": type(exc).__name__}
    run.artifacts.append(run.writer.artifact({"cell": run.cell.id, "cleanup-attempt": result}))
