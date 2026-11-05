"""The remote authority stages the uploaded vmlinux in the guest module tree (#3131).

Gated ``live_stack`` and dormant unless ``KDIVE_LIVE_VM_REMOTE_AUTHORITY_CONFIG`` names an
installed remote authority lane (x86_64 or ppc64le). The observed remote host architecture selects
the native guest. One body frames an authority-owned System, activates an external boot whose
build uploaded the tree's ``vmlinux``, and observes these facts through the
guest agent on the authority's private libvirt, as the authority account:

* ``/lib/modules/<release>/vmlinux`` exists in the booted guest with the uploaded SHA-256;
* a real ``drgn -k`` resolves ``init_uts_ns`` from it;
* after ``runs.release_external_boot`` the module appliance has restored the module tree, so
  the file is gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shlex
import tempfile
from pathlib import Path

import pytest

from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.authority_faults import activate
from tests.integration.live_stack.authority_lane import (
    REMOTE_CONFIG_ENV,
    AuthoritySystem,
    on_authority_system,
    select_remote_lane,
)
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.spine import drain_job
from tests.live_vm.installed_local_authority_support import release_external_boot
from tests.live_vm.installed_remote_authority_support import (
    RemoteAuthorityProofConfig,
    remote_authority_inventory,
    run_as_authority,
)

_KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"
_READY_WAIT_S = 180.0

# Runs as the authority account in its interpreter: one command in the System's guest through
# the guest agent, on the authority's private libvirt daemon. argv[1] is the System UUID and
# argv[2] a JSON list holding the program path and its arguments; it prints the exit code and
# the first 400 bytes of stdout as one JSON line.
_GUEST_EXEC = """
import base64, json, sys, time
from uuid import UUID

import libvirt
import libvirt_qemu

conn = libvirt.open("qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock")
domain = conn.lookupByName("kdive-" + str(UUID(sys.argv[1])))
argv = json.loads(sys.argv[2])


def agent(command):
    return json.loads(libvirt_qemu.qemuAgentCommand(domain, json.dumps(command), 30, 0))["return"]


pid = agent({"execute": "guest-exec", "arguments": {
    "path": argv[0], "arg": argv[1:], "capture-output": True}})["pid"]
deadline = time.monotonic() + 90
while True:
    status = agent({"execute": "guest-exec-status", "arguments": {"pid": pid}})
    if status["exited"] or time.monotonic() > deadline:
        break
    time.sleep(1)
out = base64.b64decode(status.get("out-data", "")).decode(errors="replace")[:400]
print(json.dumps({"exitcode": status.get("exitcode"), "out": out}))
conn.close()
"""

_DOMAIN_POWER = """
import sys
from uuid import UUID

import libvirt

conn = libvirt.open("qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock")
domain = conn.lookupByName("kdive-" + str(UUID(sys.argv[2])))
if sys.argv[1] == "start" and not domain.isActive():
    domain.create()
if sys.argv[1] == "stop" and domain.isActive():
    domain.destroy()
print(domain.isActive())
conn.close()
"""

_DRGN = 'import sys; print(prog["init_uts_ns"].name.release.string_().decode())'


def _guest(config: RemoteAuthorityProofConfig, system_id: str, *argv: str) -> tuple[int, str]:
    raw = run_as_authority(config, _GUEST_EXEC, system_id, json.dumps(list(argv)))
    data = json.loads(raw)
    return int(data["exitcode"]), str(data["out"]).strip()


def _wait_for_agent(config: RemoteAuthorityProofConfig, system_id: str) -> str:
    """Poll until the guest agent answers; return ``uname -r``."""
    import time

    deadline = time.monotonic() + _READY_WAIT_S
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            code, out = _guest(config, system_id, "/usr/bin/uname", "-r")
            if code == 0 and out:
                return out
            last = f"exit {code}"
        except Exception as failure:  # noqa: BLE001 - the agent is not up yet
            last = type(failure).__name__
        time.sleep(5)
    raise AssertionError(f"the guest agent never answered: {last}")


def _vmlinux_facts(
    config: RemoteAuthorityProofConfig, system_id: str, release: str
) -> dict[str, str]:
    path = f"/lib/modules/{release}/vmlinux"
    code, out = _guest(config, system_id, "/usr/bin/sha256sum", path)
    return {"present": str(code == 0), "sha256": out.split()[0] if code == 0 and out else ""}


def _drgn_release(config: RemoteAuthorityProofConfig, system_id: str) -> str:
    write = f"printf '%s\\n' {shlex.quote(_DRGN)} > /tmp/kdive-3131.py"
    script = f"{write} && drgn -k /tmp/kdive-3131.py"
    code, out = _guest(config, system_id, "/bin/sh", "-c", script)
    assert code == 0, f"drgn -k failed ({code}): {out}"
    return out


async def _prove(system: AuthoritySystem, tree: Path, guest_arch: str) -> dict[str, object]:
    config = system.config
    assert isinstance(config, RemoteAuthorityProofConfig)
    expected = hashlib.sha256((tree / "vmlinux").read_bytes()).hexdigest()
    observed: dict[str, object] = {}
    release = ""
    activation = None
    try:
        with tempfile.TemporaryDirectory() as scratch:
            activation = await activate(
                system,
                kernel_tree=tree,
                evidence_dir=Path(scratch) / "upload",
                with_vmlinux=True,
                guest_arch=guest_arch,
            )
        release = await asyncio.to_thread(_wait_for_agent, config, system.system_id)
        staged = await asyncio.to_thread(_vmlinux_facts, config, system.system_id, release)
        assert staged["present"] == "True", f"no /lib/modules/{release}/vmlinux in the guest"
        assert staged["sha256"] == expected, "the staged vmlinux is not the uploaded file"
        resolved = await asyncio.to_thread(_drgn_release, config, system.system_id)
        assert resolved == release, f"drgn resolved {resolved!r}, not the running {release!r}"
        observed |= {
            "kernel_release": release,
            "vmlinux_sha256": staged["sha256"],
            "drgn_resolved_release": resolved,
        }
    finally:
        if activation is not None:
            job = await release_external_boot(system.op, activation.run_id)
            await drain_job(system.op, "release", job.object_id)
    assert activation is not None
    await asyncio.to_thread(run_as_authority, config, _DOMAIN_POWER, "start", system.system_id)
    baseline = await asyncio.to_thread(_wait_for_agent, config, system.system_id)
    restored = await asyncio.to_thread(_vmlinux_facts, config, system.system_id, release)
    await asyncio.to_thread(run_as_authority, config, _DOMAIN_POWER, "stop", system.system_id)
    assert restored["present"] == "False", "the restore left the staged vmlinux behind"
    observed |= {"baseline_release": baseline, "vmlinux_after_restore": "absent"}
    observed["uploaded_vmlinux_bytes"] = (tree / "vmlinux").stat().st_size
    print("vmlinux-staging-observed " + json.dumps(observed, sort_keys=True))
    return observed


def run_remote_authority_vmlinux(tmp_path: Path) -> None:
    if os.environ.get(REMOTE_CONFIG_ENV) is None:
        pytest.skip(f"{REMOTE_CONFIG_ENV} unset; the remote authority lane is not configured")
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    if not db_url:
        pytest.fail('set KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"')
    tree_value = os.environ.get(_KERNEL_TREE_ENV)
    if not tree_value or not (Path(tree_value) / "vmlinux").is_file():
        pytest.fail(f"{_KERNEL_TREE_ENV} must name a built tree holding an unstripped vmlinux")
    tree = Path(tree_value)
    arch = remote_authority_inventory(select_remote_lane()).arch
    if arch not in SUPPORTED_ARCHES:
        pytest.fail(f"unsupported remote authority architecture: {arch}")
    cell = Cell(
        f"authority-vmlinux-staging-{arch}",
        "tool/authority-vmlinux-staging",
        3131,
        "authority-frame",
        "vmlinux staging proof",
        ("effect", "cleanup"),
        provider="remote-libvirt",
        host_arch=arch,
        guest_arch=arch,
    )
    run = CellRun(cell, EvidenceWriter(tmp_path))
    project = f"cov-{secrets.token_hex(4)}"
    base_url, issuer = require_stack(), require_issuer()

    async def body(system: AuthoritySystem) -> dict[str, object]:
        return await _prove(system, tree, arch)

    asyncio.run(on_authority_system(run, base_url, issuer, db_url, project=project, body=body))
    assert set(run.assertions) == {"effect", "cleanup"}
