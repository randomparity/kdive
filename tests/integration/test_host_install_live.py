"""One boot phase of the host-installation proof, run on the freshly installed host (ADR-0716).

`scripts/host_install_proof.py run` installs the host through the documented entry points and
invokes this node twice: after the clean install and again after repeating setup. The node reads
the deployed revisions, checks the installed worker prerequisites, boots the pinned kernel bundle
on a real guest, observes guest confinement, releases the guest and checks its cleanup, then
writes one phase record. The runner, not this node, judges the cell.

It skips unless the runner supplies ``HOST_INSTALL_PHASE``, ``HOST_INSTALL_OUTPUT``,
``HOST_INSTALL_BUNDLE``, ``HOST_INSTALL_CANDIDATE`` and ``HOST_INSTALL_IMAGE``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID
from xml.etree import ElementTree

import libvirt
import pytest

from kdive.domain.platform.arch_traits import arch_traits
from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient, oidc_issuer_from_env
from kdive.providers.shared.runtime_paths import domain_name_for
from scripts.host_install_proof import (
    ARCH_LANE,
    ROLES,
    PhaseRecord,
    bundle_inputs,
    console_has_release,
    kernel_fields,
    label_confined,
    package_digest,
    qemu_pid,
)
from tests.integration.live_stack.skew import probe_stack_skew, repo_facts
from tests.integration.live_stack.spine import (
    LOCAL_ALLOCATION_DISK_GB,
    await_system_state,
    build_profile,
    drain_job,
    full_artifact_text,
    mint_role_token,
    ok,
    put_presigned,
    scalar,
    sha256_b64,
    worker_libvirt_uri,
)
from tests.mcp.json_data import data_str

_ENV = ("PHASE", "OUTPUT", "BUNDLE", "CANDIDATE", "IMAGE")
_LIFECYCLE = Path("/opt/kdive-live-worker-lifecycle")
_SOURCE = Path(__file__).resolve().parents[2] / "src" / "kdive"
_CONTROL_GROUP = "kdive-live-control"


def _inputs() -> dict[str, str]:
    values = {name: os.environ.get(f"HOST_INSTALL_{name}", "") for name in _ENV}
    if not all(values.values()):
        pytest.skip("host-install phase inputs unset; run scripts/host_install_proof.py run")
    return values


def _host_mode() -> str:
    try:
        return subprocess.run(
            ["getenforce"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        path = Path("/sys/module/apparmor/parameters/enabled")
        return path.read_text().strip() if path.exists() else "none"


def _host_os() -> str:
    fields = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    return f"{fields['ID'].strip('"')}:{fields['VERSION_ID'].strip('"')}"


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _lifecycle_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_LIFECYCLE / ".venv/bin/python"), "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", os.defpath)},
    )


def _deployed(candidate: str, base_url: str, failures: list[str]) -> dict[str, str | None]:
    facts = repo_facts()
    if facts is None or facts.head != candidate or facts.newest_modified_source_mtime():
        failures.append("checkout is not the clean candidate")
        return dict.fromkeys(ROLES)
    probe = probe_stack_skew(base_url)
    deployed: dict[str, str | None] = {}
    for role in ("server", "reconciler", "worker"):
        reported = probe.revisions.get(role)
        deployed[role] = facts.resolve(reported) if reported else None
    if probe.worker_pids is None:
        failures.append("worker inventory disagrees with the reported worker builds")
        deployed["worker"] = None
    try:
        stamp = (_LIFECYCLE / "revision").read_text().strip()
        located = _lifecycle_python("import kdive, os; print(os.path.dirname(kdive.__file__))")
        installed = Path(located.stdout.strip())
        same_code = located.returncode == 0 and package_digest(installed) == package_digest(_SOURCE)
    except OSError as error:
        failures.append(f"authority revision unreadable: {type(error).__name__}")
        stamp, same_code = "", False
    deployed["authority"] = stamp if same_code and len(stamp) == 40 else None
    failures.extend(
        f"{role} revision is {value or 'unknown'}"
        for role, value in deployed.items()
        if value != candidate
    )
    return deployed


def _prerequisites(authority: str | None) -> dict[str, bool]:
    imports = _lifecycle_python("import guestfs, libvirt, kdive")
    socket = subprocess.run(
        ["systemctl", "is-active", "--quiet", "kdive-live-worker-lifecycle.socket"], check=False
    )
    groups = subprocess.run(["id", "-nG"], capture_output=True, text=True, check=False)
    return {
        "worker-imports": imports.returncode == 0,
        "lifecycle-socket": socket.returncode == 0,
        "operator-control-group": _CONTROL_GROUP in groups.stdout.split(),
        "published-libvirt-endpoint": "live-libvirt" in os.environ.get("KDIVE_LIBVIRT_URI", ""),
        "authority-revision": authority is not None,
    }


def _guest_observation(system_id: str, host_arch: str) -> tuple[str, str, bool]:
    """Read the running domain's accelerator and its qemu process label."""
    name = domain_name_for(UUID(system_id))
    conn = libvirt.open(worker_libvirt_uri())
    try:
        domain = conn.lookupByName(name)
        uuid, xml = domain.UUIDString(), domain.XMLDesc(0)
    finally:
        conn.close()
    kind = ElementTree.fromstring(xml).get("type")
    accelerator = ARCH_LANE[host_arch][0] if kind == "kvm" else "tcg"
    rows = subprocess.run(
        ["ps", "-ww", "-eo", "pid=,args="], capture_output=True, text=True, check=True
    ).stdout
    pid = qemu_pid(rows, name)
    # The whole label, mode suffix included: AppArmor prints `libvirt-<uuid> (enforce)`.
    label = "-" if pid is None else Path(f"/proc/{pid}/attr/current").read_text().strip("\0\n ")
    return accelerator, label, label_confined(label, uuid)


def _domain_absent(system_id: str) -> bool:
    conn = libvirt.open(worker_libvirt_uri())
    try:
        conn.lookupByName(domain_name_for(UUID(system_id)))
    except libvirt.libvirtError:
        return True
    finally:
        conn.close()
    return False


async def _upload(client: LiveStackClient, run_id: str, bundle: Path) -> None:
    files = {"kernel": bundle / "kernel.tar.gz", "effective_config": bundle / "effective_config"}
    declared = [
        {"name": name, "sha256": sha256_b64(path), "size_bytes": path.stat().st_size}
        for name, path in files.items()
    ]
    issued = ok(
        await scalar(client, "artifacts.create_run_upload", run_id=run_id, artifacts=declared),
        "upload",
    )
    targets = {item.data.get("name"): item for item in issued.items}
    for name, path in files.items():
        await put_presigned(targets[name], path)
    ok(await scalar(client, "runs.complete_build", run_id=run_id), "upload")


async def _console(client: LiveStackClient, system_id: str, run_id: str) -> str:
    listing = ok(await scalar(client, "artifacts.list", system_id=system_id), "console")
    suffix = f"/console-{run_id}"
    ids = [item.object_id for item in listing.items if item.refs.get("object", "").endswith(suffix)]
    return await full_artifact_text(client, ids[0], "console") if ids else ""


async def _boot_phase(
    inputs: dict[str, str], manifest: dict[str, Any], observe: Callable[[str], None]
) -> tuple[bool, dict[str, bool]]:
    """Allocate, provision, upload, install, boot, observe and release.

    Returns whether the console showed the bundle's release, and the cleanup observations.
    """
    arch = platform.machine()
    project = os.environ.get("KDIVE_PROJECT", "demo")
    token = mint_role_token(
        oidc_issuer_from_env(), project=project, agent_session="host-install", role="operator"
    )
    booted = False
    cleanup: dict[str, bool] = {}
    allocation_id = system_id = ""
    released = False
    async with LiveStackClient.over_http(os.environ["KDIVE_STACK_BASE_URL"], token) as client:
        try:
            granted = ok(
                await scalar(
                    client,
                    "allocations.request",
                    project=project,
                    vcpus=2,
                    memory_gb=2,
                    disk_gb=LOCAL_ALLOCATION_DISK_GB,
                    resource={"mode": "kind"},
                ),
                "allocate",
            )
            allocation_id = granted.object_id
            profile = {
                "schema_version": 1,
                "arch": arch,
                "vcpu": 2,
                "memory_mb": 2048,
                "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                "boot_method": "direct-kernel",
                "kernel_source_ref": inputs["BUNDLE"],
                "provider": {
                    "local-libvirt": {
                        "rootfs": {"kind": "local", "path": os.environ["KDIVE_GUEST_IMAGE"]},
                        "crashkernel": arch_traits(arch).default_crashkernel,
                    }
                },
            }
            provisioned = ok(
                await scalar(
                    client, "systems.provision", allocation_id=allocation_id, profile=profile
                ),
                "provision",
            )
            system_id = data_str(provisioned, "system_id")
            await await_system_state(client, "provision", system_id, "ready")
            investigation = ok(
                await scalar(client, "investigations.open", project=project, title="host-install"),
                "run",
            )
            run_id = ok(
                await scalar(
                    client,
                    "runs.create",
                    investigation_id=investigation.object_id,
                    system_id=system_id,
                    build_profile=build_profile(arch),
                ),
                "run",
            ).object_id
            await _upload(client, run_id, Path(inputs["BUNDLE"]))
            for step in ("install", "boot"):
                job = ok(await scalar(client, f"runs.{step}", run_id=run_id), step)
                await drain_job(client, step, job.object_id)
            booted = console_has_release(
                await _console(client, system_id, run_id), str(manifest["release"])
            )
            observe(system_id)
            ok(await scalar(client, "allocations.release", allocation_id=allocation_id), "release")
            released = True
            await await_system_state(client, "teardown", system_id, "torn_down")
            cleanup["system-torn-down"] = True
            cleanup["domain-absent"] = _domain_absent(system_id)
        finally:
            if allocation_id and not released:
                await scalar(client, "allocations.release", allocation_id=allocation_id)
    return booted, cleanup


@pytest.mark.live_stack
def test_installed_host_boots_pinned_kernel() -> None:
    inputs = _inputs()
    output = Path(inputs["OUTPUT"])
    output.mkdir(parents=True, exist_ok=False)
    failures: list[str] = []
    manifest, kernel_sha256 = bundle_inputs(Path(inputs["BUNDLE"]))
    deployed = _deployed(inputs["CANDIDATE"], os.environ["KDIVE_STACK_BASE_URL"], failures)
    prerequisites = _prerequisites(deployed.get("authority"))
    host_arch = platform.machine()
    row = load_rootfs_catalog()[inputs["IMAGE"]]
    guest: dict[str, Any] = {"accelerator": "none", "label": "-", "confined": False}

    def observe(system_id: str) -> None:
        accelerator, label, confined = _guest_observation(system_id, host_arch)
        guest.update(accelerator=accelerator, label=label, confined=confined)

    try:
        booted, cleanup = asyncio.run(_boot_phase(inputs, manifest, observe))
    except Exception as error:  # noqa: BLE001 - the phase record carries the failure category
        failures.append(f"boot phase raised {type(error).__name__}: {str(error)[:300]}")
        booted, cleanup = False, {}
    if not booted:
        failures.append("console never showed the bundle's kernel release")
    context = {
        "host_os": _host_os(),
        "host_arch": host_arch,
        "guest_os": f"{row.distro}:{row.version}",
        "guest_arch": row.arch,
        "accelerator": guest["accelerator"],
        "image_sha256": _file_sha256(Path(os.environ["KDIVE_GUEST_IMAGE"])),
        **kernel_fields(manifest, kernel_sha256),
    }
    host_enforcing = _host_mode() in ("Enforcing", "Y")
    failures.extend(
        f"prerequisite {name} missing" for name, held in prerequisites.items() if not held
    )
    if not (host_enforcing and guest["confined"]):
        failures.append(f"confinement not observed (guest label {guest['label']})")
    if not cleanup or not all(cleanup.values()):
        failures.append("owned guest resources were not reclaimed")
    record = PhaseRecord.model_validate(
        {
            "phase": inputs["PHASE"],
            "passed": not failures,
            "booted": booted,
            "deployed": deployed,
            "context": context,
            "host_enforcing": host_enforcing,
            "guest_label": guest["label"][:512],
            "guest_confined": guest["confined"],
            "prerequisites": prerequisites,
            "cleanup": cleanup,
            "failures": [failure[:512] for failure in failures[:64]],
        }
    )
    (output / "phase.json").write_text(record.model_dump_json(indent=2) + "\n")
    assert record.passed, failures
