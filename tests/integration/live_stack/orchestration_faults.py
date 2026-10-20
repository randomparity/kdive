"""Orchestration-fault carriers: worker interruption, lost response, backing service (#3117).

Each ``failure-resource/<provider>/x86_64/{worker-interruption,lost-response,backing-service}``
cell proves its recovery outcome plus ``no-duplicates``, ``protected-state`` and ``cleanup`` on
the managed host stack's direct provision path
(``docs/workflow/specs/2026-10-20-orchestration-fault-proofs-design.md``). The worker and
Postgres faults land in the provision's first-boot window (the owned domain active and the job
running at attempt 1); the lost-response replay runs while the provision job is running.

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

import asyncio
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import time
import xml.etree.ElementTree as ET  # noqa: S405 - the provider's own domain XML  # nosec B405
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, LiteralString
from uuid import UUID, uuid4

import httpx
import libvirt
import psycopg

from kdive.jobs.queue import DEFAULT_LEASE, DEFAULT_MAX_ATTEMPTS
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.providers.remote_libvirt.lifecycle.provisioning import (
    _AGENT_TIMEOUT_S as REMOTE_FIRST_BOOT_S,
)
from kdive.providers.shared.runtime_paths import domain_name_for
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.cleanup import domain_disks, system_failed
from tests.integration.live_stack.evidence import run_identity
from tests.integration.live_stack.remote_lifecycle import observe_host, remote_host
from tests.integration.live_stack.resource_limits import (
    Held,
    Provider,
    attempt_categories,
    fixture,
    local_guest,
    local_window_s,
    on_allocation,
    remote_guest,
    remote_provider,
    wait_terminal,
    worker_journal,
)
from tests.integration.live_stack.scenario import (
    ACCELERATORS,
    DEFAULT_SIZING,
    CellRun,
    ScenarioStop,
)
from tests.integration.live_stack.skew import readyz_urls
from tests.integration.live_stack.spine import mint_role_token, ok, scalar
from tests.live_vm.installed_local_authority_support import (
    RunningJobClaim,
    _active_worker_readyz_urls,
    running_job_claim,
    set_exact_worker_hold,
    wait_for_reclaimed_job,
)

OWNER = 3117
PROJECT = "orchestration-faults"
STALE_DROPPED = "completed but was reclaimed; result dropped"
HEARTBEAT_LOST = "heartbeat for job {job_id} failed; stopping (lease will lapse)"
TERMINAL = frozenset({"succeeded", "failed", "canceled"})
ROOT = Path(__file__).resolve().parents[3]
OUTAGE_S = 75.0  # more than two 30 s heartbeat intervals
READY_S = 120.0
_POLL_S = 0.5
_SAMPLE_S = 15.0
_HOLD_PYTHON = "/usr/bin/python3"  # the interpreter set_exact_worker_hold runs under sudo
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


def stale_outcome(journal: str, job_id: str) -> str | None:
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
    domain_active_after: bool
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
    if not seen.domain_active_after:
        raise _fail("the stale attempt stopped the reclaimed System's domain")
    if not seen.overlay_present:
        raise _fail("the stale attempt reclaimed the overlay the reclaimed System runs on")
    stale = stale_outcome(seen.stale_journal, seen.job_id)
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


def admitted(stored: Mapping[str, object]) -> tuple[str, str]:
    """The job and System ids of a stored ``systems.provision`` admission row."""
    envelope = stored.get("envelope")
    if not isinstance(envelope, Mapping) or envelope.get("type") != "provision_job":
        raise _fail(f"stored idempotency result is not a provision admission: {stored}")
    job = envelope.get("job")
    if not isinstance(job, Mapping):
        raise _fail("stored admission carries no job")
    return str(job.get("id")), str(envelope.get("system_id"))


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
    job_id, system_id = admitted(seen.stored)
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
    domain_active: bool
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
    if not seen.domain_active:
        raise _fail("the System's domain is not running after the outage")
    if seen.final.attempt <= 1:
        raise _fail("attempt 1 succeeded: the outage never reached the in-flight attempt")
    failed = attempt_categories(seen.journal, seen.job_id)
    if any(category != "infrastructure_failure" for category in failed):
        raise _fail(f"an attempt failed {failed}, not with the outage's infrastructure_failure")
    if failed:
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


# --- host observations -------------------------------------------------------------------


def _run(argv: list[str], timeout: float = 120.0) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    assert result.returncode == 0, f"{argv[0]} {argv[1]} exited {result.returncode}"
    return result.stdout


def running_units() -> list[str]:
    """The fixed live-worker units systemd reports running."""
    listing = _run(
        ["systemctl", "list-units", "kdive-live-worker@*.service", "--state=running"]
        + ["--no-legend", "--plain"]
    )
    return sorted(line.split()[0] for line in listing.splitlines() if line.strip())


def unit_identity() -> dict[str, tuple[str, str]]:
    """Each running worker unit's ``(InvocationID, MainPID)``; a restart changes both."""
    identity = {}
    for unit in running_units():
        fields = dict(
            line.partition("=")[::2]
            for line in _run(
                ["systemctl", "show", unit, "--property=InvocationID", "--property=MainPID"]
            ).splitlines()
        )
        identity[unit] = (fields.get("InvocationID", ""), fields.get("MainPID", ""))
    return identity


def postgres_container() -> str:
    """The stack's one compose ``postgres`` container; anything else blocks the cell."""
    try:
        ids = _run(compose_argv(ROOT, "ps")).split()
    except AssertionError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"docker compose is not usable: {exc}") from None
    if len(ids) != 1:
        raise ScenarioStop(Outcome.BLOCKED, f"expected one compose postgres container, {ids}")
    return ids[0]


def preconditions(base_url: str, *, postgres: bool) -> str | None:
    """Block before any mutation unless the direct-path fault lane is in place.

    Returns the postgres container id when ``postgres`` is asked for.
    """
    units = running_units()
    if len(units) < 2:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{len(units)} running worker slot(s); set KDIVE_WORKER_COUNT=2"
        )
    if "authority" in run_identity(base_url).deployed_roles:
        raise ScenarioStop(Outcome.BLOCKED, "an installed authority routes provision elsewhere")
    sudo = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["sudo", "-n", "-l", _HOLD_PYTHON], capture_output=True, timeout=30.0, check=False
    )
    if sudo.returncode != 0:
        raise ScenarioStop(Outcome.BLOCKED, f"sudo -n may not run {_HOLD_PYTHON} (worker hold)")
    journal = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["journalctl", "--no-pager", "-o", "cat", "-n", "1", "-u", "kdive-live-worker@*"],
        capture_output=True,
        timeout=30.0,
        check=False,
    )
    if journal.returncode != 0 or not journal.stdout.strip():
        raise ScenarioStop(Outcome.BLOCKED, "no worker journal access; join systemd-journal")
    return postgres_container() if postgres else None


async def require_direct(db_url: str, job_id: str) -> None:
    """The provision took the direct handler, not an authority route."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT payload ? 'authority_system_v1' FROM jobs WHERE id = %s", (job_id,)
        )
        row = await cur.fetchone()
    if row is None or row[0]:
        raise ScenarioStop(Outcome.BLOCKED, f"provision job {job_id} carries an authority marker")


def _domain(provider: Provider, system_id: str) -> tuple[bool, str | None]:
    """Whether the System's domain is active, and its UUID (``None`` when undefined)."""
    conn = provider.connect()
    try:
        try:
            dom = conn.lookupByName(domain_name_for(UUID(system_id)))
        except libvirt.libvirtError:
            return False, None
        return bool(dom.isActive()), dom.UUIDString()
    finally:
        conn.close()


def record_domain(held: Held, xml: str) -> None:
    """Keep the System's overlay for the frame's proofs and its accelerator for the cell context."""
    held.overlays = domain_disks(xml)
    held.disks = list(held.overlays)
    accelerator = ET.fromstring(xml).get("type", "")  # noqa: S314  # nosec B314
    held.run.observed["accelerator"] = ACCELERATORS.get(accelerator, "none")


async def in_flight(
    held: Held, db_url: str, job_id: str, deadline_s: float
) -> tuple[RunningJobClaim, str]:
    """Wait for the fault point; record the owned overlay and return the claim and domain UUID."""
    assert held.system_id is not None
    deadline = time.monotonic() + deadline_s
    while True:
        row = await job_holder(db_url, job_id)
        active, uuid = await asyncio.to_thread(_domain, held.provider, held.system_id)
        if window_reached(active, row) and uuid is not None:
            claim = await running_job_claim(db_url, job_id)
            if claim.attempt != 1:
                raise _fail(
                    f"fault point not reached (incomplete): claim at attempt {claim.attempt}"
                )
            record_domain(held, await asyncio.to_thread(held.provider.xml, held.system_id))
            return claim, uuid
        if window_missed(row) or time.monotonic() >= deadline:
            raise _fail(f"fault point not reached (incomplete): job {row}")
        await asyncio.sleep(_POLL_S)


async def until_running(db_url: str, job_id: str, deadline_s: float) -> JobRow:
    """Wait for the job to be held by a worker; a terminal or late job is an incomplete cell."""
    deadline = time.monotonic() + deadline_s
    while True:
        row = await job_holder(db_url, job_id)
        if row.state == "running":
            return row
        if window_missed(row) or time.monotonic() >= deadline:
            raise _fail(f"provision never ran before the in-flight replay: {row} (incomplete)")
        await asyncio.sleep(_POLL_S)


async def sampled[T](held: Held, waiting: Awaitable[T]) -> T:
    """Await ``waiting`` while sampling protected state every 15 s."""
    assert held.sampler is not None
    task = asyncio.ensure_future(waiting)
    while not task.done():
        await held.sampler.sample(held.op)
        await asyncio.wait({task}, timeout=_SAMPLE_S)
    return task.result()


def terminal_s(window_s: float) -> float:
    """Every attempt's window, one lease lapse and slack: the terminal-job bound."""
    return DEFAULT_MAX_ATTEMPTS * window_s + DEFAULT_LEASE.total_seconds() + 120.0


async def system_state(op: LiveStackClient, system_id: str) -> str:
    env = await scalar(op, "systems.get", system_id=system_id)
    return "failed" if system_failed(env) else env.status


def invocation_journal(invocation_id: str) -> str:
    """The journal of one systemd invocation, message text only."""
    return _run(
        ["journalctl", "--no-pager", "-o", "cat", f"_SYSTEMD_INVOCATION_ID={invocation_id}"]
    )


async def stale_journal(job_id: str, invocation_id: str, deadline_s: float) -> str:
    """Wait for the resumed stale invocation to log its outcome for ``job_id``."""
    deadline = time.monotonic() + deadline_s
    while True:
        journal = await asyncio.to_thread(invocation_journal, invocation_id)
        if stale_outcome(journal, job_id) is not None or time.monotonic() >= deadline:
            return journal
        await asyncio.sleep(2.0)


def ready_urls(base_url: str, units: Sequence[str]) -> list[str]:
    """The ``/readyz`` of the server, the reconciler and every running fixed worker slot.

    The host lane runs no lifecycle-witness listener, so its default port is not probed.
    """
    roles = readyz_urls(base_url, {})
    slots = _active_worker_readyz_urls(base_url, "\n".join(units))
    return [roles["server"], roles["reconciler"], *(url for _, url in slots)]


async def wait_ready(base_url: str, deadline_s: float) -> float | None:
    """Seconds until every role's ``/readyz`` answers 200, or ``None`` past the deadline."""
    started = time.monotonic()
    urls = ready_urls(base_url, await asyncio.to_thread(running_units))
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() - started < deadline_s:
            codes = []
            for url in urls:
                try:
                    codes.append((await client.get(url)).status_code)
                except httpx.HTTPError:
                    codes.append(0)
            if all(code == 200 for code in codes):
                return round(time.monotonic() - started, 1)
            await asyncio.sleep(2.0)
    return None


async def _count(db_url: str, query: LiteralString, allocation: str) -> int:
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(query, (allocation,))
        row = await cur.fetchone()
    return int(row[0]) if row else 0


_PROVISION_JOBS = (
    "SELECT count(*) FROM jobs j JOIN systems s ON j.payload->>'system_id' = s.id::text "
    "WHERE j.kind = 'provision' AND s.allocation_id = %s"
)
_SYSTEMS = "SELECT count(*) FROM systems WHERE allocation_id = %s"


async def stored_result(db_url: str, key: str) -> dict[str, object] | None:
    """The committed idempotency row for ``key`` (evidence only)."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT result FROM idempotency_keys WHERE key = %s AND kind = 'systems.provision'",
            (key,),
        )
        row = await cur.fetchone()
    return None if row is None else dict(row[0])


async def overlay_present(held: Held) -> bool:
    return bool(held.overlays) and not await asyncio.to_thread(
        held.provider.absent, held.overlays[0]
    )


# --- the frame -------------------------------------------------------------------------

Body = Callable[[Held, dict[str, object], float], Awaitable[None]]


async def frame(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, remote: bool, body: Body
) -> None:
    """Hold one default-shape Allocation of the cell's provider for ``body``.

    ``body`` gets the guest profile and the provision's first-boot window.
    """
    kernel = fixture()
    if not remote:

        async def local(held: Held) -> None:
            guest = await local_guest(held.op, run, kernel)
            await body(held, guest.profile(DEFAULT_SIZING), local_window_s())

        await on_allocation(run, base_url, issuer, db_url, DEFAULT_SIZING, local, project=PROJECT)
        return
    host = remote_host()
    observe_host(run, host)
    guest = await asyncio.to_thread(remote_guest, run, host, kernel)

    async def on_remote(held: Held) -> None:
        await body(held, guest.profile(DEFAULT_SIZING), REMOTE_FIRST_BOOT_S)

    await on_allocation(
        run, base_url, issuer, db_url, DEFAULT_SIZING, on_remote, remote_provider(host), PROJECT
    )


async def _finish(held: Held, job_id: str) -> None:
    """Prove no-duplicates and protected-state, then release and prove cleanup."""
    await held.prove_held({"job_id": job_id})
    held.run.prove("cleanup", await held.release())


# --- the scenarios -----------------------------------------------------------------------


async def worker_interruption(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path, remote: bool
) -> None:
    """Pause the holder mid-provision; another worker reclaims it; the stale attempt is inert."""
    del tmp
    preconditions(base_url, postgres=False)

    async def body(held: Held, profile: dict[str, object], window_s: float) -> None:
        job_id = await held.provision(profile)
        assert held.system_id is not None and held.sampler is not None
        await require_direct(db_url, job_id)
        claim, uuid_before = await in_flight(held, db_url, job_id, window_s)
        invocation = await asyncio.to_thread(set_exact_worker_hold, claim, "stop")
        try:
            try:
                replacement = await sampled(held, wait_for_reclaimed_job(db_url, job_id, claim))
            except TimeoutError:
                raise _fail("no other worker reclaimed the paused job (incomplete)") from None
            await wait_terminal(held.op, job_id, held.sampler, deadline_s=terminal_s(window_s))
            final = await job_holder(db_url, job_id)
        finally:
            await asyncio.to_thread(set_exact_worker_hold, claim, "continue")
        journal = await stale_journal(job_id, invocation, window_s + 60.0)
        await held.sampler.sample(held.op)
        active_after, uuid_after = await asyncio.to_thread(_domain, held.provider, held.system_id)
        seen = Interruption(
            original=claim,
            replacement=replacement,
            final=final,
            after_resume=await job_holder(db_url, job_id),
            system_state=await system_state(held.op, held.system_id),
            domain_uuid_before=uuid_before,
            domain_uuid_after=uuid_after,
            domain_active_after=active_after,
            overlay_present=await overlay_present(held),
            stale_journal=journal,
            job_id=job_id,
        )
        run.prove("worker-interruption", {**interruption_verdict(seen), "invocation": invocation})
        await _finish(held, job_id)

    await frame(run, base_url, issuer, db_url, remote=remote, body=body)


async def lost_response(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path, remote: bool
) -> None:
    """Drop a committed keyed provision's response; every replay of the key is that admission."""
    del tmp
    preconditions(base_url, postgres=False)

    async def replay(allocation: str, profile: dict[str, object], key: str) -> ToolResponse:
        token = mint_role_token(
            issuer, project=PROJECT, agent_session=f"{PROJECT}-sess", role="operator"
        )
        async with LiveStackClient.over_http(base_url, token) as client:
            return ok(
                await scalar(
                    client,
                    "systems.provision",
                    allocation_id=allocation,
                    profile=profile,
                    idempotency_key=key,
                ),
                "replay",
            )

    async def body(held: Held, profile: dict[str, object], window_s: float) -> None:
        key = f"q3117-{uuid4()}"
        await require_direct(db_url, await held.provision(profile, idempotency_key=key))
        assert held.system_id is not None and held.sampler is not None
        provisioned = held.system_id
        # The lost response: from here the job is known only from the committed row.
        stored = await stored_result(db_url, key)
        if stored is None:
            raise _fail("the keyed provision committed no idempotency row")
        job_id, _system = admitted(stored)
        before = await until_running(db_url, job_id, window_s)
        running = await replay(held.allocation, profile, key)
        after = await job_holder(db_url, job_id)
        if after.state != "running":
            raise _fail(f"provision left running during the in-flight replay: {after} (incomplete)")
        await wait_terminal(held.op, job_id, held.sampler, deadline_s=terminal_s(window_s))
        done = await replay(held.allocation, profile, key)
        record_domain(held, await asyncio.to_thread(held.provider.xml, provisioned))
        seen = LostResponse(
            stored=stored,
            provisioned_system=provisioned,
            replays=[running, done],
            provision_jobs=await _count(db_url, _PROVISION_JOBS, held.allocation),
            systems=await _count(db_url, _SYSTEMS, held.allocation),
            final=await job_holder(db_url, job_id),
            system_state=await system_state(held.op, provisioned),
        )
        proof = lost_response_verdict(seen)
        run.prove("lost-response", {**proof, "replayed_while": [before.state, after.state]})
        await _finish(held, job_id)

    await frame(run, base_url, issuer, db_url, remote=remote, body=body)


async def backing_service(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path, remote: bool
) -> None:
    """Stop Postgres mid-provision for a fixed outage; the job resumes on the same database."""
    del tmp
    container = preconditions(base_url, postgres=True)
    assert container is not None

    async def body(held: Held, profile: dict[str, object], window_s: float) -> None:
        since = time.time()
        units = await asyncio.to_thread(unit_identity)
        job_id = await held.provision(profile)
        assert held.system_id is not None and held.sampler is not None
        await require_direct(db_url, job_id)
        await in_flight(held, db_url, job_id, window_s)
        await held.sampler.sample(held.op)
        domains: list[list[str]] = []
        try:
            await asyncio.to_thread(_run, compose_argv(ROOT, "stop"))
            # No MCP call while Postgres is down; the provider's libvirt is still observable.
            ends = time.monotonic() + OUTAGE_S
            while time.monotonic() < ends:
                domains.append(sorted(await asyncio.to_thread(held.provider.domains)))
                await asyncio.sleep(_SAMPLE_S)
        finally:
            await asyncio.to_thread(_run, compose_argv(ROOT, "start"))
        after = await asyncio.to_thread(postgres_container)
        ready = await wait_ready(base_url, READY_S)
        await wait_terminal(held.op, job_id, held.sampler, deadline_s=terminal_s(window_s))
        seen = Outage(
            container_before=container,
            container_after=after,
            units_before=units,
            units_after=await asyncio.to_thread(unit_identity),
            ready_after_s=ready,
            final=await job_holder(db_url, job_id),
            system_state=await system_state(held.op, held.system_id),
            domain_active=(await asyncio.to_thread(_domain, held.provider, held.system_id))[0],
            journal=await asyncio.to_thread(worker_journal, since),
            job_id=job_id,
        )
        proof = outage_verdict(seen)
        run.prove("backing-service", {**proof, "outage_s": OUTAGE_S, "outage_domains": domains})
        await _finish(held, job_id)

    await frame(run, base_url, issuer, db_url, remote=remote, body=body)
