"""Prove the x86_64 local and remote run and image tool cells over HTTP (#3119, #3120).

``live_stack``-marked (ADR-0722). One parameter per native local-libvirt contract cell, and per
remote-libvirt cell of a guest architecture with a remote lane, of
``runs.install``, ``runs.boot``, ``runs.cancel``, ``runs.release_external_boot`` and
``images.publish``, framed by :func:`~tests.integration.live_stack.tool_cells.run_tool_cell`. A
``runs.*`` functional cell provisions the lane image in a fresh ``cov-<hex>`` project through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, uploads the verified ``longterm``
kernel fixture and calls the tool in the cell's exposure. The functional ``images.publish`` cell
publishes its exposure's :data:`~tests.integration.live_stack.tool_cells.PUBLISHED_IMAGES` image
and boots it. The
release functional cells stop ``blocked``: the demo-up lane configures no external-boot
authority. A remote cell runs on the provider host's remote lane; its ``images.publish``
functional cell also stops ``blocked``, since no remote catalog image build exists. A ``runs.*``
rejection cell aims at one unbound Run of its provider in the stack's lane-target project; an
``images.publish`` one at the published image's name (the remote lane image's name remotely).
``docs/operating/runbooks/live-testing.md`` covers the local run, ``remote-live-stack.md`` the
remote one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import secrets
import tempfile
import xml.etree.ElementTree as ET  # noqa: S405 - the worker's own domain XML  # nosec B405
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest

from kdive.domain.errors import ErrorCategory
from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from scripts.kernel_fixtures import identity
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    boot_kernel_sha256,
    deep_body,
    file_sha256,
    load_fixture,
)
from tests.integration.live_stack.image_smoke import Endpoint, provenance_matches, ssh
from tests.integration.live_stack.remote_lifecycle import (
    REMOTE_REPRESENTATIVES,
    guest_boot_kernel,
    observe_host,
    remote_host,
)
from tests.integration.live_stack.scenario import CellRun, ScenarioStop, on_catalog_system
from tests.integration.live_stack.spine import (
    build_and_upload_kernel,
    build_profile,
    drain_job,
    mint_role_token,
    ok,
    scalar,
)
from tests.integration.live_stack.tool_cells import (
    PUBLISH_TOOL,
    PUBLISHED_IMAGES,
    REMOTE_LANE_FAMILIES,
    Boundary,
    Exposure,
    Grants,
    Guest,
    HttpCaller,
    Rejection,
    boundary_of,
    lane_target,
    observe_guest,
    on_lane_system,
    one,
    project_state,
    prove_rejection,
    run_tool_cell,
    settled,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

TOOLS = (
    "images.publish",
    "runs.boot",
    "runs.cancel",
    "runs.install",
    "runs.release_external_boot",
)
# The kernel fixture every runs.* cell uploads; write the bindings with
# ``--kernel-baseline longterm``.
BASELINE = "longterm"
_LOCAL = "local-libvirt"
_REMOTE = "remote-libvirt"
_BUILD_DEADLINE_S = 3600.0
_BOOT_ID = "cat /proc/sys/kernel/random/boot_id"
_NO_AUTHORITY = (
    "runs.release_external_boot needs a configured local external-boot authority and an "
    "authority-lane System frame; the demo-up lane installs neither"
)
_NO_REMOTE_AUTHORITY = (
    "runs.release_external_boot needs a remote provider authority (provider_authority_host and "
    "the [[remote_libvirt]] authority tuple) and an authority-lane System frame; no runbook "
    "provisions one, and an authority would route every remote install and boot through "
    "external boot"
)
_NO_REMOTE_PUBLISH = (
    "images.publish builds catalog images for local-libvirt only: the IMAGE_BUILD handler "
    "answers remote-libvirt with configuration_error (not implemented); remote base images are "
    "staged with deploy/ansible/playbooks/image.yml"
)


def _grants(project: str, role: str) -> Grants:
    return Grants(f"{project}-{role}", (project,), {project: role})


def _stranger() -> Grants:
    """An operator of a fresh project, holding no role in the target and no platform role."""
    other = f"cov-{secrets.token_hex(4)}"
    return _grants(other, "operator")


def _platform_operator() -> Grants:
    project = f"cov-{secrets.token_hex(4)}"
    return Grants(f"{project}-platform", (project,), {}, ("platform_operator",))


async def _call(
    caller: HttpCaller, tool: str, args: Mapping[str, object], token: str
) -> ToolResponse:
    return one(await caller.call(tool, args, token, discover=True))


def _fixture() -> tuple[Path, dict[str, Any]]:
    """The verified ``longterm`` fixture; ``blocked`` before any stack mutation when absent."""
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the {BASELINE} fixture"
        )
    try:
        return load_fixture(Path(root), BASELINE, platform.machine())
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{BASELINE} fixture: {exc}") from None


async def _create_run(
    op: LiveStackClient, investigation: str, system_id: str | None, provider: str = _LOCAL
) -> str:
    args: dict[str, object] = {
        "investigation_id": investigation,
        "build_profile": build_profile(platform.machine()),
    }
    # An unbound Run names the Resource kind it builds for; a bound one derives it.
    args |= {"system_id": system_id} if system_id else {"target_kind": provider}
    return ok(await scalar(op, "runs.create", **args), "create-run").object_id


async def _open(op: LiveStackClient, project: str, title: str) -> str:
    return ok(
        await scalar(op, "investigations.open", project=project, title=title), "open"
    ).object_id


async def _close(op: LiveStackClient, investigation: str) -> None:
    closed = await scalar(
        op, "investigations.close", investigation_id=investigation, summary="done"
    )
    assert closed.status == "closed", f"investigation not closed: {closed.status}"


def _domain_kernel(
    xml: Callable[[str], str], system_id: str, _endpoint: Endpoint, _key: Path, _release: str
) -> tuple[str, str]:
    """The ``<os><kernel>`` file the domain boots (the install's staged kernel) and its digest."""
    kernel = ET.fromstring(xml(system_id)).findtext("./os/kernel")  # noqa: S314  # nosec B314
    assert kernel, "the installed domain names no direct kernel"
    return file_sha256(kernel), kernel


async def _step(
    run: CellRun,
    caller: HttpCaller,
    token: str,
    tree: Path,
    manifest: dict[str, Any],
    guest: Guest,
) -> dict[str, object]:
    """Upload, install and boot; the tool under test goes through the cell's exposure."""
    tool = run.cell.operation

    async def through(step: str, run_id: str) -> ToolResponse:
        if f"runs.{step}" == tool:
            return await _call(caller, tool, {"run_id": run_id}, token)
        return await scalar(guest.op, f"runs.{step}", run_id=run_id)

    # deep_body proves its own assertion names; they back this cell's `effect`, not its record.
    deep = CellRun(run.cell, run.writer, observed=run.observed)
    tmp = guest.scratch / "deep"
    tmp.mkdir()
    await deep_body(
        deep,
        guest.op,
        guest.system_id,
        guest.owned,
        project=guest.project,
        entry=guest.lane.entry,
        tree=tree,
        manifest=manifest,
        tmp=tmp,
        # A remote install is in-guest: the kernel lands in the guest's own /boot.
        installed_kernel=partial(
            guest_boot_kernel, family=REMOTE_LANE_FAMILIES[guest.lane.entry.arch]
        )
        if guest.lane.provider == _REMOTE
        else partial(_domain_kernel, guest.lane.xml),
        step=through,
    )
    run.artifacts.extend(deep.assertions.values())
    return {"tool": tool, "proofs": dict(sorted(deep.assertions.items()))}


def _os_boot(xml: str) -> tuple[str | None, str | None]:
    os_element = ET.fromstring(xml).find("os")  # noqa: S314  # nosec B314
    assert os_element is not None, "the domain XML has no <os>"
    return os_element.findtext("kernel"), os_element.findtext("cmdline")


async def _boot_id(guest: Guest) -> str:
    result = await asyncio.to_thread(ssh, guest.endpoint, guest.key, _BOOT_ID)
    assert result.returncode == 0, f"boot_id read exited {result.returncode}"
    return result.stdout.strip()


async def _cancel(
    run: CellRun,
    caller: HttpCaller,
    token: str,
    tree: Path,
    manifest: dict[str, Any],
    guest: Guest,
) -> dict[str, object]:
    """Cancel an uploaded, uncompleted Run: its System is untouched and freed, its build closed.

    Only a ``created`` or ``running`` Run is cancelable; ``runs.complete_build`` would make it
    ``succeeded``, which ``runs.cancel`` answers with ``conflict``.
    """
    op = guest.op
    upload = guest.scratch / "upload"
    investigation = await _open(op, guest.project, "run cancel")
    try:
        run_id = await _create_run(op, investigation, guest.system_id)
        await build_and_upload_kernel(
            op,
            run_id=run_id,
            arch=manifest["arch"],
            kernel_tree=tree,
            evidence_dir=upload,
            with_vmlinux=True,
            require_network=True,
            root_fs="ext4",
            complete=False,
        )
        boot = _os_boot(guest.lane.xml(guest.system_id))
        boot_id = await _boot_id(guest)
        held = await scalar(
            op,
            "runs.create",
            investigation_id=investigation,
            system_id=guest.system_id,
            build_profile=build_profile(manifest["arch"]),
        )
        assert held.data.get("reason") == "system_has_live_run", f"second create: {held.data}"
        env = await _call(caller, "runs.cancel", {"run_id": run_id}, token)
        assert env.status == "canceled", f"runs.cancel answered {env.status}"
        after = ok(await scalar(op, "runs.get", run_id=run_id), "read-after")
        steps = cast(Mapping[str, object], after.data.get("steps") or {})
        ran = [s for s in ("install", "boot") if steps.get(s) == "succeeded"]
        assert after.status == "canceled" and not ran, f"Run {after.status}, steps ran {ran}"
        assert after.data.get("build_ref") is None, "the canceled Run carries a build"
        closed = await scalar(op, "runs.complete_build", run_id=run_id, build_id="0" * 40)
        assert closed.error_category is not None, "complete_build accepted a canceled Run"
        again = ok(await scalar(op, "runs.get", run_id=run_id), "read-again")
        assert again.status == "canceled", f"the Run became {again.status}"
        system = ok(await scalar(op, "systems.get", system_id=guest.system_id), "system")
        assert system.status == "ready", f"the System is {system.status}"
        assert _os_boot(guest.lane.xml(guest.system_id)) == boot, "the domain's boot changed"
        assert await _boot_id(guest) == boot_id, "the guest rebooted"
        freed = await _create_run(op, investigation, guest.system_id)
        ok(await scalar(op, "runs.cancel", run_id=freed), "cancel-freed")
    finally:
        await _close(op, investigation)
    record = json.loads((upload / "upload.json").read_text(encoding="utf-8"))
    run.observed |= {
        "kernel_sha256": boot_kernel_sha256(tree, manifest["arch"]),
        "kernel_build_id": record["build_id"],
        "kernel_source_sha": manifest["source"]["commit"],
        "kernel_config_sha256": file_sha256(upload / "effective_config"),
        "compiler_id": identity(manifest["toolchain"]),
    }
    return {
        "run": "canceled",
        "held_before_cancel": "system_has_live_run",
        "build": "never-completed",
        "complete_build_after": closed.error_category,
        "system": "ready",
        "guest_rebooted": False,
        "system_freed": True,
    }


async def _described(op: LiveStackClient, name: str, arch: str) -> ToolResponse:
    """``images.describe`` of local-libvirt catalog image ``name``."""
    cursor = None
    while True:
        args: dict[str, object] = {"request": {"cursor": cursor}} if cursor else {}
        listing = ok(await scalar(op, "images.list", **args), "describe")
        match = next(
            (
                item
                for item in listing.items
                if (item.data.get("provider"), item.data.get("name"), item.data.get("arch"))
                == (_LOCAL, name, arch)
            ),
            None,
        )
        if match is not None:
            return ok(await scalar(op, "images.describe", image_id=match.object_id), "describe")
        cursor = listing.data.get("next_cursor") if listing.data.get("truncated") else None
        assert cursor, f"{name} is not in the catalog after its publication"


async def _build_jobs(db_url: str, provider: str, name: str) -> int:
    """The number of ``IMAGE_BUILD`` jobs ``images.publish`` enqueued for ``provider``/``name``."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*) FROM jobs WHERE dedup_key = %s", (f"image_build:{provider}:{name}",)
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _pending_rows(db_url: str, name: str) -> int:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*) FROM image_catalog WHERE provider = %s AND name = %s "
            "AND state = 'pending'",
            (_LOCAL, name),
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _publish(
    run: CellRun, caller: HttpCaller, base_url: str, issuer: OidcIssuer, db_url: str
) -> None:
    """Publish the cell's image through its exposure, then boot it and prove the frame cleanup."""
    name = PUBLISHED_IMAGES.get((platform.machine(), run.cell.exposure))
    if name is None:
        raise ScenarioStop(Outcome.BLOCKED, f"no published image for {platform.machine()}")
    entry = load_rootfs_catalog()[name]
    # images.publish never recycles a job of one name: a prior job would be returned again, and
    # the cell would re-observe another publication.
    if await _build_jobs(db_url, _LOCAL, name):
        raise ScenarioStop(
            Outcome.BLOCKED, f"{name} was already published on this stack; wipe it first"
        )
    token = caller.token(_platform_operator())
    env = await _call(caller, PUBLISH_TOOL, {"provider": _LOCAL, "name": name}, token)
    # The build job's authorizing project is `platform`; a viewer there may wait on it.
    viewer = mint_role_token(issuer, project="platform", agent_session="cov-publish", role="viewer")
    async with LiveStackClient.over_http(base_url, viewer) as platform_client:
        await drain_job(platform_client, "publish", env.object_id, deadline_s=_BUILD_DEADLINE_S)
    project = f"cov-{secrets.token_hex(4)}"

    async def body(op: LiveStackClient, system_id: str, _owned: list[str]) -> None:
        described = await _described(op, name, entry.arch)
        digest = str(described.data.get("digest", ""))
        assert described.data.get("state") == "registered", "the published row is not registered"
        assert digest.startswith("sha256:") and len(digest) == 71, f"digest {digest!r}"
        assert await _pending_rows(db_url, name) == 0, f"a pending {name} row remains"
        provenance_matches(described, entry)
        with tempfile.TemporaryDirectory() as scratch:
            await observe_guest(run, op, system_id, Path(scratch), entry)
        run.prove(
            "effect",
            {
                "exposure": run.cell.exposure,
                "job": {"enqueued": env.status, "drained": "succeeded"},
                "image": name,
                "state": "registered",
                "digest": digest,
                "provenance": "matches-catalog",
                "guest": "matches-catalog",
            },
        )

    await on_catalog_system(run, base_url, issuer, db_url, project=project, image=name, body=body)
    # The published image is the cell's output, recorded in `effect`; it binds no input digest.
    run.observed["image_sha256"] = None


async def _functional(
    run: CellRun, caller: HttpCaller, base_url: str, issuer: OidcIssuer, db_url: str
) -> None:
    tool = run.cell.operation
    remote = run.cell.provider == _REMOTE
    blocked = {"runs.release_external_boot": _NO_AUTHORITY}
    if remote:
        blocked = {
            "runs.release_external_boot": _NO_REMOTE_AUTHORITY,
            PUBLISH_TOOL: _NO_REMOTE_PUBLISH,
        }
    if tool in blocked:
        if remote:
            # A read-only probe, so the blocked record carries the provider host like the rest.
            observe_host(run, remote_host())
        raise ScenarioStop(Outcome.BLOCKED, blocked[tool])
    if tool == PUBLISH_TOOL:
        await _publish(run, caller, base_url, issuer, db_url)
        return
    tree, manifest = _fixture()
    project = f"cov-{secrets.token_hex(4)}"
    token = caller.token(_grants(project, "contributor"))
    body = _cancel if tool == "runs.cancel" else _step
    await on_lane_system(
        run,
        base_url,
        issuer,
        db_url,
        project=project,
        body=partial(body, run, caller, token, tree, manifest),
    )


@dataclass(frozen=True)
class _RunTarget:
    """An unbound ``created`` Run in the lane target's project T: what ``runs.*`` cells aim at."""

    project: str
    run_id: str


_RUN_TARGETS: dict[tuple[str, str], _RunTarget | Exception] = {}


async def _unbound_run(
    base_url: str, issuer: OidcIssuer, db_url: str, project: str, provider: str
) -> str:
    token = mint_role_token(
        issuer, project=project, agent_session=f"{project}-sess", role="operator"
    )
    async with LiveStackClient.over_http(base_url, token) as op:
        run_id = await _create_run(op, await _open(op, project, "run target"), None, provider)
    await settled(db_url, project)
    return run_id


async def _run_target(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> _RunTarget:
    """The stack's target Run of the cell's provider, created on first use; a failure is replayed.

    Every ``runs.*`` handler resolves the Run and checks membership and the contributor role
    before any binding or state check, so an unbound Run is a valid target.
    """
    target = await lane_target(run, base_url, issuer, db_url)
    key = (base_url, run.cell.provider)
    cached = _RUN_TARGETS.get(key)
    if cached is None:
        try:
            run_id = await _unbound_run(base_url, issuer, db_url, target.project, key[1])
            cached = _RunTarget(target.project, run_id)
        except Exception as exc:  # noqa: BLE001 - remembered and replayed for every cell
            cached = exc
        _RUN_TARGETS[key] = cached
    if isinstance(cached, Exception):
        raise AssertionError(f"the target Run could not be prepared: {cached!r}")
    return cached


# install and boot answer a Run outside the caller's projects as configuration_error, cancel and
# release as not_found; each is identical to the answer for an absent run_id.
_ISOLATION = {
    "runs.boot": frozenset({ErrorCategory.CONFIGURATION_ERROR.value}),
    "runs.install": frozenset({ErrorCategory.CONFIGURATION_ERROR.value}),
    "runs.cancel": frozenset({ErrorCategory.NOT_FOUND.value}),
    "runs.release_external_boot": frozenset({ErrorCategory.NOT_FOUND.value}),
}


def _run_rejection(tool: str, boundary: Boundary, target: _RunTarget) -> Rejection:
    args = {"run_id": target.run_id}
    if boundary == "validation":
        return Rejection({"run_id": 7}, _grants(target.project, "contributor"))
    if boundary in ("authentication", "authorization"):
        # Viewer is one rank below the contributor gate; its issued-token call writes nothing.
        return Rejection(args, _grants(target.project, "viewer"))
    twin = {"run_id": str(uuid4())}
    return Rejection(args, _stranger(), _ISOLATION[tool], absent_twin=twin)


def _publish_rejection(boundary: Boundary, provider: str, name: str) -> Rejection:
    args = {"provider": provider, "name": name}
    if boundary == "validation":
        return Rejection({**args, "name": 7}, _platform_operator())
    return Rejection(args, _stranger())


async def _publish_state(db_url: str, provider: str, name: str) -> dict[str, object]:
    """The ``platform`` snapshot, the image's catalog rows and its build jobs.

    ``jobs`` has no project column, so ``project_state`` cannot see a leaked publish's job; the
    dedup-key count can.
    """
    platform_state = await project_state(db_url, "platform")
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*), coalesce(string_agg(t::text, ',' ORDER BY t::text), '') "
            "FROM image_catalog t WHERE t.provider = %s AND t.name = %s",
            (provider, name),
        )
        row = await cursor.fetchone()
    assert row is not None
    rows = [row[0], hashlib.sha256(str(row[1]).encode()).hexdigest()]
    jobs = await _build_jobs(db_url, provider, name)
    return {"platform": platform_state, "image_catalog": rows, "build_jobs": jobs}


def _rejected_image(cell: Cell) -> str:
    """The image name a publish rejection aims at: the published image, or the remote lane's."""
    if cell.provider == _REMOTE:
        return REMOTE_REPRESENTATIVES[REMOTE_LANE_FAMILIES[str(cell.guest_arch)]].name
    return PUBLISHED_IMAGES[(platform.machine(), cell.exposure)]


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    caller = HttpCaller(cast(Exposure, run.cell.exposure), base_url, issuer)
    if run.cell.kind == "functional":
        await _functional(run, caller, base_url, issuer, db_url)
        return
    boundary = boundary_of(run.cell)
    if run.cell.operation == PUBLISH_TOOL:
        await lane_target(run, base_url, issuer, db_url)  # the record's observed context
        name = _rejected_image(run.cell)
        snapshot = partial(_publish_state, db_url, run.cell.provider, name)
        rejection = _publish_rejection(boundary, run.cell.provider, name)
    else:
        target = await _run_target(run, base_url, issuer, db_url)
        snapshot = partial(project_state, db_url, target.project)
        rejection = _run_rejection(run.cell.operation, boundary, target)
    await prove_rejection(run, caller, boundary, rejection, snapshot)


def _cells() -> list[Cell]:
    host = platform.machine()
    return [
        c
        for c in tool_cells(TOOLS)
        if (c.provider == _LOCAL and c.guest_arch == host)
        or (c.provider == _REMOTE and c.guest_arch in REMOTE_LANE_FAMILIES)
    ]


@pytest.mark.parametrize("cell", _cells(), ids=lambda cell: cell.id)
def test_run_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a run or image lifecycle tool."""
    run_tool_cell(cell, _scenario)
