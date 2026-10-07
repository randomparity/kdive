"""Prove the x86_64 break-glass provider tool cells over HTTP on both providers (#3112).

``live_stack``-marked (ADR-0722). One parameter per native local-libvirt contract cell and per
x86_64 remote-libvirt contract cell of ``ops.force_release``, ``ops.force_teardown``,
``ops.resolve_recovery_orphan`` and ``systems.resolve_external_boot_conflict``, framed by
:func:`~tests.integration.live_stack.tool_cells.run_tool_cell`. A functional ``force_teardown``
cell provisions its provider's lane image in a fresh ``cov-<hex>`` project through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, tears it down as a platform admin
outside that project, and proves the owned domain and disks gone while the provider's other
domains and the lane's staged base stay. The other functional cells stop ``blocked`` (operator
decisions of 2026-10-07): the resolve tools need an installed provider authority, an
authority-owned System and a constructed orphan or conflict, and the ``force_release`` ordering
exists only for an authority-owned System with external-boot history. A rejection cell aims at
the stack's :func:`~tests.integration.live_stack.tool_cells.lane_target` for its provider.
``docs/operating/runbooks/live-testing.md`` covers both lanes.
"""

from __future__ import annotations

import asyncio
import platform
import secrets
from collections.abc import Callable, Mapping
from functools import partial
from typing import NoReturn, cast
from uuid import UUID, uuid4

import libvirt
import psycopg
import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.providers.shared.runtime_paths import domain_name_for
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.image_smoke import staged_image
from tests.integration.live_stack.remote_lifecycle import (
    REMOTE_REPRESENTATIVES,
    observe_host,
    observer,
    remote_host,
    remote_kdive_domains,
    staged_base_volume,
)
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.spine import await_system_state, drain_job, worker_libvirt_uri
from tests.integration.live_stack.tool_cells import (
    LANE_IMAGES,
    REMOTE_LANE_FAMILIES,
    Boundary,
    Exposure,
    Grants,
    Guest,
    HttpCaller,
    LaneTarget,
    Rejection,
    boundary_of,
    lane_target,
    on_lane_system,
    one,
    project_state,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

TOOLS = (
    "ops.force_release",
    "ops.force_teardown",
    "ops.resolve_recovery_orphan",
    "systems.resolve_external_boot_conflict",
)
_RELEASE = "ops.force_release"
_TEARDOWN = "ops.force_teardown"
_CONFLICT = "systems.resolve_external_boot_conflict"
_REMOTE = "remote-libvirt"
_REASON = "coverage #3112"
_NO_ORDERING = (
    "ops.force_release's observed ordering (terminal only after protected provider effects are "
    "quiescent) exists only for an authority-owned System with external-boot history; no lane "
    "frames one"
)
_NO_AUTHORITY = (
    "{tool} needs an installed provider authority, an authority-lane System frame and a harness "
    "that constructs a quarantined recovery orphan or a recovery conflict; the demo-up lane has "
    "none of them"
)
_NO_REMOTE_AUTHORITY = (
    "{tool} needs a remote provider authority (provider_authority_host and the [[remote_libvirt]] "
    "authority tuple), an authority-lane System frame and a harness that constructs a "
    "quarantined recovery orphan or a recovery conflict; an authority would route every remote "
    "install and boot through external boot"
)


def _platform(role: str) -> Grants:
    """A ``role`` platform principal with a fresh project of its own and no role in any other."""
    project = f"cov-{secrets.token_hex(4)}"
    return Grants(f"{project}-{role}", (project,), {}, (role,))


def _grants(project: str, role: str) -> Grants:
    return Grants(f"{project}-{role}", (project,), {project: role})


def _stranger() -> Grants:
    other = f"cov-{secrets.token_hex(4)}"
    return _grants(other, "operator")


async def _call(
    caller: HttpCaller, tool: str, args: Mapping[str, object], token: str
) -> ToolResponse:
    return one(await caller.call(tool, args, token, discover=True))


async def _audit_rows(db_url: str, principal: str, tool: str, scope: str) -> int:
    """``platform_audit_log`` rows ``principal`` wrote for ``tool`` on ``scope``."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*) FROM platform_audit_log "
            "WHERE principal = %s AND tool = %s AND scope = %s",
            (principal, tool, scope),
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


def _volume_present(conn: libvirt.virConnect, pool: str, volume: str) -> bool:
    found = conn.storagePoolLookupByName(pool)
    found.refresh(0)
    try:
        found.storageVolLookupByName(volume)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
            return False
        raise
    return True


def _inventory(cell: Cell) -> tuple[set[str], bool]:
    """The provider's ``kdive-`` domains, and whether the lane's staged base still exists."""
    if cell.provider == _REMOTE:
        host = remote_host()
        image = REMOTE_REPRESENTATIVES[REMOTE_LANE_FAMILIES[str(cell.guest_arch)]]
        conn = observer(host.dest)
        try:
            present = _volume_present(conn, host.pool, staged_base_volume(image))
            return remote_kdive_domains(conn), present
        finally:
            conn.close()
    conn = libvirt.open(worker_libvirt_uri())
    try:
        domains = remote_kdive_domains(conn)
    finally:
        conn.close()
    return domains, staged_image(LANE_IMAGES[platform.machine()]) is not None


def _defined(xml: Callable[[str], str], system_id: str) -> bool:
    try:
        xml(system_id)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return False
        raise
    return True


async def _force_teardown(
    run: CellRun, caller: HttpCaller, db_url: str, guest: Guest
) -> dict[str, object]:
    """Tear the frame's System down as an outside platform admin; unrelated resources stay."""
    admin = _platform("platform_admin")
    domain = domain_name_for(UUID(guest.system_id))
    domains, base = await asyncio.to_thread(_inventory, run.cell)
    assert domain in domains, "the provider does not list the frame's domain"
    assert base, "the lane's staged base is missing before the teardown"
    args = {"system_id": guest.system_id, "reason": _REASON}
    env = await _call(caller, _TEARDOWN, args, caller.token(admin))
    if env.status != "torn_down":
        await drain_job(guest.op, "force-teardown", env.object_id)
    await await_system_state(guest.op, "force-teardown", guest.system_id, "torn_down")
    assert not await asyncio.to_thread(_defined, guest.lane.xml, guest.system_id), (
        "the domain is still defined"
    )
    surviving = [path for path in guest.owned if not guest.lane.absent(path)]
    assert not surviving, f"owned disk(s) survived the forced teardown: {surviving}"
    after, kept = await asyncio.to_thread(_inventory, run.cell)
    assert after == domains - {domain}, (
        f"provider domains changed beyond the torn-down one: {sorted(after ^ domains)}"
    )
    assert kept, "the lane's staged base did not survive the forced teardown"
    scope = f"{guest.project}:{guest.system_id}"
    rows = await _audit_rows(db_url, admin.subject, _TEARDOWN, scope)
    assert rows == 1, f"{rows} platform_audit_log rows for the forced teardown"
    return {
        "system": "torn_down",
        "domain": "absent",
        "disks_absent": len(guest.owned),
        "other_domains_kept": len(after),
        "staged_base": "present",
        "platform_audit_rows": rows,
    }


def _blocked(run: CellRun) -> NoReturn:
    tool = run.cell.operation
    reason = _NO_ORDERING if tool == _RELEASE else _NO_AUTHORITY.format(tool=tool)
    if run.cell.provider == _REMOTE:
        # A read-only probe, so the blocked record carries the provider host like the rest.
        observe_host(run, remote_host())
        if tool != _RELEASE:
            reason = _NO_REMOTE_AUTHORITY.format(tool=tool)
    raise ScenarioStop(Outcome.BLOCKED, reason)


def _args(tool: str, system_id: str, allocation_id: str) -> dict[str, object]:
    if tool == _RELEASE:
        return {"allocation_id": allocation_id, "reason": _REASON}
    if tool == _TEARDOWN:
        return {"system_id": system_id, "reason": _REASON}
    if tool == "ops.resolve_recovery_orphan":
        return {
            "system_id": system_id,
            "object_identities": ["cov-orphan"],
            "disposition": "delete",
        }
    return {
        "system_id": system_id,
        "operation": "restore-recorded-source",
        "observed_identity": "sha256:" + "0" * 64,
    }


def _rejection(tool: str, boundary: Boundary, target: LaneTarget) -> Rejection:
    args = _args(tool, target.system_id, target.allocation_id)
    key = "allocation_id" if tool == _RELEASE else "system_id"
    if tool != _CONFLICT:
        # The ops tools gate on platform_admin before resolving the object; platform_operator is
        # the platform role below it, and its issued-token call writes only its denial audit row.
        if boundary == "validation":
            return Rejection({**args, key: 7}, _platform("platform_admin"))
        return Rejection(args, _platform("platform_operator"))
    if boundary == "validation":
        return Rejection({**args, key: 7}, _grants(target.project, "admin"))
    if boundary == "authentication":
        return Rejection(args, _grants(target.project, "viewer"))
    if boundary == "authorization":
        return Rejection(args, _grants(target.project, "contributor"))
    # A System outside the caller's projects answers exactly like an absent one: not_found.
    twin = _args(tool, str(uuid4()), str(uuid4()))
    return Rejection(
        args, _stranger(), frozenset({ErrorCategory.NOT_FOUND.value}), absent_twin=twin
    )


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    caller = HttpCaller(cast(Exposure, run.cell.exposure), base_url, issuer)
    tool = run.cell.operation
    if run.cell.kind == "functional":
        if tool != _TEARDOWN:
            _blocked(run)
        project = f"cov-{secrets.token_hex(4)}"
        body = partial(_force_teardown, run, caller, db_url)
        await on_lane_system(run, base_url, issuer, db_url, project=project, body=body)
        return
    target = await lane_target(run, base_url, issuer, db_url)
    boundary = boundary_of(run.cell)
    snapshot = partial(project_state, db_url, target.project)
    await prove_rejection(run, caller, boundary, _rejection(tool, boundary, target), snapshot)


def _cells() -> list[Cell]:
    host = platform.machine()
    return [
        c
        for c in tool_cells(TOOLS)
        if (c.provider == "local-libvirt" and c.guest_arch == host)
        or (c.provider == _REMOTE and c.guest_arch in REMOTE_LANE_FAMILIES)
    ]


@pytest.mark.parametrize("cell", _cells(), ids=lambda cell: cell.id)
def test_breakglass_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a break-glass provider tool."""
    run_tool_cell(cell, _scenario)
