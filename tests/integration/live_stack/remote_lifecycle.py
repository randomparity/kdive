"""Remote deep-lifecycle frame, provider-host observer and bindings (#2810, ADR-0715).

The ``deep-lifecycle/remote-libvirt/x86_64`` cells run #2809's provider-neutral ``deep_body`` on
a System of a separate ``remote-libvirt`` host. The provider host is observed through the
operator's own SSH access (:data:`HOST_SSH_ENV`), never the worker's TLS identity:
:func:`host_probe` reads its OS, architecture and virtualization, and :func:`observer` opens
``qemu+ssh`` for the domain XML, volume absence and the defined ``kdive-*`` domains;
:func:`volume_sha256` hashes the base volume on the provider host.
``python -m tests.integration.live_stack.remote_lifecycle bindings --candidate SHA --out FILE``
writes the qualifier's expected ``Context`` for those cells. The live test is
``tests/integration/test_remote_deep_lifecycle_live.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import xml.etree.ElementTree as ET  # noqa: S405 - the provider host's domain XML  # nosec B405
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache, partial
from pathlib import Path
from uuid import UUID

import libvirt

from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.model import StagedSource
from kdive.inventory.path import systems_toml_path
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.providers.remote_libvirt.config import remote_config_for_resource, remote_instance_names
from kdive.providers.shared.runtime_paths import domain_name_for
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings, Outcome
from tests.integration.live_stack.cleanup import capacity_in_use, domain_disks, release_and_verify
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    Fixture,
    baseline,
    bound_kernel,
    load_fixture,
)
from tests.integration.live_stack.evidence import key_values, os_identity
from tests.integration.live_stack.image_smoke import Endpoint, ssh
from tests.integration.live_stack.scenario import (
    ACCELERATORS,
    CatalogBody,
    CellRun,
    Provision,
    ScenarioStop,
    cleanup_attempt,
    provision_catalog,
)
from tests.integration.live_stack.spine import (
    REMOTE_ALLOCATION_DISK_GB,
    mint_role_token,
    ok,
    scalar,
    seed_metering,
)

# Test-only input, unprefixed so env-docs-check does not require it in the product catalog.
HOST_SSH_ENV = "REMOTE_PROVIDER_SSH"
_DESTINATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*")
HOST_PROBE = (
    'cat /etc/os-release; printf "machine=%s\\nvirt=%s\\n" '
    '"$(uname -m)" "$(systemd-detect-virt || true)"'
)


@dataclass(frozen=True)
class RemoteImage:
    """A remote-libvirt base image of ``deploy/ansible/inventory/group_vars/all.yml``."""

    name: str
    distro: str
    version: str
    arch: str


@dataclass(frozen=True)
class RemoteHost:
    """The observed provider host; ``dest`` is operator input and never enters evidence."""

    dest: str
    pool: str
    host_os: str
    host_arch: str
    virt: str


# One remote base image per family; Fedora and Enterprise Linux stay distinct (#2803 req 7).
REMOTE_REPRESENTATIVES = {
    "suse": RemoteImage("opensuse-leap-15.6-kdive-remote-base", "opensuse-leap", "15.6", "x86_64"),
    "debian": RemoteImage("ubuntu-2404-kdive-remote-base", "ubuntu", "24.04", "x86_64"),
    "fedora": RemoteImage("fedora-kdive-remote-base-43", "fedora", "43", "x86_64"),
    "enterprise": RemoteImage("rocky-10-kdive-remote-base", "rocky", "10", "x86_64"),
}
# Families with no remote full cycle yet, and the issue that owns the gap.
REMOTE_BLOCKED: dict[str, str] = {}


def remote_cells() -> list[Cell]:
    """The remote ``deep-lifecycle`` cells #2810 owns (x86_64; ppc64le is #2818's)."""
    return [
        cell
        for cell in build_contract().cells
        if cell.operation == "deep-lifecycle" and cell.owner == 2810
    ]


def destination() -> str | None:
    """The operator's ``user@host`` SSH destination for the provider host, if configured."""
    value = os.environ.get(HOST_SSH_ENV, "")
    if not value:
        return None
    if not _DESTINATION.fullmatch(value):
        raise ValueError(f"{HOST_SSH_ENV} is not a user@host SSH destination")
    return value


def _host_ssh(dest: str, command: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603,S607 - fixed argv, validated destination  # nosec B603 B607
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", dest, command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def host_probe(dest: str) -> dict[str, str]:
    """The provider host's ``ID:VERSION_ID``, ``uname -m`` and ``systemd-detect-virt``."""
    result = _host_ssh(dest, HOST_PROBE, 60.0)
    assert result.returncode == 0, f"provider-host probe exited {result.returncode}"
    fields = key_values(result.stdout)
    return {
        "host_os": os_identity(result.stdout),
        "host_arch": fields.get("machine", ""),
        "virt": fields.get("virt", ""),
    }


def observer(dest: str) -> libvirt.virConnect:
    """The test's own libvirt connection to the provider host, over the operator's SSH."""
    return libvirt.open(f"qemu+ssh://{dest}/system?no_tty=1")


def volume_absent(conn: libvirt.virConnect, path: str) -> bool:
    """True only when no refreshed active pool of the provider host resolves ``path``."""
    for pool in conn.listAllStoragePools(libvirt.VIR_CONNECT_LIST_STORAGE_POOLS_ACTIVE):
        pool.refresh(0)
    try:
        conn.storageVolLookupByPath(path)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
            return True
        raise
    return False


def remote_volume_absent(dest: str, path: str) -> bool:
    """:func:`volume_absent` over the test's own observer connection to the provider host."""
    conn = observer(dest)
    try:
        return volume_absent(conn, path)
    finally:
        conn.close()


def remote_kdive_domains(conn: libvirt.virConnect) -> set[str]:
    """The names of every ``kdive-`` domain the provider host defines."""
    return {d.name() for d in conn.listAllDomains(0) if d.name().startswith("kdive-")}


@cache
def volume_sha256(dest: str, pool: str, volume: str) -> str:
    """SHA-256 of ``pool``/``volume``, read through libvirt on the provider host itself.

    ``virsh vol-download`` runs beside the volume, so only the digest crosses the network.
    """
    command = (
        "set -o pipefail; virsh -q -c qemu:///system vol-download "
        f"--pool {shlex.quote(pool)} {shlex.quote(volume)} /dev/stdout | sha256sum"
    )
    result = _host_ssh(dest, command, 1800.0)
    assert result.returncode == 0, f"volume digest exited {result.returncode}"
    digest = result.stdout.split()[0]
    assert re.fullmatch(r"[0-9a-f]{64}", digest), "volume digest is not a SHA-256"
    return digest


def staged_volume(name: str) -> str | None:
    """The storage-pool volume ``systems.toml`` stages for remote-libvirt image ``name``."""
    doc = load_inventory_optional(systems_toml_path())
    for image in doc.image if doc is not None else ():
        if (
            image.provider == "remote-libvirt"
            and image.name == name
            and isinstance(image.source, StagedSource)
        ):
            return image.source.volume
    return None


def staged_base_volume(image: RemoteImage) -> str:
    """The staged base volume of ``image``; an unstaged image is a blocked cell."""
    volume = staged_volume(image.name)
    if volume is None:
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"{image.name} is not a staged remote-libvirt [[image]]; build it with "
            "deploy/ansible/playbooks/image.yml",
        )
    return volume


def remote_host() -> RemoteHost:
    """The configured, reachable provider host; a missing prerequisite is a blocked cell."""
    try:
        dest = destination()
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, str(exc)) from None
    if dest is None:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{HOST_SSH_ENV} unset; give the test SSH access to the provider host"
        )
    names = remote_instance_names()
    if len(names) != 1:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{len(names)} [[remote_libvirt]] instances; declare exactly one"
        )
    try:
        probe = host_probe(dest)
    except (AssertionError, OSError, subprocess.SubprocessError, ValueError) as exc:
        # The type only: a message can carry the SSH destination (ADR-0715 evidence).
        raise ScenarioStop(
            Outcome.BLOCKED, f"provider-host probe failed: {type(exc).__name__}"
        ) from None
    return RemoteHost(dest, remote_config_for_resource(names[0]).storage_pool, **probe)


def observe_host(run: CellRun, host: RemoteHost) -> None:
    """Record the provider host as the cell's host; a foreign architecture is blocked."""
    if host.host_arch != run.cell.guest_arch:
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"the provider host is {host.host_arch}; {run.cell.id} needs {run.cell.guest_arch}",
        )
    run.observed |= {"host_os": host.host_os, "host_arch": host.host_arch}
    provider = {"os": host.host_os, "arch": host.host_arch, "virtualization": host.virt}
    run.artifacts.append(run.writer.artifact({"cell": run.cell.id, "provider_host": provider}))


def remote_profile(arch: str, volume: str) -> dict[str, object]:
    """A disk-image profile booting staged base volume ``volume`` (``kernel_source_ref`` unread)."""
    return {
        "schema_version": 1,
        "arch": arch,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": REMOTE_ALLOCATION_DISK_GB,
        "boot_method": "disk-image",
        "kernel_source_ref": "remote-deep-lifecycle-unread",
        "provider": {"remote-libvirt": {"base_image_volume": volume}},
    }


def guest_boot_kernel(
    _system_id: str, endpoint: Endpoint, key: Path, release: str, *, family: str
) -> tuple[str, None]:
    """Hash the exact family-owned installed artifact; never fall back to another kernel."""
    if family not in REMOTE_REPRESENTATIVES:
        raise ValueError(f"no remote kernel observation path for family {family}")
    directory = "/boot/kdive" if family in {"debian", "suse"} else "/boot"
    command = f"sha256sum -- {shlex.quote(f'{directory}/vmlinuz-{release}')}"
    result = ssh(endpoint, key, command)
    assert result.returncode == 0, f"cannot read the installed kernel: exit {result.returncode}"
    return result.stdout.split()[0], None


async def remote_cleanup(
    op: LiveStackClient,
    host: RemoteHost,
    allocation: str,
    system_id: str,
    owned: list[str],
    in_use: int,
    before: set[str],
) -> dict[str, object]:
    """Release, then prove on the provider host the domain, volumes and domain set reclaimed."""
    conn = observer(host.dest)
    try:
        result = await release_and_verify(
            op,
            allocation_id=allocation,
            system_id=system_id,
            domain=domain_name_for(UUID(system_id)),
            disks=owned,
            in_use_before=in_use,
            connect=partial(observer, host.dest),
            absent=partial(volume_absent, conn),
        )
        left = remote_kdive_domains(conn) - before
    finally:
        conn.close()
    assert not left, f"kdive domain(s) remain on the provider host: {sorted(left)}"
    return {**result, "remote_domains": "unchanged"}


def remote_xml(dest: str, system_id: str) -> str:
    """System ``system_id``'s domain XML, read on the provider host through the observer."""
    conn = observer(dest)
    try:
        return conn.lookupByName(domain_name_for(UUID(system_id))).XMLDesc(0)
    finally:
        conn.close()


def _kdive_domains(dest: str) -> set[str]:
    conn = observer(dest)
    try:
        return remote_kdive_domains(conn)
    finally:
        conn.close()


async def on_remote_system(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    family: str,
    body: CatalogBody,
    provision: Provision = provision_catalog,
) -> None:
    """Provision ``family``'s remote representative to ``ready``, run ``body``, prove cleanup.

    ``provision`` creates the System; a cell whose tool under test is the provision passes its
    own.
    """
    if family in REMOTE_BLOCKED:
        raise ScenarioStop(Outcome.BLOCKED, REMOTE_BLOCKED[family])
    image = REMOTE_REPRESENTATIVES[family]
    host = remote_host()
    observe_host(run, host)
    volume = staged_base_volume(image)
    run.observed["image_sha256"] = await asyncio.to_thread(
        volume_sha256, host.dest, host.pool, volume
    )
    token = mint_role_token(
        issuer, project=project, agent_session=f"{project}-sess", role="operator"
    )
    async with LiveStackClient.over_http(base_url, token) as op:
        await seed_metering(db_url, project)
        in_use_before = await capacity_in_use(op)
        domains_before = await asyncio.to_thread(_kdive_domains, host.dest)
        allocation = ok(
            await scalar(
                op,
                "allocations.request",
                project=project,
                vcpus=2,
                memory_gb=2,
                disk_gb=REMOTE_ALLOCATION_DISK_GB,
                resource={"mode": "kind", "kind": "remote-libvirt"},
            ),
            "allocate",
        ).object_id
        system_id: str | None = None
        owned: list[str] = []
        cleaned = False
        try:
            system_id = await provision(op, allocation, remote_profile(image.arch, volume))
            xml = await asyncio.to_thread(remote_xml, host.dest, system_id)
            accelerator = ET.fromstring(xml).get("type", "")  # noqa: S314  # nosec B314
            run.observed["accelerator"] = ACCELERATORS.get(accelerator, "none")
            owned = domain_disks(xml)
            await body(op, system_id, owned)
            cleanup = await remote_cleanup(
                op, host, allocation, system_id, owned, in_use_before, domains_before
            )
            run.prove("cleanup", cleanup)
            cleaned = True
        finally:
            if not cleaned:
                await cleanup_attempt(
                    run,
                    op,
                    allocation,
                    None
                    if system_id is None
                    else partial(
                        remote_cleanup,
                        op,
                        host,
                        allocation,
                        system_id,
                        owned,
                        in_use_before,
                        domains_before,
                    ),
                )


def bindings(
    candidate: str,
    *,
    root: Path | None,
    matrix: str,
    host: RemoteHost,
    digest: Callable[[str, str, str], str] = volume_sha256,
    fixture: Fixture = load_fixture,
    staged: Callable[[str], str | None] = staged_volume,
) -> InputBindings:
    """The expected ``Context`` of every #2810 cell; null guest fields for a blocked family."""
    kernels: dict[str, dict[str, str]] = {}
    cells = {}
    for cell in remote_cells():
        image = REMOTE_REPRESENTATIVES.get(str(cell.family))
        volume = staged(image.name) if image is not None else None
        if baseline(cell) not in kernels:
            kernels[baseline(cell)] = bound_kernel(root, baseline(cell), host.host_arch, fixture)
        guest = (
            {
                "guest_os": f"{image.distro}:{image.version}",
                "guest_arch": image.arch,
                "image_sha256": digest(host.dest, host.pool, volume) if volume else None,
                **kernels[baseline(cell)],
            }
            if image is not None
            else {}
        )
        cells[cell.id] = Context.model_validate(
            {
                "host_os": host.host_os,
                "host_arch": host.host_arch,
                "accelerator": cell.accelerator,
                **guest,
            }
        )
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=cells)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write remote deep-lifecycle input bindings.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("bindings")
    write.add_argument("--candidate", required=True)
    write.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        host = remote_host()
    except ScenarioStop as stop:
        print(f"cannot bind the remote cells: {stop}")
        return 2
    root = os.environ.get(FIXTURE_ROOT_ENV)
    inputs = bindings(
        args.candidate,
        root=Path(root) if root else None,
        matrix=build_contract().matrix_sha256,
        host=host,
    )
    args.out.write_text(inputs.model_dump_json(indent=1) + "\n", encoding="utf-8")
    bound = sum(c.kernel_build_id is not None for c in inputs.cells.values())
    print(f"wrote {len(inputs.cells)} binding(s); {bound} with a verified kernel fixture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
