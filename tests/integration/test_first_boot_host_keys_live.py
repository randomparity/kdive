"""Operator-run regression proof: a System's first-boot host keys survive `runs.boot` (#2757).

``live_vm``-gated, with the same prerequisites as ``test_console_parts_live.py``: the live stack
(KDIVE_STACK_BASE_URL, KDIVE_OIDC_ISSUER, KDIVE_DATABASE_URL), a local KVM host, a kdive-ready
guest image (KDIVE_GUEST_IMAGE) and a built kernel tree (KDIVE_KERNEL_SRC).

A new System's first boot writes its SSH host keys and cloud-init's once-per-instance markers.
Before ADR-0679, a `runs.boot` issued right after provisioning hard-destroyed the domain while
those writes were still in the guest page cache: the keys survived as 0-byte files, the markers
survived intact, and sshd never started again. This test provisions, installs a `console`-method
Run and boots it with no wait in between, then requires sshd to answer over the loopback forward
and every ``/etc/ssh/ssh_host_*`` file to be non-empty.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from uuid import UUID

import libvirt
import psycopg
import pytest

from kdive.mcp.dev_harness import LiveStackClient
from kdive.providers.shared.libvirt_xml import recorded_ssh_port
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.system_bootstrap_key import (
    load_system_bootstrap_private_key,
    materialized_private_key,
)
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.spine import (
    LOCAL_ALLOCATION_DISK_GB,
    await_system_state,
    build_and_upload_kernel,
    build_profile,
    drain_job,
    mint_role_token,
    ok,
    phase,
    scalar,
    seed_metering,
    worker_libvirt_uri,
)
from tests.live_vm import require_native_guest_arch
from tests.mcp.json_data import data_str

pytestmark = pytest.mark.live_vm

_PROJECT = "first-boot-keys-proof"
_AGENT_SESSION = "first-boot-keys-sess"
# Exit 0 only when host keys exist and none of the ssh_host_* files is empty.
_KEY_CHECK = (
    "ls /etc/ssh/ssh_host_*_key >/dev/null && "
    "! find /etc/ssh -maxdepth 1 -name 'ssh_host_*' -size 0 | grep -q ."
)
_SSH_DEADLINE_S = 60.0


def _require_env() -> tuple[str, str]:
    image = os.environ.get("KDIVE_GUEST_IMAGE")
    if not image or not Path(image).exists():
        # Fail, never skip: the native tier has no summary gate, so a skip reads as a pass (#2518).
        pytest.fail("KDIVE_GUEST_IMAGE unset or missing; build the rootfs with `kdive build-fs`")
    tree = os.environ.get("KDIVE_KERNEL_SRC")
    if not tree or not Path(tree).exists():
        pytest.skip("KDIVE_KERNEL_SRC unset or missing; fetch and build the kernel tree first")
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    if not db_url:
        pytest.skip("KDIVE_DATABASE_URL unset; bring up the stack (see the live-stack runbook)")
    return image, db_url


def _console_profile(arch: str, image: str) -> dict[str, object]:
    # No crashkernel and no debug section, so the install method resolves to `console`.
    return {
        "schema_version": 1,
        "arch": arch,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ["KDIVE_KERNEL_SRC"],
        "provider": {"local-libvirt": {"rootfs": {"kind": "local", "path": image}}},
    }


def _check_host_keys(port: int, key_path: Path) -> subprocess.CompletedProcess[bytes]:
    """SSH in over the loopback forward and run the host-key check, retrying until the deadline."""
    argv = [
        "ssh",
        "-i",
        str(key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(port),
        "root@127.0.0.1",
        "--",
        _KEY_CHECK,
    ]
    deadline = time.monotonic() + _SSH_DEADLINE_S
    while True:
        result = subprocess.run(argv, capture_output=True, timeout=60.0, check=False)  # noqa: S603
        # 255 is ssh's own failure (sshd not answering); anything else is the check's verdict.
        if result.returncode != 255 or time.monotonic() >= deadline:
            return result
        time.sleep(5.0)


def test_first_boot_host_keys_survive_immediate_boot() -> None:
    """Provision → install (console) → boot with no wait; sshd answers and keys are non-empty."""
    image, db_url = _require_env()
    issuer = require_issuer()
    base_url = require_stack()
    arch = require_native_guest_arch()
    token = mint_role_token(issuer, project=_PROJECT, agent_session=_AGENT_SESSION, role="operator")

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, token)
        async with op:
            await seed_metering(db_url, _PROJECT)
            async with phase("allocate"):
                env = ok(
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
                )
                allocation_id = env.object_id
            try:
                async with phase("provision"):
                    env = ok(
                        await scalar(
                            op,
                            "systems.provision",
                            allocation_id=allocation_id,
                            profile=_console_profile(arch, image),
                        ),
                        "provision",
                    )
                    system_id = data_str(env, "system_id")
                    await await_system_state(op, "provision", system_id, "ready")
                async with phase("create-run"):
                    inv = ok(
                        await scalar(
                            op, "investigations.open", project=_PROJECT, title="first-boot-keys"
                        ),
                        "open-investigation",
                    )
                    env = ok(
                        await scalar(
                            op,
                            "runs.create",
                            investigation_id=inv.object_id,
                            system_id=system_id,
                            build_profile=build_profile(arch),
                        ),
                        "create-run",
                    )
                    run_id = env.object_id
                async with phase("upload-build"):
                    await build_and_upload_kernel(
                        op, run_id=run_id, arch=arch, root_fs="ext4", require_network=True
                    )
                for step in ("install", "boot"):
                    async with phase(step):
                        env = ok(await scalar(op, f"runs.{step}", run_id=run_id), step)
                        await drain_job(op, step, env.object_id)
                async with phase("check-host-keys"):
                    await asyncio.to_thread(_assert_host_keys, db_url, UUID(system_id))
            finally:
                async with phase("release"):
                    ok(
                        await scalar(op, "allocations.release", allocation_id=allocation_id),
                        "release",
                    )

    asyncio.run(_run())


def _assert_host_keys(db_url: str, system_id: UUID) -> None:
    async def _key() -> str:
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            return await load_system_bootstrap_private_key(
                conn, system_id, secret_registry=SecretRegistry()
            )

    private_key = asyncio.run(_key())
    conn = libvirt.open(worker_libvirt_uri())
    try:
        port = recorded_ssh_port(conn.lookupByName(domain_name_for(system_id)).XMLDesc(0))
    finally:
        conn.close()
    assert port is not None, "no SSH hostfwd port in the domain XML (ADR-0281)"
    with materialized_private_key(private_key) as key_path:
        result = _check_host_keys(port, key_path)
    assert result.returncode == 0, (
        "after an immediate runs.boot, sshd did not answer (exit 255) or a host key is missing "
        f"or 0 bytes (#2757): exit={result.returncode} "
        f"stderr={result.stderr.decode(errors='replace')!r}"
    )
