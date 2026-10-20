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
``public`` stops ``blocked`` before any mutation (#3144, #3148).

A ``remote-libvirt`` cell runs the remote branch (#3209) on the installed remote authority that
``REMOTE_CONFIG_ENV`` names: public HTTP provisioning on the authority-routed remote resource,
observed through a fixed program run as the authority account over the operator's bounded SSH
seam on the authority's private daemon and storage pool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import stat
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import libvirt
import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kdive.domain.errors import CategorizedError
from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.providers.local_libvirt.config import local_authority_instance_for_resource
from kdive.providers.remote_libvirt.config import remote_config_for_resource
from kdive.providers.shared.runtime_paths import console_boot_marker_path, domain_name_for
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
    REMOTE_ALLOCATION_DISK_GB,
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
from tests.live_vm.installed_remote_authority_support import (
    CONFIG_ENV as REMOTE_CONFIG_ENV,
)
from tests.live_vm.installed_remote_authority_support import (
    RemoteAuthorityInventory,
    RemoteAuthorityProofConfig,
    remote_authority_inventory,
    require_installed_authority,
)
from tests.live_vm.installed_remote_authority_support import (
    load_config as load_remote_config,
)
from tests.mcp.json_data import data_str

# Test-only input, unprefixed so env-docs-check does not require it in the product catalog.
CONFIG_ENV = "LOCAL_AUTHORITY_LANE_CONFIG"
SYSTEMS_TOML_ENV = "KDIVE_SYSTEMS_TOML"
# The fixed worker input the fixture script copies onto the private daemon.
FIXTURE_ROOTFS = "/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2"
_COLLECTOR_REVISION = Path("/opt/kdive-authority-inventory/revision")
_REVISION = "/opt/kdive-provider-authority/revision"
_MAX_INVENTORY_BYTES = 1_048_576
_TERMINAL = frozenset({"succeeded", "failed", "canceled"})
_AUTHORITY_ROOTFS = "/var/lib/kdive/provider-authority/rootfs"
_AUTHORITY_CONSOLE = "/var/lib/kdive/provider-authority/console"
REMOTE_ROUTED_BLOCKED = (
    "the remote frame needs the proof config's resource_name declared in the lane's systems.toml "
    "with the config's authority_instance as its authority route"
)
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
# Catalog images admission can bind as an authority-owned remote System's root (root_provenance).
_REMOTE_ROOTS = (
    "SELECT name, digest FROM image_catalog WHERE provider = 'remote-libvirt' AND arch = %s "
    "AND state = 'registered' AND visibility = 'public' AND digest IS NOT NULL "
    "AND provenance->'root_spec' IS NOT NULL ORDER BY name"
)


class AuthorityLaneConfig(BaseModel):
    """Non-secret operator inputs of the installed local-authority lane."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    installed_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    authority_service: Literal["kdive-external-boot-authority.service"]
    system_mode: Literal["fixture", "public"]
    barrier_socket: Path | None = None

    @field_validator("barrier_socket")
    @classmethod
    def validate_barrier(cls, value: Path | None) -> Path | None:
        """Admit only the fixed authority proof socket (ADR-0622)."""
        return NativeAuthorityConfig.validate_barrier(value)


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
    if not isinstance(value, dict) or set(value) != {"active", "type"}:
        raise AssertionError("authority inventory System facts are malformed")
    active, kind = value.get("active"), value.get("type")
    if not isinstance(active, bool) or not isinstance(kind, str) or not kind:
        raise AssertionError("authority inventory System facts are malformed")
    return DomainFacts(active, kind)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate inventory key")
        result[key] = value
    return result


def authority_inventory(system_id: str | None = None) -> AuthorityInventory:
    """The authority daemon's domains, the private roots' entries and the System's domain facts."""
    argv = [
        "sudo",
        "-n",
        "-u",
        "kdive-authority-observer",
        "--",
        "/usr/bin/python3",
        "-I",
        "-S",
        "/usr/libexec/kdive-authority-inventory-client.py",
    ]
    if system_id is not None:
        system_id = str(UUID(system_id))
        argv.append(system_id)
    result = subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
        argv, capture_output=True, text=True, timeout=120.0, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"authority inventory exited {result.returncode}")
    if len(result.stdout.encode("utf-8")) > _MAX_INVENTORY_BYTES:
        raise AssertionError("authority inventory output exceeds its bound")
    try:
        data = json.loads(result.stdout, object_pairs_hook=_unique_keys)
    except ValueError as exc:
        raise AssertionError("authority inventory is malformed") from exc
    if not isinstance(data, dict) or set(data) != {"domains", "roots", "system"}:
        raise AssertionError("authority inventory is malformed")
    roots = data["roots"]
    if not isinstance(roots, dict) or set(roots) != {_AUTHORITY_ROOTFS, _AUTHORITY_CONSOLE}:
        raise AssertionError("authority inventory roots are malformed")
    domains = _names(data["domains"], "domains")
    facts = _domain_facts(data["system"])
    selected_exists = system_id is not None and "kdive-" + system_id in domains
    if selected_exists != (facts is not None):
        raise AssertionError("authority inventory System selection is malformed")
    return AuthorityInventory(
        domains,
        {str(root): _names(names, "root") for root, names in roots.items()},
        facts,
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


def _running_workers() -> str:
    return _output(
        "systemctl", "list-units", "kdive-live-worker@*.service", "--state=running", "--no-legend"
    ).stdout.strip()


def require_lane(config: AuthorityLaneConfig, base_url: str) -> str:
    """Refuse unless the lane runs the configured candidate; return the running worker units."""
    active = _output("systemctl", "is-active", config.authority_service).stdout.strip()
    assert active == "active", f"{config.authority_service} is {active!r}, not active"
    installed = _output("sudo", "-n", "cat", _REVISION)
    assert installed.returncode == 0, "cannot read the installed authority revision with sudo -n"
    assert installed.stdout.strip() == config.installed_revision, (
        "the installed authority revision does not match the lane config"
    )
    metadata = _COLLECTOR_REVISION.lstat()
    assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0, (
        "collector revision must be a root-owned regular file"
    )
    assert stat.S_IMODE(metadata.st_mode) == 0o444, "collector revision must be read-only"
    assert _COLLECTOR_REVISION.read_text().strip() == config.installed_revision, (
        "the installed collector revision does not match the lane config"
    )
    running = _running_workers()
    require_deployed_revision(config, base_url, running)
    require_installed_authority_routes(config, running)
    return running


def require_remote_lane(config: RemoteAuthorityProofConfig, base_url: str) -> None:
    """Refuse unless the remote authority and the control plane run the configured candidate."""
    require_installed_authority(config)
    require_deployed_revision(config, base_url, _running_workers())


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


def remote_authority_profile(arch: str, image: str) -> dict[str, object]:
    """A disk-image profile whose root is the catalog image ``image`` (``kernel_source_ref``
    unread)."""
    return {
        "schema_version": 1,
        "arch": arch,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": REMOTE_ALLOCATION_DISK_GB,
        "boot_method": "disk-image",
        "kernel_source_ref": "remote-authority-unread",
        "provider": {
            "remote-libvirt": {
                "base_image_source": {
                    "kind": "catalog",
                    "provider": "remote-libvirt",
                    "name": image,
                }
            }
        },
    }


async def remote_root(db_url: str, resource: str, arch: str) -> tuple[str, str, str]:
    """The remote Resource's id and the one catalog image (name, digest) to root its System on."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT id::text FROM resources WHERE kind = 'remote-libvirt' AND name = %s",
            (resource,),
        )
        found = await cursor.fetchone()
        cursor = await conn.execute(_REMOTE_ROOTS, (arch,))
        images = await cursor.fetchall()
    if found is None:
        # The name stays out of the reason: it comes from the private proof config.
        raise ScenarioStop(
            Outcome.BLOCKED,
            "the control plane holds no remote-libvirt resource named by the proof config",
        )
    if len(images) != 1:
        raise ScenarioStop(
            Outcome.BLOCKED,
            "remote authority provisioning needs exactly one registered public remote-libvirt "
            f"catalog image for {arch} with a digest and an inspected root; found {len(images)}",
        )
    return str(found[0]), str(images[0][0]), str(images[0][1])


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
    """The six names a fixture create makes for ``system_id``, in creation order (runbook)."""
    return [
        f"{_AUTHORITY_ROOTFS}/{system_id}-fixture-base.qcow2",
        f"{_AUTHORITY_ROOTFS}/{system_id}-baseline",
        f"{_AUTHORITY_ROOTFS}/{system_id}-overlay.qcow2",
        f"{_AUTHORITY_CONSOLE}/{system_id}.log",
        str(console_boot_marker_path(Path(_AUTHORITY_CONSOLE) / f"{system_id}.log")),
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
    ``config`` is the per-System fixture config on the local lane, and on a remote cell the remote
    proof config with this invocation's ownership prefix (its ``project`` names the proof host's
    carrier project, not this System's; use ``project``).
    ``config`` and ``ledger`` are the invocation's: a body records each Investigation it opens in
    ``ledger`` (the frame closes any still open). A body releases every external boot it starts,
    in a ``finally`` block: the frame does not, and a held activation fails its cleanup.
    """

    op: LiveStackClient
    project: str
    allocation_id: str
    system_id: str
    config: NativeAuthorityConfig | RemoteAuthorityProofConfig
    ledger: ResourceLedger


AuthorityBody = Callable[[AuthoritySystem], Awaitable[dict[str, object]]]


@dataclass
class _Frame:
    """What one invocation owns so far; cleanup removes exactly this."""

    op: LiveStackClient
    db_url: str
    project: str
    before: dict[str, object]
    remote: RemoteAuthorityProofConfig | None = None
    allocation: str = ""
    in_use: int = 0
    system_id: str | None = None
    worker_disks: list[str] = field(default_factory=list)
    fixture: NativeAuthorityConfig | None = None
    ledger: ResourceLedger | None = None


def _require_systems_toml() -> Path:
    # The routing guards read the lane's inventory; the test session sandboxes the XDG default
    # and keeps only an exported path (tests/conftest.py), so an unset path would pass them.
    inventory = os.environ.get(SYSTEMS_TOML_ENV, "")
    if not inventory or not Path(inventory).is_file():
        raise ScenarioStop(Outcome.BLOCKED, f"{SYSTEMS_TOML_ENV} must name the lane's systems.toml")
    return Path(inventory)


def select_lane() -> AuthorityLaneConfig:
    """The runnable local lane config, or a ``blocked`` stop before anything is read or written."""
    config = load_lane_config()
    if config is None:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{CONFIG_ENV} unset; name the installed local-authority lane config"
        )
    if config.system_mode == "public":
        raise ScenarioStop(Outcome.BLOCKED, PUBLIC_BLOCKED)
    inventory = _require_systems_toml()
    # One local-libvirt resource exists per worker libvirt endpoint, so fixture mode needs the
    # lane's fixture-mode inventory; refusing here stops before any allocation is requested.
    try:
        local = load_inventory(inventory).local_libvirt
    except InventoryError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{SYSTEMS_TOML_ENV} is invalid: {exc}") from exc
    if local and all(entry.authority_instance for entry in local):
        raise ScenarioStop(Outcome.BLOCKED, NO_PLAIN_BLOCKED)
    return config


def select_remote_lane() -> RemoteAuthorityProofConfig:
    """The remote proof config with a freshly minted ownership prefix, or a ``blocked`` stop."""
    config = load_remote_config()
    if config is None:
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"{REMOTE_CONFIG_ENV} unset; name the installed remote-authority proof config",
        )
    _require_systems_toml()
    try:
        route = remote_config_for_resource(config.resource_name).authority
    except CategorizedError:
        route = None
    if route is None or route.authority_instance != config.authority_instance:
        raise ScenarioStop(Outcome.BLOCKED, REMOTE_ROUTED_BLOCKED)
    prefix = f"kdive-2151-{config.installed_revision[:12]}-{secrets.token_hex(4)}"
    return RemoteAuthorityProofConfig.model_validate(
        config.model_dump() | {"ownership_prefix": prefix}
    )


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
    if run.cell.provider == "remote-libvirt":
        await _on_remote_authority_system(run, base_url, issuer, db_url, project=project, body=body)
        return
    lane = select_lane()
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
        profile = fixture_profile(entry.arch, digest, f"{project}-unread")

        async def create(frame: _Frame) -> dict[str, object]:
            return await _create_and_run(run, frame, lane, running, profile, digest, body)

        await _allocate_and_run(
            run,
            _Frame(op, db_url, project, before),
            {"mode": "kind"},
            disk_gb=LOCAL_ALLOCATION_DISK_GB,
            create=create,
        )


async def _allocate_and_run(
    run: CellRun,
    frame: _Frame,
    resource: dict[str, str],
    *,
    disk_gb: int,
    create: Callable[[_Frame], Awaitable[dict[str, object]]],
) -> None:
    """Record the capacity and an allocation in ``frame``, create and run, then prove ``effect``
    and ``cleanup``; cleanup runs whether or not creation or the body raised."""
    op = frame.op
    frame.in_use = await capacity_in_use(op)
    frame.allocation = ok(
        await scalar(
            op,
            "allocations.request",
            project=frame.project,
            vcpus=2,
            memory_gb=2,
            disk_gb=disk_gb,
            resource=resource,
        ),
        "allocate",
    ).object_id
    try:
        observed = await create(frame)
    except BaseException as primary:
        await _cleanup_after_failure(run, frame, primary)
        raise
    run.prove("effect", {"exposure": run.cell.exposure, **observed})
    run.prove("cleanup", await _cleanup(frame))


def _remote_state(inventory: RemoteAuthorityInventory) -> dict[str, object]:
    """What the remote branch must leave as it found it: the authority's domains and volumes."""
    return {
        "remote_authority_domains": inventory.domains,
        "remote_authority_volumes": inventory.volumes,
    }


async def _on_remote_authority_system(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    body: AuthorityBody,
) -> None:
    """The remote branch: provision publicly on the authority-routed resource, then the body."""
    config = select_remote_lane()
    arch = str(run.cell.guest_arch)
    await asyncio.to_thread(require_remote_lane, config, base_url)
    await require_evidence_read_access(db_url)
    observed = await asyncio.to_thread(remote_authority_inventory, config)
    if observed.arch != arch:
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"the remote authority host is {observed.arch}; {run.cell.id} needs {arch}",
        )
    resource_id, image, digest = await remote_root(db_url, config.resource_name, arch)
    token = mint_role_token(issuer, project=project, agent_session=f"{project}-sess", role="admin")
    async with LiveStackClient.over_http(base_url, token) as op:
        await seed_metering(db_url, project)
        run.observed |= {"image_sha256": digest.removeprefix("sha256:"), "guest_arch": arch}
        profile = remote_authority_profile(arch, image)

        async def create(frame: _Frame) -> dict[str, object]:
            return await _create_remote_and_run(run, frame, config, profile, digest, body)

        await _allocate_and_run(
            run,
            _Frame(op, db_url, project, _remote_state(observed), remote=config),
            {"mode": "id", "resource_id": resource_id},
            disk_gb=REMOTE_ALLOCATION_DISK_GB,
            create=create,
        )


async def _create_remote_and_run(
    run: CellRun,
    frame: _Frame,
    config: RemoteAuthorityProofConfig,
    profile: dict[str, object],
    digest: str,
    body: AuthorityBody,
) -> dict[str, object]:
    """Provision through HTTP, observe the domain on the remote authority daemon, run ``body``."""
    op = frame.op
    frame.ledger = ResourceLedger(config.ownership_prefix)
    provisioned = ok(
        await scalar(op, "systems.provision", allocation_id=frame.allocation, profile=profile),
        "provision",
    )
    system_id = frame.system_id = data_str(provisioned, "system_id")
    await await_system_state(op, "provision", system_id, "ready")
    await require_root_provenance(frame.db_url, system_id, digest, str(profile["arch"]))
    facts = (await asyncio.to_thread(remote_authority_inventory, config, system_id)).system
    assert facts is not None and facts.active, (
        "the System's domain is not running on the remote authority daemon"
    )
    run.observed["accelerator"] = ACCELERATORS.get(facts.kind, "none")
    return await body(
        AuthoritySystem(op, frame.project, frame.allocation, system_id, config, frame.ledger)
    )


async def _move_to_authority(frame: _Frame, config: NativeAuthorityConfig) -> None:
    """Run the fixture create and record its names; a failed create records the fixed six."""
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
    assert reported == names, "the fixture create reported names other than its six"


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
        barrier_socket=lane.barrier_socket,
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


async def _allocation_released(op: LiveStackClient, allocation_id: str) -> bool:
    env = await scalar(op, "allocations.wait", allocation_id=allocation_id, timeout_s=0)
    return env.status == "released"


async def _teardown(op: LiveStackClient, system_id: str) -> None:
    """Tear the System down unless the body already did (the #3185 force-release proof)."""
    current = await scalar(op, "systems.get", system_id=system_id)
    if current.status != "torn_down":
        ok(await scalar(op, "systems.teardown", system_id=system_id), "teardown")
        await await_system_state(op, "teardown", system_id, "torn_down")


async def _release(frame: _Frame) -> dict[str, object]:
    """Release the allocation and require the capacity in use back where it was."""
    op = frame.op
    if not await _allocation_released(op, frame.allocation):
        ok(await scalar(op, "allocations.release", allocation_id=frame.allocation), "release")
    await _await_released(op, frame.allocation, DRAIN_DEADLINE_S, POLL_INTERVAL_S)
    in_use = await capacity_in_use(op)
    assert in_use == frame.in_use, f"capacity in use is {in_use}, not {frame.in_use}"
    return {"allocation": "released", "in_use": in_use}


def _require_unchanged(before: dict[str, object], after: dict[str, object]) -> None:
    changed = sorted(key for key in after if after[key] != before.get(key))
    assert not changed, f"the authority lane inventory changed in {changed}"


async def _cleanup(frame: _Frame) -> dict[str, object]:
    """Drain, close, remove the fixture, tear down, release, then compare the inventory."""
    if frame.remote is not None:
        return await _remote_cleanup(frame, frame.remote)
    op = frame.op
    owned = [frame.allocation]
    if frame.system_id is None:
        released = await _release(frame)
        drained: list[str] = []
    else:
        drained = await drain_system_jobs(op, frame.db_url, frame.system_id)
        if frame.fixture is not None and frame.ledger is not None:
            await _close_investigations(op, frame.ledger)
            owned += [r.identity for r in frame.ledger.resources if r.kind == "authority-fixture"]
            await asyncio.to_thread(remove_authority_fixture, frame.fixture, frame.ledger)
        owned.append(frame.system_id)
        await _teardown(op, frame.system_id)
        released = await release_and_verify(
            op,
            allocation_id=frame.allocation,
            system_id=frame.system_id,
            domain=domain_name_for(UUID(frame.system_id)),
            disks=frame.worker_disks,
            in_use_before=frame.in_use,
        )
    after = await asyncio.to_thread(lane_inventory)
    _require_unchanged(frame.before, after)
    return {
        **released,
        "jobs_drained": sorted(drained),
        "owned": owned,
        "inventory_sha256": _digest(after),
    }


async def _remote_cleanup(frame: _Frame, config: RemoteAuthorityProofConfig) -> dict[str, object]:
    """Drain, close, tear down through the authority, prove absence, release, compare."""
    op = frame.op
    owned = [frame.allocation]
    drained: list[str] = []
    if frame.system_id is not None:
        drained = await drain_system_jobs(op, frame.db_url, frame.system_id)
        if frame.ledger is not None:
            await _close_investigations(op, frame.ledger)
        await _teardown(op, frame.system_id)
        left = await asyncio.to_thread(remote_authority_inventory, config, frame.system_id)
        assert left.system is None, "the System's domain remains on the remote authority daemon"
        stranded = [volume for volume in left.volumes if frame.system_id in volume]
        assert not stranded, f"the System's volumes remain in the authority pool: {stranded}"
        owned += [frame.system_id, domain_name_for(UUID(frame.system_id))]
    released = await _release(frame)
    after = _remote_state(await asyncio.to_thread(remote_authority_inventory, config))
    _require_unchanged(frame.before, after)
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
