"""Bounded authority recovery faults for the authority-owned System frame (#3185, ADR-0787).

Each arm runs inside :func:`~tests.integration.live_stack.authority_lane.on_authority_system`
and binds its fault to the frame's exact System, the Run it activates, and one authority
operation through the root-only proof barrier (ADR-0622). It reads real journal, job, receipt
and provider evidence before it calls a recovery tool, and it calls that tool through the
caller it is handed, so a carrier chooses the exposure and role (#3186).

- :func:`held_teardown_release` holds a teardown after its provider effect and proves
  ``ops.force_release`` refuses until the effect is quiescent, then releases.
- :func:`quarantined_orphan` fails one cleanup finalization (ADR-0787) so the authority
  publishes a real quarantine row, then resolves exactly that row.
- :func:`recorded_conflict` changes the System's persistent definition out of band, as a host
  administrator would (ADR-0584), so the release refuses and the activation is recorded in
  ``recovery_conflict`` from the authority's journal head (ADR-0805); it then resolves it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal, LiteralString
from uuid import UUID

import psycopg

from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack import authority_lane
from tests.integration.live_stack.authority_lane import AuthoritySystem
from tests.integration.live_stack.cleanup import capacity_in_use
from tests.integration.live_stack.scenario import ScenarioStop
from tests.integration.live_stack.spine import (
    SpinePhaseError,
    await_system_state,
    drain_job,
    ok,
    scalar,
)
from tests.live_vm import installed_local_authority_support as local_support
from tests.live_vm import installed_remote_authority_support as remote_support
from tests.live_vm.installed_local_authority_support import (
    ActivationJob,
    NormalOperationJobs,
    assert_root_release_completion,
    release_external_boot,
    require_evidence_read_access,
    start_external_boot_activation,
)
from tests.live_vm.installed_remote_authority_support import RemoteAuthorityProofConfig

LOCAL_PRESENT_ORPHAN_BLOCKED = (
    "a quarantined receipt survives to resolution only if the release job ends terminally "
    "(ADR-0714), whose recovery is #3143"
)
STALE_ARM = (
    "the authority proof barrier is already armed; an interrupted run left it, so restart the "
    "authority once (docs/operating/runbooks/live-testing.md, stale arm)"
)
TEARDOWN_REQUIRED = "external_boot_system_teardown_required"
FAULT_READ_TABLES = (
    "jobs",
    "external_boot_activations",
    "external_boot_reservation_releases",
    "external_boot_authority_journal_heads",
    "external_boot_authority_audit",
    "external_boot_recovery_quarantine",
    "external_boot_recovery_orphan_requests",
)
HOLD_TIMEOUT_S = 120.0
ROW_DEADLINE_S = 60.0
# Two reconciler passes (30 s each) plus the repair job's own runtime.
RELEASE_REPAIR_DEADLINE_S = 180.0
_POLL_S = 0.5
_REASON = "authority recovery fault proof (#3185)"
_AUTHORITY_PYTHON = "/opt/kdive-provider-authority/.venv/bin/python"
_MAX_RECEIPT_REPORT_BYTES = 4096
# The out-of-band change an administrator makes (ADR-0584): one token appended to the System's
# persistent kernel command line on the authority daemon, so its boot identity is a third one.
# Root through the receipt seam locally, the authority account remotely; it takes a UUID only.
_PERTURB = """
import sys
from uuid import UUID
from xml.etree.ElementTree import tostring

import libvirt
from defusedxml.ElementTree import fromstring

if len(sys.argv) != 2:
    raise SystemExit("the perturbation takes one System UUID")
name = "kdive-" + str(UUID(sys.argv[1]))
conn = libvirt.open("qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock")
if conn is None:
    raise SystemExit("authority daemon connection returned no handle")
try:
    domain = conn.lookupByName(name)
    root = fromstring(domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
    cmdline = root.find("./os/cmdline")
    if cmdline is None or not cmdline.text:
        raise SystemExit("the System's persistent definition has no external-boot cmdline")
    cmdline.text += " kdive.authority-conflict-proof=1"
    conn.defineXML(tostring(root, encoding="unicode"))
finally:
    conn.close()
"""
# Read-only, root through the sudo seam the proof barrier already requires (the ADR-0800
# inventory collector returns fixed facts only); it takes UUIDs only.
_RECEIPT = """
import hashlib
import json
import os
import sys
from uuid import UUID

if len(sys.argv) != 3:
    raise SystemExit("receipt takes a System UUID and an activation UUID")
directory = f"{UUID(sys.argv[1])}.{UUID(sys.argv[2])}"
root = "/var/lib/kdive/provider-authority/recovery"
path = os.path.join(root, directory, "cleanup-quarantine.json")
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
except FileNotFoundError:
    print(json.dumps({"present": False}))
    raise SystemExit(0)
with os.fdopen(fd, "rb") as handle:
    data = handle.read(262145)
if len(data) > 262144:
    raise SystemExit("receipt exceeds its bound")
proof = json.loads(data)["proof"]
print(json.dumps({
    "present": True,
    "sha256": hashlib.sha256(data).hexdigest(),
    "operation_id": proof["operation_id"],
    "journal_digest": proof["journal_digest"],
}))
"""

# Read-only, as the remote authority account; it scans the module store's fixed evidence
# directory for the one cleanup receipt that names this System and activation (#3326).
_REMOTE_RECEIPT = """
import hashlib
import json
import os
import sys
from uuid import UUID

if len(sys.argv) != 3:
    raise SystemExit("receipt takes a System UUID and an activation UUID")
system_id, activation_id = str(UUID(sys.argv[1])), str(UUID(sys.argv[2]))
root = "/var/lib/kdive/provider-authority/remote-module-preparations/evidence"
found = []
for name in sorted(os.listdir(root)):
    if not name.endswith(".cleanup-quarantine"):
        continue
    fd = os.open(os.path.join(root, name), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(1048577)
    if len(data) > 1048576:
        raise SystemExit("receipt exceeds its bound")
    receipt = json.loads(data)
    binding = receipt["binding"]
    if binding["system_id"] == system_id and binding["activation_id"] == activation_id:
        found.append((data, receipt))
if len(found) > 1:
    raise SystemExit("more than one cleanup receipt names the activation")
if not found:
    print(json.dumps({"present": False}))
else:
    data, receipt = found[0]
    print(json.dumps({
        "present": True,
        "sha256": hashlib.sha256(data).hexdigest(),
        "operation_id": receipt["operation_identity"],
        "journal_digest": receipt["journal_digest"],
    }))
"""

type ToolCall = Callable[[str, dict[str, object]], Awaitable[ToolResponse]]
type BarrierRequest = Callable[[dict[str, str]], dict[str, str]]
type Checkpoint = Literal["before-provider", "after-provider", "before-finalize"]


@dataclass(frozen=True)
class Barrier:
    """The root-only proof barrier of one installed authority, bound to one System."""

    request: BarrierRequest
    system_id: str

    async def _call(self, document: dict[str, str]) -> dict[str, str]:
        return await asyncio.to_thread(self.request, document)

    async def arm(self, run_id: str, operation: str, checkpoint: Checkpoint) -> None:
        response = await self._call(
            {
                "action": "arm",
                "system_id": self.system_id,
                "run_id": run_id,
                "operation": operation,
                "checkpoint": checkpoint,
            }
        )
        if response == {"state": "busy"}:
            raise AssertionError(STALE_ARM)
        if response != {"state": "armed"}:
            raise AssertionError(f"the proof barrier refused the arm: {response!r}")

    async def status(self) -> str:
        response = await self._call({"action": "status"})
        state = response.get("state")
        if set(response) != {"state"} or state not in {"idle", "armed", "reached", "released"}:
            raise AssertionError(f"the proof barrier status is malformed: {response!r}")
        return state

    async def wait_reached(self, timeout_s: float = HOLD_TIMEOUT_S) -> None:
        """Wait for the armed operation by its observable state, never elapsed-time luck."""
        deadline = time.monotonic() + timeout_s
        while (state := await self.status()) != "reached":
            if state != "armed":
                raise AssertionError(f"the proof barrier lost its arm: {state}")
            if time.monotonic() >= deadline:
                raise AssertionError("the armed authority operation never reached its barrier")
            await asyncio.sleep(_POLL_S)

    async def release(self) -> None:
        response = await self._call({"action": "release"})
        if response not in ({"state": "released"}, {"state": "idle"}):
            raise AssertionError(f"the proof barrier refused the release: {response!r}")

    async def abort(self) -> None:
        response = await self._call({"action": "abort"})
        if response != {"state": "aborted"}:
            raise AssertionError(f"the proof barrier refused the abort: {response!r}")


def barrier_for(system: AuthoritySystem) -> Barrier:
    """The barrier of the authority that owns ``system``; refuses one that exposes none."""
    config = system.config
    if isinstance(config, RemoteAuthorityProofConfig):
        remote_support.require_fault_barrier(config)
        return Barrier(partial(remote_support.fault_barrier_request, config), system.system_id)
    local_support.require_fault_barrier(config)
    return Barrier(partial(local_support.fault_barrier_request, config), system.system_id)


async def _settle(barrier: Barrier) -> None:
    """Release whatever the arm holds and require the barrier back to idle."""
    if await barrier.status() in {"armed", "reached"}:
        await barrier.release()
    deadline = time.monotonic() + HOLD_TIMEOUT_S
    while (state := await barrier.status()) != "idle":
        if time.monotonic() >= deadline:
            raise AssertionError(f"the proof barrier stayed {state} after its release")
        await asyncio.sleep(_POLL_S)


@asynccontextmanager
async def held(
    barrier: Barrier, run_id: str, operation: str, checkpoint: Checkpoint
) -> AsyncIterator[Barrier]:
    """Arm one exact operation; on every exit release it and require the barrier idle."""
    await barrier.arm(run_id, operation, checkpoint)
    try:
        yield barrier
    finally:
        await _settle(barrier)


async def _rows(
    db_url: str, sql: LiteralString, params: tuple[object, ...]
) -> list[tuple[object, ...]]:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(sql, params)
        return list(await cursor.fetchall())


async def job_claim(db_url: str, job_id: str) -> tuple[object, ...]:
    """``(state, attempt)`` of one job."""
    rows = await _rows(db_url, "SELECT state, attempt FROM jobs WHERE id = %s", (job_id,))
    assert len(rows) == 1, f"job {job_id} is not in the database"
    return rows[0]


async def require_hold_budget(db_url: str, job_id: str, attempt: object) -> None:
    """Refuse a fault once the held attempt lost its lease or its authority deadline."""
    rows = await _rows(
        db_url,
        "SELECT state, attempt, lease_expires_at > clock_timestamp() FROM jobs WHERE id = %s",
        (job_id,),
    )
    if rows != [("running", attempt, True)]:
        raise AssertionError(f"hold budget exceeded: job {job_id} is {rows!r}, not held")


async def journal_head(db_url: str, system_id: str) -> tuple[object, ...] | None:
    """``(phase, operation)`` of the System's authority journal head."""
    rows = await _rows(
        db_url,
        "SELECT phase, head_record->>'operation' FROM external_boot_authority_journal_heads "
        "WHERE system_id = %s",
        (system_id,),
    )
    assert len(rows) <= 1, f"System {system_id} has {len(rows)} authority journal heads"
    return rows[0] if rows else None


async def quarantine_rows(db_url: str) -> dict[str, dict[str, object]]:
    """Every quarantine row, keyed by id; a fault must change only its own."""
    rows = await _rows(
        db_url,
        "SELECT id::text, row_to_json(q)::text FROM external_boot_recovery_quarantine q "
        "ORDER BY id",
        (),
    )
    return {str(row_id): json.loads(str(body)) for row_id, body in rows}


async def orphan_request(db_url: str, job_id: str) -> list[tuple[object, ...]]:
    """``(object_ids, completed)`` of the orphan request one resolution job serves."""
    return await _rows(
        db_url,
        "SELECT object_ids::text[], completed_at IS NOT NULL "
        "FROM external_boot_recovery_orphan_requests WHERE job_id = %s",
        (job_id,),
    )


async def attempt_failure_contexts(db_url: str, job_id: str, attempt: object) -> list[object]:
    """The bound failure contexts the authority audit kept for one job attempt (ADR-0714)."""
    rows = await _rows(
        db_url,
        "SELECT failure_context FROM external_boot_authority_audit "
        "WHERE job_id = %s AND job_attempt = %s AND failure_context IS NOT NULL",
        (job_id, attempt),
    )
    return [row[0] for row in rows]


async def activation_of(db_url: str, run_id: str) -> str:
    rows = await _rows(
        db_url, "SELECT id::text FROM external_boot_activations WHERE run_id = %s", (run_id,)
    )
    assert len(rows) == 1, f"Run {run_id} has {len(rows)} activations"
    return str(rows[0][0])


@dataclass(frozen=True)
class Receipt:
    """The local authority's private cleanup receipt for one activation."""

    present: bool
    sha256: str | None = None
    operation_id: str | None = None
    journal_digest: str | None = None


def receipt_state(system_id: str, activation_id: str) -> Receipt:
    """Read the private cleanup receipt through the fixed root observer."""
    argv = ["sudo", "-n", _AUTHORITY_PYTHON, "-I", "-c", _RECEIPT]
    argv += [str(UUID(system_id)), str(UUID(activation_id))]
    result = subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
        argv, capture_output=True, text=True, timeout=60.0, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"the receipt observer exited {result.returncode}")
    if len(result.stdout) > _MAX_RECEIPT_REPORT_BYTES:
        raise AssertionError("the receipt observer output exceeds its bound")
    return Receipt(**json.loads(result.stdout))


def remote_receipt_state(config: RemoteAuthorityProofConfig, ids: tuple[str, str]) -> Receipt:
    """Read the remote private cleanup receipt as the authority account over the operator's SSH."""
    output = remote_support.run_as_authority(
        config, _REMOTE_RECEIPT, str(UUID(ids[0])), str(UUID(ids[1]))
    )
    if len(output) > _MAX_RECEIPT_REPORT_BYTES:
        raise AssertionError("the receipt observer output exceeds its bound")
    return Receipt(**json.loads(output))


def perturb_definition(system: AuthoritySystem) -> None:
    """Move the System's persistent definition to a third identity behind the authority."""
    system_id = str(UUID(system.system_id))
    config = system.config
    if isinstance(config, RemoteAuthorityProofConfig):
        remote_support.run_as_authority(config, _PERTURB, system_id)
        return
    argv = ["sudo", "-n", _AUTHORITY_PYTHON, "-I", "-c", _PERTURB, system_id]
    result = subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
        argv, capture_output=True, text=True, timeout=60.0, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"the definition perturbation exited {result.returncode}")


async def conflict_head(db_url: str, system_id: str) -> tuple[object, ...] | None:
    """``(phase, operation, outcome, category, composite)`` of the System's journal head."""
    rows = await _rows(
        db_url,
        "SELECT phase, head_record->>'operation', head_record->>'outcome', "
        "head_record#>>'{observation,category}', head_record#>>'{observation,composite_state}' "
        "FROM external_boot_authority_journal_heads WHERE system_id = %s",
        (system_id,),
    )
    assert len(rows) <= 1, f"System {system_id} has {len(rows)} authority journal heads"
    return rows[0] if rows else None


async def head_sequence(db_url: str, system_id: str) -> int | None:
    """The sequence of the System's authority journal head."""
    rows = await _rows(
        db_url,
        "SELECT sequence FROM external_boot_authority_journal_heads WHERE system_id = %s",
        (system_id,),
    )
    return int(str(rows[0][0])) if rows else None


async def audited_sequence(db_url: str, job_id: str) -> int | None:
    """The journal sequence the authority audit anchored for ``job_id``'s terminal receipt."""
    rows = await _rows(
        db_url,
        "SELECT journal_sequence FROM external_boot_authority_audit "
        "WHERE job_id = %s AND journal_sequence IS NOT NULL ORDER BY journal_sequence DESC",
        (job_id,),
    )
    return int(str(rows[0][0])) if rows else None


async def _activation_state(db_url: str, activation_id: str) -> object:
    rows = await _rows(
        db_url, "SELECT state FROM external_boot_activations WHERE id = %s", (activation_id,)
    )
    assert len(rows) == 1, f"activation {activation_id} is not in the database"
    return rows[0][0]


async def _release_cleanup(db_url: str, activation_id: str) -> tuple[bool, int]:
    """``(cleanup_complete, release credits)`` of one activation."""
    rows = await _rows(
        db_url,
        "SELECT cleanup_complete, (SELECT count(*) FROM external_boot_reservation_releases "
        "WHERE activation_id = %s) FROM external_boot_activations WHERE id = %s",
        (activation_id, activation_id),
    )
    assert len(rows) == 1, f"activation {activation_id} is not in the database"
    return bool(rows[0][0]), int(str(rows[0][1]))


async def _cleaned_within(db_url: str, activation_id: str, window_s: float) -> bool:
    """Poll until the activation's release cleanup is complete, or the window closes."""
    deadline = time.monotonic() + window_s
    while True:
        if (await _release_cleanup(db_url, activation_id))[0]:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_POLL_S)


async def _drain_conflict(system: AuthoritySystem, phase: str, job_id: str) -> None:
    """Require the job to end ``failed`` with category ``conflict``, not to succeed."""
    try:
        await drain_job(system.op, phase, job_id)
    except SpinePhaseError as failed:
        assert failed.error_category == "conflict", f"{phase} failed with {_cause(failed)}"
    else:
        raise AssertionError(f"the {phase} job succeeded over a third identity")


def _domain_present(system: AuthoritySystem) -> bool:
    config = system.config
    if isinstance(config, RemoteAuthorityProofConfig):
        inventory = remote_support.remote_authority_inventory(config, system.system_id)
        return inventory.system is not None
    return authority_lane.authority_inventory(system.system_id).system is not None


async def _allocation_state(system: AuthoritySystem) -> str:
    env = await scalar(
        system.op, "allocations.wait", allocation_id=system.allocation_id, timeout_s=0
    )
    return env.status


async def activate(
    system: AuthoritySystem,
    *,
    kernel_tree: Path | None = None,
    evidence_dir: Path | None = None,
) -> ActivationJob:
    """Start one external boot; a remote one that fails is a recorded blocker, not a pass.

    ``kernel_tree`` defaults to ``KDIVE_KERNEL_SRC``; ``evidence_dir`` retains the upload record.
    """
    remote = isinstance(system.config, RemoteAuthorityProofConfig)
    try:
        activation = await start_external_boot_activation(
            system.op,
            system.ledger,
            project=system.project,
            system_id=system.system_id,
            label=system.config.ownership_prefix,
            # Only the local fixture profile carries a crashkernel; only remote boot needs an
            # initrd (#3323).
            require_kdump=not remote,
            kernel_tree=kernel_tree,
            evidence_dir=evidence_dir,
            with_initrd=remote,
        )
        await drain_job(system.op, "activate", activation.activate_job_id)
    except SpinePhaseError as failed:
        if not remote:
            raise
        reason = f"remote external-boot activation failed at {failed.phase} ({_cause(failed)})"
        raise ScenarioStop(Outcome.BLOCKED, reason) from failed
    return activation


def _cause(failed: SpinePhaseError) -> str:
    """The phase reason plus the envelope's public category and ``data.reason``, if any."""
    data_reason = failed.data.get("reason")
    envelope = [part for part in (failed.error_category, data_reason) if isinstance(part, str)]
    return f"{failed.reason}; {': '.join(envelope)}" if envelope else failed.reason


async def held_teardown_release(
    system: AuthoritySystem,
    db_url: str,
    force_release: ToolCall,
    *,
    kernel_tree: Path | None = None,
) -> dict[str, object]:
    """``ops.force_release`` refuses while a teardown effect is held, then releases.

    The release is observed outside the tool's envelope: the allocation reads ``released`` and
    the capacity in use drops (#3186).
    """
    op, system_id = system.op, system.system_id
    await require_evidence_read_access(db_url, FAULT_READ_TABLES)
    activation = await activate(system, kernel_tree=kernel_tree)
    release = await release_external_boot(op, activation.run_id)
    await drain_job(op, "release", release.object_id)
    await assert_root_release_completion(
        db_url,
        NormalOperationJobs(
            activation.investigation_id,
            activation.run_id,
            activation.activate_job_id,
            release.object_id,
        ),
    )
    in_use = await capacity_in_use(op)
    args: dict[str, object] = {"allocation_id": system.allocation_id, "reason": _REASON}
    started = time.monotonic()
    async with held(barrier_for(system), activation.run_id, "teardown", "after-provider") as hold:
        teardown = ok(await scalar(op, "systems.teardown", system_id=system_id), "teardown")
        await hold.wait_reached()
        state, attempt = await job_claim(db_url, teardown.object_id)
        assert state == "running", f"the held teardown job is {state}"
        current = await scalar(op, "systems.get", system_id=system_id)
        assert current.status != "torn_down", "the System is torn down while its effect is held"
        assert await journal_head(db_url, system_id) == ("mutation-started", "teardown")
        assert not await asyncio.to_thread(_domain_present, system), (
            "the held teardown has not removed the domain from the authority daemon"
        )
        assert await _allocation_state(system) == "active"
        assert await capacity_in_use(op) == in_use, "capacity moved while the effect was held"
        await require_hold_budget(db_url, teardown.object_id, attempt)
        refused = await force_release("ops.force_release", args)
        refusal = (refused.error_category, refused.data.get("reason"))
        assert refusal == ("conflict", TEARDOWN_REQUIRED), f"force_release answered {refusal}"
        assert refused.data.get("system_id") == system_id, "the refusal names another System"
        assert await _allocation_state(system) == "active", "force_release released a held System"
        held_s = round(time.monotonic() - started, 1)
    await drain_job(op, "teardown", teardown.object_id)
    await await_system_state(op, "teardown", system_id, "torn_down")
    assert await journal_head(db_url, system_id) == ("terminal", "teardown")
    assert await _allocation_state(system) == "active", "the teardown released the allocation"
    in_use = await capacity_in_use(op)
    released = await force_release("ops.force_release", args)
    assert released.status == "released", f"force_release answered {released.status}"
    allocation = await _allocation_state(system)
    assert allocation == "released", f"after force_release the allocation is {allocation}"
    freed = in_use - await capacity_in_use(op)
    assert freed > 0, "capacity in use did not drop after force_release"
    return {
        "held_effect": "teardown after-provider",
        "refusal": TEARDOWN_REQUIRED,
        "release_after_quiescence": released.status,
        "allocation_after_release": allocation,
        "capacity_freed": freed,
        "held_seconds": held_s,
    }


async def _published_row(
    db_url: str, before: dict[str, dict[str, object]]
) -> tuple[str, dict[str, object]]:
    """Wait for exactly one new quarantine row; every earlier row must be unchanged."""
    deadline = time.monotonic() + ROW_DEADLINE_S
    while True:
        rows = await quarantine_rows(db_url)
        assert {key: rows.get(key) for key in before} == before, "an unrelated quarantine row moved"
        new = sorted(set(rows) - set(before))
        if new:
            assert len(new) == 1, f"the abort published {len(new)} quarantine rows, not one"
            return new[0], rows[new[0]]
        if time.monotonic() >= deadline:
            raise AssertionError("the aborted finalize published no quarantine row")
        await asyncio.sleep(_POLL_S)


async def _await_internal_job(db_url: str, job_id: str) -> None:
    """Wait for a platform-internal job to succeed, read from the database.

    ``resolve_recovery_orphan`` is in ``PLATFORM_INTERNAL_JOB_KINDS``: no tenant-facing job tool
    exposes it, so ``jobs.wait`` never reports it terminal.
    """
    deadline = time.monotonic() + ROW_DEADLINE_S
    while True:
        state, _attempt = await job_claim(db_url, job_id)
        if state == "succeeded":
            return
        assert state not in {"failed", "canceled"}, f"resolve-orphan job {job_id} is {state}"
        assert time.monotonic() < deadline, f"resolve-orphan job {job_id} is still {state}"
        await asyncio.sleep(_POLL_S)


async def _resolve(
    system: AuthoritySystem, resolve: ToolCall, db_url: str, identities: list[str]
) -> str:
    """Admit and drain one exact-set ``delete`` disposition; return its job id."""
    admitted = await resolve(
        "ops.resolve_recovery_orphan",
        {"system_id": system.system_id, "object_identities": identities, "disposition": "delete"},
    )
    job = ok(admitted, "resolve-orphan").object_id
    await _await_internal_job(db_url, job)
    return job


async def _resolve_published(
    system: AuthoritySystem,
    resolve: ToolCall,
    db_url: str,
    owner: tuple[dict[str, dict[str, object]], str],
    failure: BaseException,
) -> None:
    """Failure cleanup: resolve the rows this arm's abort published, or name them as residue.

    The set comes from the database, not from what the arm observed: the authority publishes
    after the abort returns, so a row can exist before the arm's poll sees it.
    """
    before, activation_id = owner
    identities: list[str] = []
    deadline = time.monotonic() + ROW_DEADLINE_S
    try:
        while not identities and time.monotonic() < deadline:
            identities = [
                str(row["object_identity"])
                for key, row in (await quarantine_rows(db_url)).items()
                if key not in before
                and (row["system_id"], row["activation_id"], row["status"])
                == (system.system_id, activation_id, "quarantined")
            ]
            if not identities:
                await asyncio.sleep(_POLL_S)
        if not identities:
            failure.add_note("no owned quarantine row observed after the abort")
            return
        await _resolve(system, resolve, db_url, identities)
    except Exception as cleanup:  # noqa: BLE001 - recorded on the primary failure below
        failure.add_note(f"owned quarantine residue {identities}: {type(cleanup).__name__}")


async def quarantined_orphan(
    system: AuthoritySystem,
    db_url: str,
    resolve: ToolCall,
    *,
    kernel_tree: Path | None = None,
) -> dict[str, object]:
    """Publish one real quarantine by an aborted finalize, then resolve exactly that row."""
    op, system_id = system.op, system.system_id
    config = system.config

    def observe_receipt(activation_id: str) -> Receipt:
        if isinstance(config, RemoteAuthorityProofConfig):
            return remote_receipt_state(config, (system_id, activation_id))
        return receipt_state(system_id, activation_id)

    provider_kind = (
        "remote-libvirt" if isinstance(config, RemoteAuthorityProofConfig) else ("local-libvirt")
    )
    await require_evidence_read_access(db_url, FAULT_READ_TABLES)
    activation = await activate(system, kernel_tree=kernel_tree)
    activation_id = await activation_of(db_url, activation.run_id)
    before = await quarantine_rows(db_url)
    aborted = False
    try:
        async with held(
            barrier_for(system), activation.run_id, "cleanup", "before-finalize"
        ) as hold:
            release = await release_external_boot(op, activation.run_id)
            await hold.wait_reached()
            job = release.object_id
            state, attempt = await job_claim(db_url, job)
            assert state == "running", f"the held release job is {state}"
            assert await journal_head(db_url, system_id) == ("terminal", "cleanup")
            receipt = await asyncio.to_thread(observe_receipt, activation_id)
            assert receipt.present, "the private cleanup receipt is absent before the abort"
            assert await quarantine_rows(db_url) == before, "a quarantine row preceded the abort"
            await require_hold_budget(db_url, job, attempt)
            # Set first: a lost abort reply must still clean up a row the abort published.
            aborted = True
            await hold.abort()
            row_id, row = await _published_row(db_url, before)
        assert (
            row["system_id"],
            row["activation_id"],
            row["object_kind"],
            row["provider_kind"],
            row["status"],
        ) == (system_id, activation_id, "recovery-record", provider_kind, "quarantined")
        assert (row["operation_identity"], row["mutation_journal_digest"]) == (
            receipt.operation_id,
            receipt.journal_digest,
        ), "the published row does not name the observed receipt"
        await drain_job(op, "release", job)
        assert await job_claim(db_url, job) == ("succeeded", int(str(attempt)) + 1)
        contexts = await attempt_failure_contexts(db_url, job, attempt)
        assert any(
            isinstance(context, dict) and context.get("authority_reason") == "provider-conflict"
            for context in contexts
        ), f"the aborted attempt carries no provider-conflict context: {contexts!r}"
        assert not (await asyncio.to_thread(observe_receipt, activation_id)).present
        quarantined = await quarantine_rows(db_url)
        assert quarantined[row_id]["status"] == "quarantined"
        resolved_by = await _resolve(system, resolve, db_url, [str(row["object_identity"])])
    except BaseException as failure:
        if aborted:
            await _resolve_published(system, resolve, db_url, (before, activation_id), failure)
        raise
    after = await quarantine_rows(db_url)
    assert {k: v for k, v in after.items() if k != row_id} == {
        k: v for k, v in quarantined.items() if k != row_id
    }, "the resolution changed another quarantine row"
    resolved = after[row_id]
    assert (resolved["status"], resolved["disposition_job_id"]) == ("deleted", resolved_by)
    assert resolved["resolved_at"] is not None
    assert await orphan_request(db_url, resolved_by) == [([row_id], True)]
    return {
        "published": "recovery-record",
        "release_attempts": int(str(attempt)) + 1,
        "receipt_at_resolution": "absent",
        "resolved": "deleted",
        "present_receipt_delete": LOCAL_PRESENT_ORPHAN_BLOCKED,
    }


async def recorded_conflict(
    system: AuthoritySystem, db_url: str, *, kernel_tree: Path | None = None
) -> dict[str, object]:
    """Record a real ``recovery_conflict`` from a host change, then resolve it by its composite."""
    op, system_id = system.op, system.system_id
    await require_evidence_read_access(db_url, FAULT_READ_TABLES)
    activation = await activate(system, kernel_tree=kernel_tree)
    activation_id = await activation_of(db_url, activation.run_id)
    await asyncio.to_thread(perturb_definition, system)
    release = await release_external_boot(op, activation.run_id)
    await _drain_conflict(system, "release", release.object_id)
    head = await conflict_head(db_url, system_id)
    assert head is not None and head[:4] == ("terminal", "recover", "conflict", "conflict"), head
    composite = str(head[4])
    assert await _activation_state(db_url, activation_id) == "recovery_conflict"
    resolution = ok(
        await scalar(
            op,
            "systems.resolve_external_boot_conflict",
            system_id=system_id,
            operation="restore-recorded-source",
            observed_identity=composite,
        ),
        "resolve-conflict",
    )
    await drain_job(op, "resolve-conflict", resolution.object_id)
    # The reconciler's release repair lane completes the user's release once the activation is
    # recovered, so a later record can follow the resolution: judge the resolution's own record.
    anchored = await audited_sequence(db_url, resolution.object_id)
    assert anchored is not None, "the resolve-conflict job anchored no journal record"
    head = await conflict_head(db_url, system_id)
    current = await head_sequence(db_url, system_id)
    assert head is not None and current is not None and current >= anchored, (head, anchored)
    if current == anchored:
        assert head[:4] == ("terminal", "resolve-conflict", "source", "source"), head
    assert await _activation_state(db_url, activation_id) == "recovered"
    # A fresh release would replay the failed conflict job (same dedup key), so await the
    # reconciler's repair of the user's release instead of admitting another.
    assert await _cleaned_within(db_url, activation_id, RELEASE_REPAIR_DEADLINE_S), (
        "the reconciler did not complete the release after the resolution"
    )
    credits = (await _release_cleanup(db_url, activation_id))[1]
    assert credits == 1, f"the release credit after the resolution is {credits}"
    return {
        "recorded": "recovery_conflict",
        "release": "failed: conflict",
        "resolution_admitted": resolution.object_id,
        "resolution": "succeeded: restored the recorded source",
        "activation": "recovered",
        "release_after_resolution": "reconciler",
        "release_credits": credits,
    }
