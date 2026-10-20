"""Orchestration-fault carriers: worker interruption, lost response, backing service (#3117).

Each ``failure-resource/<provider>/x86_64/{worker-interruption,lost-response,backing-service}``
cell proves its recovery outcome plus ``no-duplicates``, ``protected-state`` and ``cleanup`` on
the managed host stack's direct provision path
(``docs/workflow/specs/2026-10-20-orchestration-fault-proofs-design.md``). Every fault lands in
the provision's first-boot window: the owned domain active and the job running at attempt 1.

- ``worker-interruption`` pauses the exact job holder (SIGSTOP through a pidfd, never a systemd
  restart), waits for a real lease lapse and a reclaim by another worker, then resumes the stale
  worker and proves its late effects left the System, domain and overlay alone.
- ``lost-response`` provisions with an idempotency key, discards the envelope once its row is
  committed and replays the key from a new session while the job runs and after it finishes.
- ``backing-service`` stops the stack's Postgres for a fixed outage and restarts the same
  container; the job resumes through a requeue or a lease reclaim.

The verdict functions here are pure; the live node is
``tests/integration/test_orchestration_faults_live.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg

from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.resource_limits import attempt_categories
from tests.integration.live_stack.scenario import ScenarioStop
from tests.live_vm.installed_local_authority_support import RunningJobClaim

OWNER = 3117
PROJECT = "orchestration-faults"
STALE_DROPPED = "completed but was reclaimed; result dropped"
HEARTBEAT_LOST = "heartbeat for job {job_id} failed; stopping (lease will lapse)"
TERMINAL = frozenset({"succeeded", "failed", "canceled"})
ComposeAction = Literal["stop", "start", "ps"]


@dataclass(frozen=True)
class JobRow:
    """The job's state, attempt counter and current holder."""

    state: str
    attempt: int
    worker_id: str | None


async def job_holder(db_url: str, job_id: str) -> JobRow:
    """Read the job's state, attempt and holder (evidence only)."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute("SELECT state, attempt, worker_id FROM jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None, f"job {job_id} has no row"
    return JobRow(str(row[0]), int(row[1]), None if row[2] is None else str(row[2]))


def _fail(reason: str) -> ScenarioStop:
    return ScenarioStop(Outcome.FAILURE, reason)


def window_reached(domain_active: bool, row: JobRow) -> bool:
    """The fault point: the owned domain is up while attempt 1 still holds the running job."""
    return domain_active and row.state == "running" and row.attempt == 1


def window_missed(row: JobRow) -> bool:
    """The job finished before the fault point was seen; the cell is incomplete, never passed."""
    return row.state in TERMINAL


def _stale_outcome(journal: str, job_id: str) -> str | None:
    if f"job {job_id} {STALE_DROPPED}" in journal:
        return "dropped"
    failed = attempt_categories(journal, job_id)
    return f"failed:{failed[-1]}" if failed else None


@dataclass(frozen=True)
class Interruption:
    """What the worker-interruption cell observed around the paused holder."""

    original: RunningJobClaim
    replacement: RunningJobClaim
    final: JobRow
    after_resume: JobRow
    system_state: str
    domain_uuid_before: str
    domain_uuid_after: str | None
    overlay_present: bool
    stale_journal: str
    job_id: str


def interruption_verdict(seen: Interruption) -> dict[str, object]:
    """Prove a reclaimed provision finished once and the resumed stale attempt changed nothing."""
    if seen.replacement.worker_id == seen.original.worker_id:
        raise _fail(f"job {seen.job_id} was reclaimed by its own paused worker")
    if seen.replacement.attempt <= seen.original.attempt:
        raise _fail(
            f"reclaim attempt {seen.replacement.attempt} is not above {seen.original.attempt}"
        )
    expected = JobRow("succeeded", seen.replacement.attempt, seen.replacement.worker_id)
    if seen.final != expected:
        raise _fail(f"reclaimed job ended {seen.final}, expected {expected}")
    if seen.after_resume != seen.final:
        raise _fail(f"the resumed stale worker changed the job row to {seen.after_resume}")
    if seen.system_state == "failed":
        raise _fail("the stale attempt failed the System after the reclaim committed it")
    if seen.system_state != "ready":
        raise _fail(f"System is {seen.system_state} after recovery, not ready")
    if seen.domain_uuid_after is None:
        raise _fail("the stale attempt tore down the reclaimed System's domain")
    if seen.domain_uuid_after != seen.domain_uuid_before:
        raise _fail(
            f"domain UUID changed from {seen.domain_uuid_before} to {seen.domain_uuid_after}"
        )
    if not seen.overlay_present:
        raise _fail("the stale attempt reclaimed the overlay the reclaimed System runs on")
    stale = _stale_outcome(seen.stale_journal, seen.job_id)
    if stale is None:
        raise _fail(f"the resumed stale worker logged no outcome for job {seen.job_id}")
    return {
        "original": {"worker": seen.original.worker_id, "attempt": seen.original.attempt},
        "replacement": {"worker": seen.replacement.worker_id, "attempt": seen.replacement.attempt},
        "final": {
            "state": seen.final.state,
            "attempt": seen.final.attempt,
            "worker": seen.final.worker_id,
        },
        "stale_outcome": stale,
        "domain_uuid": seen.domain_uuid_after,
    }


@dataclass(frozen=True)
class LostResponse:
    """What the lost-response cell observed: the committed row and every replay of its key."""

    stored: Mapping[str, object]
    provisioned_system: str
    replays: Sequence[ToolResponse]
    provision_jobs: int
    systems: int
    final: JobRow
    system_state: str


def lost_response_verdict(seen: LostResponse) -> dict[str, object]:
    """Prove every replay of the key returned the committed admission and nothing doubled."""
    envelope = seen.stored.get("envelope")
    if not isinstance(envelope, Mapping) or envelope.get("type") != "provision_job":
        raise _fail(f"stored idempotency result is not a provision admission: {seen.stored}")
    job = envelope.get("job")
    if not isinstance(job, Mapping):
        raise _fail("stored admission carries no job")
    job_id, system_id = str(job.get("id")), str(envelope.get("system_id"))
    if system_id != seen.provisioned_system:
        raise _fail(f"stored system {system_id} is not the provisioned {seen.provisioned_system}")
    if not seen.replays:
        raise _fail("no replay was made")
    for replay in seen.replays:
        if replay.object_id != job_id:
            raise _fail(f"replay returned job {replay.object_id}, stored {job_id}")
        if replay.data.get("system_id") != system_id:
            raise _fail(
                f"replay returned system {replay.data.get('system_id')}, stored {system_id}"
            )
    if seen.provision_jobs != 1:
        raise _fail(f"{seen.provision_jobs} provision jobs for the Allocation")
    if seen.systems != 1:
        raise _fail(f"{seen.systems} Systems under the Allocation")
    if seen.final.state != "succeeded" or seen.system_state != "ready":
        raise _fail(f"provision job {seen.final.state}, System {seen.system_state}")
    return {
        "job_id": job_id,
        "system_id": system_id,
        "replays": [r.status for r in seen.replays],
        "provision_jobs": seen.provision_jobs,
        "systems": seen.systems,
    }


@dataclass(frozen=True)
class Outage:
    """What the backing-service cell observed across the Postgres outage."""

    container_before: str
    container_after: str
    units_before: Mapping[str, tuple[str, str]]
    units_after: Mapping[str, tuple[str, str]]
    ready_after_s: float | None
    final: JobRow
    system_state: str
    journal: str
    job_id: str


def outage_verdict(seen: Outage) -> dict[str, object]:
    """Prove the job resumed after the outage on the same Postgres and unrestarted workers."""
    if seen.container_after != seen.container_before:
        raise _fail(
            f"postgres container changed from {seen.container_before} to {seen.container_after}"
        )
    if dict(seen.units_after) != dict(seen.units_before):
        raise _fail(
            f"worker units changed across the outage: {seen.units_before} -> {seen.units_after}"
        )
    if seen.ready_after_s is None:
        raise _fail("the stack did not return to ready after Postgres restarted")
    if seen.final.state != "succeeded" or seen.system_state != "ready":
        raise _fail(
            f"provision job {seen.final.state}, System {seen.system_state} after the outage"
        )
    if seen.final.attempt <= 1:
        raise _fail("attempt 1 succeeded: the outage never reached the in-flight attempt")
    failed = attempt_categories(seen.journal, seen.job_id)
    if "infrastructure_failure" in failed:
        path = "requeued"
    elif HEARTBEAT_LOST.format(job_id=seen.job_id) in seen.journal:
        path = "lease-reclaimed"
    else:
        path = "unknown"
    return {
        "container": seen.container_after,
        "ready_after_s": seen.ready_after_s,
        "attempt": seen.final.attempt,
        "resume_path": path,
        "failed_attempts": failed,
    }


def compose_argv(project_dir: Path, action: ComposeAction) -> list[str]:
    """The fixed ``docker compose`` argv for the stack's Postgres; nothing else is allowed."""
    assert action in ("stop", "start", "ps"), f"refused compose action {action!r}"
    tail = ["ps", "-q", "postgres"] if action == "ps" else [action, "postgres"]
    return ["docker", "compose", "--project-directory", str(project_dir), *tail]
