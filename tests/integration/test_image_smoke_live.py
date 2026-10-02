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
from functools import partial
from pathlib import Path
from uuid import UUID

import pytest

from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.providers.shared.libvirt_xml import recorded_ssh_port
from kdive.providers.shared.runtime_paths import console_log_path, read_console_log
from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.image_smoke import (
    native_cells,
    os_matches,
    ssh,
    toolchain_command,
)
from tests.integration.live_stack.scenario import (
    CellRun,
    authorize_ssh,
    domain_xml,
    on_catalog_system,
    probe_new_boot,
    run_cell,
    ssh_probe,
)
from tests.integration.live_stack.spine import await_system_state, drain_job, ok, scalar

pytestmark = pytest.mark.live_stack

_CATALOG = load_rootfs_catalog()


async def _scenario(
    run: CellRun, op: LiveStackClient, system_id: str, owned: list[str], tmp_path: Path
) -> None:
    entry = _CATALOG[str(run.cell.image)]
    xml = domain_xml(system_id)
    assert recorded_ssh_port(xml) is not None, "no loopback SSH forward in the domain XML"
    marker = b"kdive-ready" in read_console_log(console_log_path(UUID(system_id)))
    assert marker, "System reached ready without the kdive-ready first-boot marker"
    run.prove("first-boot", {"state": "ready", "console_marker": "kdive-ready"})
    endpoint, key = await asyncio.wait_for(
        authorize_ssh(op, system_id, tmp_path, "image-smoke"), timeout=900
    )
    first = await asyncio.to_thread(ssh_probe, endpoint, key)
    assert first.get("uid") == "0", f"ssh as root reported uid {first.get('uid')!r}"
    run.prove("authenticated-access", {"user": "root", "uid": first["uid"]})
    os_release = {k: first.get(k) for k in ("ID", "VERSION_ID", "machine")}
    assert os_matches(entry, first), f"guest {os_release} is not catalog {entry.distro}"
    run.observed |= {"guest_os": f"{entry.distro}:{entry.version}", "guest_arch": entry.arch}
    run.prove("os-architecture", {"observed": os_release})
    if "build-toolchain" in run.cell.assertions:
        result = await asyncio.to_thread(ssh, endpoint, key, toolchain_command(entry))
        assert result.returncode == 0, f"toolchain check exit {result.returncode}"
        run.prove("build-toolchain", {"packages_and_build": "ok"})
    env = ok(await scalar(op, "control.power", system_id=system_id, action="cycle"), "reboot")
    await drain_job(op, "reboot", env.object_id)
    await await_system_state(op, "reboot", system_id, "ready")
    second = await asyncio.to_thread(probe_new_boot, endpoint, key, first["boot_id"])
    assert second.get("uid") == "0", "ssh after reboot did not authenticate as root"
    run.prove("reboot", {"action": "cycle", "boot_id_changed": True})


async def _smoke(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path
) -> None:
    await on_catalog_system(
        run,
        base_url,
        issuer,
        db_url,
        project="image-smoke",
        image=str(run.cell.image),
        body=partial(_scenario, run, tmp_path=tmp),
    )


@pytest.mark.parametrize("cell", native_cells(), ids=lambda cell: str(cell.image))
def test_image_smoke(cell: Cell, tmp_path: Path) -> None:
    """Acquire → first boot → authenticate → OS/arch → (toolchain) → reboot → cleanup."""
    run_cell(cell, partial(_smoke, tmp=tmp_path))
