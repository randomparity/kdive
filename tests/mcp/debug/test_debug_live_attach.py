"""Live end-to-end proof for #747 (ADR-0233): the real ``debug.start_session`` handler opens a
live gdbstub session against a real early-boot-panicked, preserved libvirt domain.

Kept in its own module (not ``test_debug_tools.py``) so the live marker does not make the
behaviour-test-coverage gate treat the ``debug.*`` covering test as live-only. Reuses the public
debug-session seed helpers and the shared ``migrated_url`` Postgres fixture.

`live_vm`-gated (bzimage family, ADR-0392): the operator points ``KDIVE_LIVE_VM_BZIMAGE`` at a
kernel image that panics early in boot (no usable rootfs), optionally overriding
``KDIVE_LIBVIRT_URI`` (default ``qemu:///session`` so it needs no root).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kdive.domain.capacity.state import SystemState
from kdive.mcp.tools.debug.sessions import lifecycle as debug_tools
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.testing.live_vm import boot_gdbstub_domain
from tests.live_vm import require_live_vm_bzimage
from tests.mcp.debug.live_support import render_panicking_domain
from tests.mcp.debug.session_support import (
    PROFILE_POLICY,
    granted_allocation,
    pool,
    request_context,
    seed_run,
    seed_system,
)
from tests.mcp.systems_support import provider_resolver


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_vm_start_session_attaches_to_halted_early_boot_crash(  # pragma: no cover
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real Postgres + the real start_session handler + the real LocalLibvirtConnect connector
    (resolves the gdb port from the live domain XML, runs the real rsp_reachable probe) open a
    live gdbstub session against a real KVM domain that VFS-panics on an empty disk. Only the
    boot-step row is seeded directly (its recording is unit-tested separately)."""
    contract = require_live_vm_bzimage()
    try:
        import libvirt  # noqa: F401, PLC0415  # operator-provided; presence gates the live boot
    except ImportError:
        pytest.skip("libvirt-python unavailable")

    monkeypatch.setenv("KDIVE_LIBVIRT_URI", contract.libvirt_uri)
    disk = tmp_path / "garbage.qcow2"
    console = tmp_path / "console.log"
    console.write_text("")
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(disk), "1G"], check=True, capture_output=True
    )

    final_xml = render_panicking_domain(bzimage=str(contract.bzimage), disk=disk, console=console)

    async def _drive() -> Any:
        async with pool(migrated_url) as db_pool:
            alloc_id = await granted_allocation(db_pool)
            sys_id = await seed_system(db_pool, alloc_id, SystemState.READY)
            run_id = await seed_run(
                db_pool, sys_id, boot_result={"boot_outcome": "crashed_halted_live"}
            )
            handlers = debug_tools.DebugSessionHandlers.from_resolver(
                provider_resolver(
                    connector=LocalLibvirtConnect.from_env(),
                    profile_policy=PROFILE_POLICY,
                    supported_debug_transports=frozenset({"gdbstub"}),
                ),
                runtime_resolver=None,
                secret_registry=SecretRegistry(),
            )
            resp = await handlers.start_session(
                db_pool, request_context(), run_id=run_id, transport="gdbstub"
            )
            if resp.status == "live":
                await handlers.end_session(db_pool, request_context(), resp.object_id)
            return resp

    with boot_gdbstub_domain(
        final_xml,
        uri=contract.libvirt_uri,
        wait_for="panic",
        console_log=console,
    ):
        resp = asyncio.run(_drive())
        assert resp.status == "live", f"start_session did not attach: {resp.status} {resp.data}"
