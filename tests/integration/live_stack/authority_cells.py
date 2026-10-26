"""Functional authority tool cells on the authority System frame (#3186, ADR-0788).

The run and break-glass carriers frame the functional cells of ``runs.release_external_boot``,
``ops.force_release``, ``ops.resolve_recovery_orphan`` and
``systems.resolve_external_boot_conflict`` through :func:`run_authority_cell`. A cell whose
construction waits on a product change stops ``blocked`` after its lane gates and before any
mutation, with that change's reason. Every other cell runs its body inside
:func:`~tests.integration.live_stack.authority_lane.on_authority_system`, uploading the bound
``longterm`` fixture, and calls the tool under test through the cell's exposure.
"""

from __future__ import annotations

import asyncio
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg

from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack import authority_lane
from tests.integration.live_stack.authority_faults import (
    CONFLICT_BLOCKED,
    FIXTURE_ORPHAN_BLOCKED,
    REMOTE_ORPHAN_BLOCKED,
    ToolCall,
    activate,
    activation_of,
    blocked_after_gates,
    job_claim,
    receipt_state,
)
from tests.integration.live_stack.authority_lane import (
    AuthoritySystem,
    authority_inventory,
    observe_remote_authority,
    on_authority_system,
)
from tests.integration.live_stack.deep_lifecycle import uploaded_kernel_inputs
from tests.integration.live_stack.remote_lifecycle import observe_host, remote_host
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.service_cells import _fixture
from tests.integration.live_stack.spine import drain_job, ok
from tests.integration.live_stack.tool_cells import Caller, Grants
from tests.live_vm.installed_local_authority_support import (
    NormalOperationJobs,
    assert_root_release_completion,
    release_external_boot,
)
from tests.live_vm.installed_remote_authority_support import (
    RemoteAuthorityProofConfig,
    remote_authority_inventory,
)

_REMOTE = "remote-libvirt"
_RELEASE = "runs.release_external_boot"
# Cells whose construction waits on a product change; the reason names its owner.
PRODUCT_BLOCKERS: dict[tuple[str, str], str] = {
    ("ops.resolve_recovery_orphan", "local-libvirt"): FIXTURE_ORPHAN_BLOCKED,
    ("ops.resolve_recovery_orphan", _REMOTE): REMOTE_ORPHAN_BLOCKED,
    ("systems.resolve_external_boot_conflict", "local-libvirt"): CONFLICT_BLOCKED,
    ("systems.resolve_external_boot_conflict", _REMOTE): CONFLICT_BLOCKED,
}
_ADMISSION_ATTEMPTS = 3
_RELEASE_JOBS: LiteralString = (
    "SELECT id::text FROM jobs WHERE payload->'external_boot_authority_v1'->>'run_id' = %s "
    "AND payload->'external_boot_authority_v1'->>'operation' = 'release' ORDER BY created_at"
)
_ACTIVATIONS: LiteralString = "SELECT count(*) FROM external_boot_activations WHERE run_id = %s"

type CellBody = Callable[[AuthoritySystem, Path, dict[str, Any]], Awaitable[dict[str, object]]]


def exposed(caller: Caller, token: str) -> ToolCall:
    """Call a tool through ``caller``'s exposure; error envelopes come back unchanged.

    A held arm must observe a refusal, so nothing here asserts success. ``gateway`` first finds
    the tool with ``tools.search``.
    """

    async def call(tool: str, args: dict[str, object]) -> ToolResponse:
        result = await caller.call(tool, args, token, discover=True)
        assert isinstance(result, ToolResponse), f"{tool} answered {len(result)} envelopes"
        return result

    return call


def contributor_grants(project: str) -> Grants:
    """The release cell's caller: a contributor of the frame's project (#3186)."""
    return Grants(f"{project}-contributor", (project,), {project: "contributor"})


def platform_admin_grants() -> Grants:
    """The ops cells' caller: a platform admin with a project of its own, outside the frame's."""
    project = f"cov-{secrets.token_hex(4)}"
    return Grants(f"{project}-platform_admin", (project,), {}, ("platform_admin",))


async def run_authority_cell(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    body: CellBody,
    *,
    project: str,
) -> None:
    """Stop a product-blocked cell after its lane gates; frame ``project``'s System otherwise."""
    reason = PRODUCT_BLOCKERS.get((run.cell.operation, run.cell.provider))
    remote = run.cell.provider == _REMOTE
    if reason is not None:
        if remote:
            # A read-only probe, so the blocked record carries the provider host like the rest.
            observe_host(run, await asyncio.to_thread(remote_host))
        blocked_after_gates(run, base_url, reason)
    tree, manifest = _fixture()

    async def framed(system: AuthoritySystem) -> dict[str, object]:
        if remote:
            await observe_remote_authority(run, system, db_url)
        return await body(system, tree, manifest)

    await on_authority_system(run, base_url, issuer, db_url, project=project, body=framed)


async def _rows(db_url: str, sql: LiteralString, run_id: str) -> list[tuple[object, ...]]:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(sql, (run_id,))
        return list(await cursor.fetchall())


async def release_jobs(db_url: str, run_id: str) -> list[str]:
    """The release jobs the authority enqueued for ``run_id``."""
    return [str(row[0]) for row in await _rows(db_url, _RELEASE_JOBS, run_id)]


async def activated(db_url: str, run_id: str) -> bool:
    """Whether ``run_id`` has an external-boot activation row."""
    rows = await _rows(db_url, _ACTIVATIONS, run_id)
    return bool(rows) and int(str(rows[0][0])) > 0


async def settle_release(system: AuthoritySystem, db_url: str) -> None:
    """Release every external boot the body started, judged from the database.

    A tool call can raise after the server admitted it, and an activation can raise after its
    boot was admitted, so the client's view never decides: an admitted release is drained, an
    activation with no release is released through the frame's own client.
    """
    for resource in system.ledger.resources:
        if resource.kind != "run":
            continue
        jobs = await release_jobs(db_url, resource.identity)
        if not jobs and await activated(db_url, resource.identity):
            jobs = [(await release_external_boot(system.op, resource.identity)).object_id]
        for job_id in jobs:
            await drain_job(system.op, "release-cleanup", job_id)


async def _admit_release(call: ToolCall, system: AuthoritySystem, run_id: str) -> str:
    """Admit the release through ``call``, draining a System job its conflict names first."""
    for _attempt in range(_ADMISSION_ATTEMPTS):
        envelope = await call(_RELEASE, {"run_id": run_id})
        data = envelope.data or {}
        if envelope.status != "error" or data.get("reason") != "system_job_active":
            break
        for job_id in cast("list[str]", data.get("job_ids", [])):
            await drain_job(system.op, "release", job_id)
    return ok(envelope, "release").object_id


def _authority_state(system: AuthoritySystem) -> dict[str, object]:
    """What a release must give back: the System's private storage and its defined domain.

    Locally that is the private rootfs root; the console root is left to the frame, which
    compares the whole lane inventory once the System is gone.
    """
    config = system.config
    if isinstance(config, RemoteAuthorityProofConfig):
        remote = remote_authority_inventory(config, system.system_id)
        owned = sorted(v for v in remote.volumes if system.system_id in v)
        return {"volumes": owned, "domain": remote.system is not None}
    local = authority_inventory(system.system_id)
    rootfs = local.roots[authority_lane._AUTHORITY_ROOTFS]
    return {"rootfs": sorted(rootfs), "domain": local.system is not None}


async def released_external_boot(
    run: CellRun,
    call: ToolCall,
    db_url: str,
    system: AuthoritySystem,
    tree: Path,
    manifest: dict[str, Any],
) -> dict[str, object]:
    """Activate one external boot, release it through ``call``, and observe the reclamation."""
    remote = isinstance(system.config, RemoteAuthorityProofConfig)
    before = await asyncio.to_thread(_authority_state, system)
    assert before["domain"], "the System's domain is not defined before the activation"
    with tempfile.TemporaryDirectory() as scratch:
        upload = Path(scratch) / "upload"
        try:
            activation = await activate(system, kernel_tree=tree, evidence_dir=upload)
            job = await _admit_release(call, system, activation.run_id)
            await drain_job(system.op, "release", job)
        except BaseException as primary:
            try:
                await settle_release(system, db_url)
            except Exception as cleanup:  # noqa: BLE001 - noted on the primary failure
                primary.add_note(f"release cleanup failed: {type(cleanup).__name__}")
            raise
        run.observed |= uploaded_kernel_inputs(tree, manifest, upload)
    state, _attempt = await job_claim(db_url, job)
    assert state == "succeeded", f"the release job is {state}"
    await assert_root_release_completion(
        db_url,
        NormalOperationJobs(
            activation.investigation_id, activation.run_id, activation.activate_job_id, job
        ),
    )
    after = await asyncio.to_thread(_authority_state, system)
    assert after == before, f"the release left the authority state changed: {before} -> {after}"
    receipt = "not-applicable"
    if not remote:
        activation_id = await activation_of(db_url, activation.run_id)
        present = (await asyncio.to_thread(receipt_state, system.system_id, activation_id)).present
        assert not present, "the private cleanup receipt survived the release"
        receipt = "absent"
    return {
        "release_job": state,
        "root_release": "complete",
        "cleanup_receipt": receipt,
        "authority_state": "restored",
    }
