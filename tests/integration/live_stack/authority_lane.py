"""Authority-owned System frame of the HTTP tool cells (#3184, ADR-0722, ADR-0715).

:func:`on_authority_system` is the third System frame beside
:func:`~tests.integration.live_stack.tool_cells.on_lane_system` and
:func:`~tests.integration.live_stack.tool_cells.lane_target`. A carrier selects it explicitly. It
runs on the installed local-authority lane that ``CONFIG_ENV`` names, where the System's domain
lives on the authority's private session daemon and its artifacts under the private roots, so it
observes there and never through SSH: an authority System refuses ordinary mutation, including
``systems.authorize_ssh_key``, before its first activation.

``system_mode`` ``fixture`` provisions a System through HTTP with the fixed pinned rootfs, then
moves it onto the private daemon with ``scripts/live-vm/provision-authority-fixture.py``.
``public`` stops ``blocked`` before any mutation (#3144, #3148); a remote cell stops ``blocked``
too (#3209).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import libvirt
import psycopg
from pydantic import BaseModel, ConfigDict, Field

from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.providers.local_libvirt.config import local_authority_instance_for_resource
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.services.investigations.common import TERMINAL_INVESTIGATION_STATES
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.cleanup import (
    _await_released,  # the release wait of a frame that never got a System
    capacity_in_use,
    domain_disks,
    release_and_verify,
)
from tests.integration.live_stack.scenario import (
    ACCELERATORS,
    CellRun,
    ScenarioStop,
    acquire_image,
    domain_xml,
)
from tests.integration.live_stack.spine import (
    DRAIN_DEADLINE_S,
    LOCAL_ALLOCATION_DISK_GB,
    POLL_INTERVAL_S,
    await_system_state,
    mint_role_token,
    ok,
    scalar,
    seed_metering,
    worker_libvirt_uri,
)
from tests.integration.live_stack.tool_cells import lane_image
from tests.live_vm.installed_local_authority_support import (
    NativeAuthorityConfig,
    OwnedResource,
    ResourceLedger,
    provision_authority_fixture,
    read_protected_config,
    remove_authority_fixture,
    require_authority_artifact_confinement,
    require_deployed_revision,
    require_evidence_read_access,
    require_installed_authority_routes,
)
from tests.mcp.json_data import data_str

# Test-only input, unprefixed so env-docs-check does not require it in the product catalog.
CONFIG_ENV = "LOCAL_AUTHORITY_LANE_CONFIG"
SYSTEMS_TOML_ENV = "KDIVE_SYSTEMS_TOML"
# The fixed worker input the fixture script copies onto the private daemon.
FIXTURE_ROOTFS = "/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2"
_AUTHORITY_PYTHON = "/opt/kdive-provider-authority/.venv/bin/python"
_REVISION = "/opt/kdive-provider-authority/revision"
_MAX_INVENTORY_BYTES = 1_048_576
_TERMINAL = frozenset({"succeeded", "failed", "canceled"})
_AUTHORITY_ROOTFS = "/var/lib/kdive/provider-authority/rootfs"
_AUTHORITY_CONSOLE = "/var/lib/kdive/provider-authority/console"
REMOTE_BLOCKED = "the remote authority-owned System frame is #3209"
PUBLIC_BLOCKED = (
    "public authority-owned provisioning is blocked by #3144 (the authority answers "
    "provider-conflict after it defines the domain and fences the allocation from release) and "
    "#3148 (the local System identity never matches libvirt's machine name); "
    "set system_mode to fixture"
)
ROUTED_BLOCKED = (
    "fixture mode needs the lane's plain local-libvirt resource without an authority_instance; "
    "an authority-routed resource makes systems.provision the blocked public path (#3144, #3148)"
)
NO_PLAIN_BLOCKED = (
    "the lane inventory routes every [[local_libvirt]] entry through an authority_instance; "
    "run fixture mode on the lane's fixture-mode inventory (live-testing runbook)"
)
# Every non-terminal job that names the System or one of its Runs.
_OPEN_JOBS = (
    "SELECT id::text FROM jobs WHERE state NOT IN ('succeeded', 'failed', 'canceled') "
    "AND (payload->>'system_id' = %(system)s OR payload->>'run_id' IN "
    "(SELECT id::text FROM runs WHERE system_id = %(system)s::uuid)) ORDER BY created_at"
)

# Root runs this with the authority's interpreter in isolated mode; its only argument is a UUID.
_INVENTORY = """
import json
import os
import sys
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring

if len(sys.argv) > 2:
    raise SystemExit("inventory takes at most one System UUID")
name = "kdive-" + str(UUID(sys.argv[1])) if len(sys.argv) == 2 else None
roots = ("/var/lib/kdive/provider-authority/rootfs", "/var/lib/kdive/provider-authority/console")
conn = libvirt.open("qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock")
if conn is None:
    raise SystemExit("authority daemon connection returned no handle")
try:
    domains = sorted(domain.name() for domain in conn.listAllDomains(0))
    system = None
    if name in domains:
        domain = conn.lookupByName(name)
        kind = fromstring(domain.XMLDesc(0)).get("type", "")
        system = {"active": domain.isActive() == 1, "type": kind}
finally:
    conn.close()
listing = {root: sorted(os.listdir(root)) for root in roots}
print(json.dumps({"domains": domains, "roots": listing, "system": system}))
"""


class AuthorityLaneConfig(BaseModel):
    """Non-secret operator inputs of the installed local-authority lane."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    installed_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    authority_service: Literal["kdive-external-boot-authority.service"]
    system_mode: Literal["fixture", "public"]


def load_lane_config(environment: Mapping[str, str] | None = None) -> AuthorityLaneConfig | None:
    """The lane config ``CONFIG_ENV`` names, or ``None`` when it is unset."""
    raw = (os.environ if environment is None else environment).get(CONFIG_ENV)
    if raw is None:
        return None
    data = read_protected_config(raw, "local authority lane config")
    return AuthorityLaneConfig.model_validate_json(data)


def _names(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise AssertionError(f"authority inventory {what} is malformed")
    return [str(name) for name in value]


@dataclass(frozen=True)
class DomainFacts:
    """The System's domain on the authority daemon: running, and its libvirt domain type."""

    active: bool
    kind: str


@dataclass(frozen=True)
class AuthorityInventory:
    """The authority daemon's domain names, private-root entries and one System's domain."""

    domains: list[str]
    roots: dict[str, list[str]]
    system: DomainFacts | None


def _domain_facts(value: object) -> DomainFacts | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AssertionError("authority inventory System facts are malformed")
    active, kind = value.get("active"), value.get("type")
    if not isinstance(active, bool) or not isinstance(kind, str):
        raise AssertionError("authority inventory System facts are malformed")
    return DomainFacts(active, kind)


def authority_inventory(system_id: str | None = None) -> AuthorityInventory:
    """The authority daemon's domains, the private roots' entries and the System's domain facts."""
    argv = ["sudo", "-n", _AUTHORITY_PYTHON, "-I", "-c", _INVENTORY]
    if system_id is not None:
        argv.append(str(UUID(system_id)))
    result = subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
        argv, capture_output=True, text=True, timeout=120.0, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"authority inventory exited {result.returncode}")
    if len(result.stdout) > _MAX_INVENTORY_BYTES:
        raise AssertionError("authority inventory output exceeds its bound")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or set(data) != {"domains", "roots", "system"}:
        raise AssertionError("authority inventory is malformed")
    roots = data["roots"]
    if not isinstance(roots, dict):
        raise AssertionError("authority inventory roots are malformed")
    return AuthorityInventory(
        _names(data["domains"], "domains"),
        {str(root): _names(names, "root") for root, names in roots.items()},
        _domain_facts(data["system"]),
    )


def worker_domains() -> list[str]:
    """Every domain the worker's libvirt defines."""
    conn = libvirt.open(worker_libvirt_uri())
    try:
        return sorted(domain.name() for domain in conn.listAllDomains(0))
    finally:
        conn.close()


def lane_inventory() -> dict[str, object]:
    """What the frame must leave as it found it: both daemons' domains and the private roots."""
    authority = authority_inventory()
    return {
        "authority_domains": authority.domains,
        "authority_roots": authority.roots,
        "worker_domains": worker_domains(),
    }


def _output(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
        argv, capture_output=True, text=True, timeout=60.0, check=False
    )


def require_lane(config: AuthorityLaneConfig, base_url: str) -> str:
    """Refuse unless the lane runs the configured candidate; return the running worker units."""
    active = _output("systemctl", "is-active", config.authority_service).stdout.strip()
    assert active == "active", f"{config.authority_service} is {active!r}, not active"
    installed = _output("sudo", "-n", "cat", _REVISION)
    assert installed.returncode == 0, "cannot read the installed authority revision with sudo -n"
    assert installed.stdout.strip() == config.installed_revision, (
        "the installed authority revision does not match the lane config"
    )
    running = _output(
        "systemctl", "list-units", "kdive-live-worker@*.service", "--state=running", "--no-legend"
    ).stdout.strip()
    require_deployed_revision(config, base_url, running)
    require_installed_authority_routes(config, running)
    return running


def fixture_profile(arch: str, digest: str, unread_ref: str) -> dict[str, object]:
    """A direct-kernel profile booting the fixed worker rootfs pinned to ``digest``."""
    return {
        "schema_version": 1,
        "arch": arch,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": unread_ref,
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "local", "path": FIXTURE_ROOTFS, "sha256": digest},
                "crashkernel": "256M",
            }
        },
    }


async def require_root_provenance(db_url: str, system_id: str, digest: str, arch: str) -> None:
    """Require admission to have bound the System's root to the registered image."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT image_digest, architecture FROM system_root_provenance WHERE system_id = %s",
            (system_id,),
        )
        row = await cursor.fetchone()
    assert row is not None, "the System has no root provenance; stage the registered image bytes"
    assert (row[0], row[1]) == (digest, arch), "the System's root provenance is another image"


async def _open_jobs(db_url: str, system_id: str) -> list[str]:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(_OPEN_JOBS, {"system": system_id})
        return [str(row[0]) for row in await cursor.fetchall()]


async def drain_system_jobs(
    op: LiveStackClient, db_url: str, system_id: str, *, deadline_s: float = DRAIN_DEADLINE_S
) -> list[str]:
    """Wait until no job of the System or its Runs is open; return the terminal states seen.

    The query repeats until it finds nothing, so a job queued during the drain is waited on too.
    """
    deadline = time.monotonic() + deadline_s
    states: list[str] = []
    while open_jobs := await _open_jobs(db_url, system_id):
        for job_id in open_jobs:
            env = await scalar(op, "jobs.wait", job_id=job_id, timeout_s=60.0)
            if env.status in _TERMINAL:
                states.append(env.status)
        assert time.monotonic() < deadline, f"job(s) of System {system_id} stayed open"
        await asyncio.sleep(POLL_INTERVAL_S)
    return states


def fixture_names(system_id: str) -> list[str]:
    """The five names a fixture create makes for ``system_id``, in creation order (runbook)."""
    return [
        f"{_AUTHORITY_ROOTFS}/{system_id}-fixture-base.qcow2",
        f"{_AUTHORITY_ROOTFS}/{system_id}-baseline",
        f"{_AUTHORITY_ROOTFS}/{system_id}-overlay.qcow2",
        f"{_AUTHORITY_CONSOLE}/{system_id}.log",
        domain_name_for(UUID(system_id)),
    ]


async def allocated_resource(db_url: str, allocation_id: str) -> str:
    """The name of the Resource ``allocation_id`` holds."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT r.name FROM allocations a JOIN resources r ON r.id = a.resource_id "
            "WHERE a.id = %s",
            (allocation_id,),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0], f"allocation {allocation_id} holds no named resource"
    return str(row[0])


@dataclass(frozen=True)
class AuthoritySystem:
    """A ``ready`` authority-owned System as the frame's body sees it.

    ``op`` is the frame's own client with the project's ``admin`` role, which teardown needs; a
    body proves a tool through its cell's role-scoped caller, never through ``op``.
    ``config`` and ``ledger`` are the invocation's: a body records each Investigation it opens in
    ``ledger`` (the frame closes any still open). A body releases every external boot it starts,
    in a ``finally`` block: the frame does not, and a held activation fails its cleanup.
    """

    op: LiveStackClient
    project: str
    allocation_id: str
    system_id: str
    config: NativeAuthorityConfig
    ledger: ResourceLedger


AuthorityBody = Callable[[AuthoritySystem], Awaitable[dict[str, object]]]


@dataclass
class _Frame:
    """What one invocation owns so far; cleanup removes exactly this."""

    op: LiveStackClient
    db_url: str
    project: str
    allocation: str
    in_use: int
    before: dict[str, object]
    system_id: str | None = None
    worker_disks: list[str] = field(default_factory=list)
    fixture: NativeAuthorityConfig | None = None
    ledger: ResourceLedger | None = None


def select_lane(run: CellRun) -> AuthorityLaneConfig:
    """The runnable lane config, or a ``blocked`` stop before anything is read or written."""
    if run.cell.provider == "remote-libvirt":
        raise ScenarioStop(Outcome.BLOCKED, REMOTE_BLOCKED)
    config = load_lane_config()
    if config is None:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{CONFIG_ENV} unset; name the installed local-authority lane config"
        )
    if config.system_mode == "public":
        raise ScenarioStop(Outcome.BLOCKED, PUBLIC_BLOCKED)
    # The routed-resource guard reads the lane's inventory; the test session sandboxes the XDG
    # default and keeps only an exported path (tests/conftest.py), so an unset path would pass it.
    inventory = os.environ.get(SYSTEMS_TOML_ENV, "")
    if not inventory or not Path(inventory).is_file():
        raise ScenarioStop(Outcome.BLOCKED, f"{SYSTEMS_TOML_ENV} must name the lane's systems.toml")
    # One local-libvirt resource exists per worker libvirt endpoint, so fixture mode needs the
    # lane's fixture-mode inventory; refusing here stops before any allocation is requested.
    try:
        local = load_inventory(Path(inventory)).local_libvirt
    except InventoryError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{SYSTEMS_TOML_ENV} is invalid: {exc}") from exc
    if local and all(entry.authority_instance for entry in local):
        raise ScenarioStop(Outcome.BLOCKED, NO_PLAIN_BLOCKED)
    return config


def _digest(state: object) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


async def on_authority_system(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    body: AuthorityBody,
) -> None:
    """Create an authority-owned System in ``project``, run ``body``, then prove ``cleanup``.

    ``body`` returns the ``effect`` observation; the frame adds the exposure. Cleanup runs
    whether or not ``body`` raised, including on interruption.
    """
    lane = select_lane(run)
    name, entry = lane_image()
    running = await asyncio.to_thread(require_lane, lane, base_url)
    await require_evidence_read_access(db_url)
    before = await asyncio.to_thread(lane_inventory)
    # systems.teardown, which cleanup needs, is an admin operation.
    token = mint_role_token(issuer, project=project, agent_session=f"{project}-sess", role="admin")
    async with LiveStackClient.over_http(base_url, token) as op:
        await seed_metering(db_url, project)
        digest = str((await acquire_image(op, name, entry.arch))["digest"])
        run.observed |= {
            "image_sha256": digest.removeprefix("sha256:"),
            "guest_os": f"{entry.distro}:{entry.version}",
            "guest_arch": entry.arch,
        }
        in_use = await capacity_in_use(op)
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
        frame = _Frame(op, db_url, project, allocation, in_use, before)
        try:
            profile = fixture_profile(entry.arch, digest, f"{project}-unread")
            observed = await _create_and_run(run, frame, lane, running, profile, digest, body)
        except BaseException as primary:
            await _cleanup_after_failure(run, frame, primary)
            raise
        run.prove("effect", {"exposure": run.cell.exposure, **observed})
        run.prove("cleanup", await _cleanup(frame))


async def _move_to_authority(frame: _Frame, config: NativeAuthorityConfig) -> None:
    """Run the fixture create and record its names; a failed create records the fixed five."""
    assert frame.ledger is not None and frame.system_id is not None
    names = fixture_names(frame.system_id)
    created = ResourceLedger(config.ownership_prefix, fixture_system=config.system_id)
    try:
        await provision_authority_fixture(frame.db_url, config, created)
    except BaseException:
        for identity in names:
            frame.ledger.record(OwnedResource(kind="authority-fixture", identity=identity))
        raise
    reported = [resource.identity for resource in created.resources]
    for identity in reported:
        frame.ledger.record(OwnedResource(kind="authority-fixture", identity=identity))
    assert reported == names, "the fixture create reported names other than its five"


async def _create_and_run(
    run: CellRun,
    frame: _Frame,
    lane: AuthorityLaneConfig,
    running: str,
    profile: dict[str, object],
    digest: str,
    body: AuthorityBody,
) -> dict[str, object]:
    """Provision through HTTP, move onto the private daemon, gate, observe, then run ``body``."""
    op = frame.op
    resource = await allocated_resource(frame.db_url, frame.allocation)
    if local_authority_instance_for_resource(resource) is not None:
        raise ScenarioStop(Outcome.BLOCKED, ROUTED_BLOCKED)
    provisioned = ok(
        await scalar(op, "systems.provision", allocation_id=frame.allocation, profile=profile),
        "provision",
    )
    system_id = frame.system_id = data_str(provisioned, "system_id")
    await await_system_state(op, "provision", system_id, "ready")
    frame.worker_disks = domain_disks(await asyncio.to_thread(domain_xml, system_id))
    await require_root_provenance(frame.db_url, system_id, digest, str(profile["arch"]))
    prefix = f"kdive-2151-{lane.installed_revision[:12]}-{secrets.token_hex(4)}"
    config = NativeAuthorityConfig(
        installed_revision=lane.installed_revision,
        system_id=UUID(system_id),
        project=frame.project,
        ownership_prefix=prefix,
        authority_service=lane.authority_service,
    )
    frame.fixture, frame.ledger = config, ResourceLedger(prefix, fixture_system=config.system_id)
    await _move_to_authority(frame, config)
    await asyncio.to_thread(require_authority_artifact_confinement, config, running)
    verify = config.model_copy(update={"fixture_mode": "verify-existing"})
    await provision_authority_fixture(frame.db_url, verify, frame.ledger)
    facts = (await asyncio.to_thread(authority_inventory, system_id)).system
    assert facts is not None and facts.active, (
        "the System's domain is not running on the authority daemon"
    )
    run.observed["accelerator"] = ACCELERATORS.get(facts.kind, "none")
    return await body(
        AuthoritySystem(op, frame.project, frame.allocation, system_id, config, frame.ledger)
    )


async def _close_investigations(op: LiveStackClient, ledger: ResourceLedger) -> None:
    for resource in reversed(ledger.resources):
        if resource.kind != "investigation":
            continue
        current = await scalar(op, "investigations.get", investigation_id=resource.identity)
        if current.status in TERMINAL_INVESTIGATION_STATES:
            continue
        ok(
            await scalar(
                op,
                "investigations.close",
                investigation_id=resource.identity,
                summary="Authority System frame cleanup",
            ),
            "close-investigation",
        )


async def _cleanup(frame: _Frame) -> dict[str, object]:
    """Drain, close, remove the fixture, tear down, release, then compare the inventory."""
    op = frame.op
    owned = [frame.allocation]
    if frame.system_id is None:
        ok(await scalar(op, "allocations.release", allocation_id=frame.allocation), "release")
        await _await_released(op, frame.allocation, DRAIN_DEADLINE_S, POLL_INTERVAL_S)
        in_use = await capacity_in_use(op)
        assert in_use == frame.in_use, f"capacity in use is {in_use}, not {frame.in_use}"
        released: dict[str, object] = {"allocation": "released", "in_use": in_use}
        drained: list[str] = []
    else:
        drained = await drain_system_jobs(op, frame.db_url, frame.system_id)
        if frame.fixture is not None and frame.ledger is not None:
            await _close_investigations(op, frame.ledger)
            owned += [r.identity for r in frame.ledger.resources if r.kind == "authority-fixture"]
            await asyncio.to_thread(remove_authority_fixture, frame.fixture, frame.ledger)
        owned.append(frame.system_id)
        ok(await scalar(op, "systems.teardown", system_id=frame.system_id), "teardown")
        await await_system_state(op, "teardown", frame.system_id, "torn_down")
        released = await release_and_verify(
            op,
            allocation_id=frame.allocation,
            system_id=frame.system_id,
            domain=domain_name_for(UUID(frame.system_id)),
            disks=frame.worker_disks,
            in_use_before=frame.in_use,
        )
    after = await asyncio.to_thread(lane_inventory)
    changed = sorted(key for key in after if after[key] != frame.before.get(key))
    assert not changed, f"the authority lane inventory changed in {changed}"
    return {
        **released,
        "jobs_drained": sorted(drained),
        "owned": owned,
        "inventory_sha256": _digest(after),
    }


async def _cleanup_after_failure(run: CellRun, frame: _Frame, primary: BaseException) -> None:
    """Clean up after a failed creation or body; a failed cleanup joins ``primary``."""
    try:
        result = await _cleanup(frame)
    except Exception as cleanup:  # noqa: BLE001 - grouped with the primary failure below
        # The type only: a message can carry host paths or a libvirt URI (ADR-0715 evidence).
        attempt: dict[str, object] = {"error": type(cleanup).__name__}
        run.artifacts.append(run.writer.artifact({"cell": run.cell.id, "cleanup-attempt": attempt}))
        # A KeyboardInterrupt or cancellation stays a BaseExceptionGroup; two errors narrow to
        # an ExceptionGroup.
        raise BaseExceptionGroup(
            "authority frame and cleanup failures", [primary, cleanup]
        ) from None
    run.artifacts.append(run.writer.artifact({"cell": run.cell.id, "cleanup-attempt": result}))
