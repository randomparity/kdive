"""Strand a real build-use pin for the recovery tool cells (#3111, ADR-0722 2026-10-14).

An install job pins its reusable build between ``acquire_build_use`` and ``release_build_use``
(``src/kdive/jobs/handlers/runs/install.py``). :func:`on_stranded_pin` provisions the lane image
under a frame of its own, uploads the verified ``longterm`` fixture with its ``vmlinux`` (so the
install injects modules and holds the pin for seconds), calls ``runs.install`` and freezes the
holder's worker unit once the pin row appears. The body then sees a live holder with a pin; its
:meth:`Stranded.kill` kills the unit and has the lifecycle witness publish the incarnation's
durable termination, the only fact ``ops.recover_build_use`` accepts as death evidence. The frame
always restores the worker fleet, waits for the install job and leaves no pin behind.
``docs/workflow/specs/2026-10-14-build-use-recovery-cells-design.md`` is the design.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import time
import xml.etree.ElementTree as ET  # noqa: S405 - the worker's own domain XML  # nosec B405
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, LiteralString

import psycopg
from psycopg.rows import dict_row

from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
    LifecycleResponse,
    SlotPhase,
)
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.deep_lifecycle import FIXTURE_ROOT_ENV, load_fixture
from tests.integration.live_stack.scenario import CellRun, ScenarioStop, provision_catalog
from tests.integration.live_stack.spine import build_and_upload_kernel, build_profile, ok, scalar
from tests.integration.live_stack.tool_cells import lane_for

_ROOT = Path(__file__).resolve().parents[3]
_LIFECYCLE = _ROOT / "scripts" / "live-stack" / "worker-lifecycle.sh"
_BASELINE = "longterm"
_WORKER_UNIT = re.compile(r"kdive-live-worker@[1-8]\.service")
_POLL_S = 0.02
_PIN_DEADLINE_S = 600.0
_RESTORE_DEADLINE_S = 300.0
_DEATH_DEADLINE_S = 30.0
_TERMINAL_JOBS = frozenset({"succeeded", "failed", "canceled"})
# The transaction lock acquire, release and recovery of one holder's pins all take (0105, 0108).
_HOLDER_LOCK: LiteralString = "hashtextextended('kdive:worker-incarnation:' || %s, 1803)"

Rows = list[dict[str, Any]]


async def rows(db_url: str, query: LiteralString, params: Sequence[object] = ()) -> Rows:
    """``query``'s rows from a read-only session on the evidence database."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return list(await cursor.fetchall())


PINS: LiteralString = (
    "SELECT use_id, investigation_id, generation, job_id, attempt, holder_worker_id, created_at "
    "FROM investigation_build_uses WHERE investigation_id = ANY(%s::uuid[]) "
    "ORDER BY created_at, use_id"
)
ALL_PINS: LiteralString = "SELECT * FROM investigation_build_uses ORDER BY created_at, use_id"
INCARNATION: LiteralString = (
    "SELECT incarnation, state, outcome, authority_kind, authority_binding, terminated_at "
    "FROM worker_incarnations WHERE incarnation = %s"
)
_JOB_STATE: LiteralString = "SELECT state FROM jobs WHERE id = %s"


def _sudo(*argv: str) -> None:
    result = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["sudo", "-n", *argv], capture_output=True, text=True, timeout=60.0, check=False
    )
    if result.returncode != 0:
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"sudo -n {' '.join(argv[:2])} exited {result.returncode}: {result.stderr[-300:]}; "
            "the recovery cells need passwordless sudo for systemctl on the worker units",
        )


def _lifecycle(*argv: str) -> LifecycleResponse:
    result = subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
        [str(_LIFECYCLE), *argv],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=300.0,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    try:
        response = LifecycleResponse.model_validate_json(result.stdout.strip())
    except ValueError:
        raise AssertionError(
            f"worker-lifecycle.sh {' '.join(argv)} exited {result.returncode} without a response "
            f"frame: {result.stderr[-500:]}"
        ) from None
    assert response.ok, f"worker-lifecycle.sh {' '.join(argv)}: {response.model_dump_json()}"
    return response


def _active_state(unit: str) -> str:
    result = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["systemctl", "show", "--property=ActiveState", "--value", unit],
        capture_output=True,
        text=True,
        timeout=30.0,
        check=True,
    )
    return result.stdout.strip()


async def _holder_lock_free(db_url: str, holder: str) -> bool:
    """Whether no transaction holds ``holder``'s pin lock; a granted probe is released at once."""
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        row = await (
            await conn.execute(f"SELECT pg_try_advisory_lock({_HOLDER_LOCK})", (holder,))
        ).fetchone()
        assert row is not None
        if row[0]:
            await conn.execute(f"SELECT pg_advisory_unlock({_HOLDER_LOCK})", (holder,))
        return bool(row[0])


@dataclass(frozen=True)
class Seams:
    """The host and database effects a stranded pin needs; tests replace them."""

    sudo: Callable[..., None] = _sudo
    lifecycle: Callable[..., LifecycleResponse] = _lifecycle
    active_state: Callable[[str], str] = _active_state
    rows: Callable[[str, LiteralString, Sequence[object]], Awaitable[Rows]] = rows
    lock_free: Callable[[str, str], Awaitable[bool]] = _holder_lock_free
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass
class Stranded:
    """A pin whose holder is alive and frozen until :meth:`kill`."""

    db_url: str
    project: str
    pin: dict[str, Any]
    unit: str
    seams: Seams
    killed: bool = False

    @property
    def holder(self) -> str:
        return str(self.pin["holder_worker_id"])

    async def incarnation(self) -> dict[str, Any]:
        found = await self.seams.rows(self.db_url, INCARNATION, (self.holder,))
        assert len(found) == 1, f"holder {self.holder} has {len(found)} incarnation rows"
        return found[0]

    async def kill(self) -> dict[str, Any]:
        """Kill the frozen holder and have the lifecycle witness publish its termination."""
        self.seams.sudo("systemctl", "kill", "--signal=SIGKILL", self.unit)
        self.killed = True
        deadline = time.monotonic() + _DEATH_DEADLINE_S
        while (state := self.seams.active_state(self.unit)) not in {"failed", "inactive"}:
            assert time.monotonic() < deadline, f"{self.unit} still {state} after SIGKILL"
            await self.seams.sleep(0.2)
        await asyncio.to_thread(self.seams.lifecycle, "status")
        incarnation = await self.incarnation()
        assert incarnation["state"] == "terminated" and incarnation["outcome"], (
            f"the witness published no termination for {self.holder}: {incarnation}"
        )
        return incarnation


StrandedBody = Callable[[Stranded], Awaitable[dict[str, object]]]


def _fixture() -> tuple[Path, dict[str, Any]]:
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the {_BASELINE} fixture"
        )
    try:
        return load_fixture(Path(root), _BASELINE, platform.machine())
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{_BASELINE} fixture: {exc}") from None


def started_slots(response: LifecycleResponse) -> int:
    """How many slots of the fleet run a started worker."""
    return sum(slot.phase is SlotPhase.STARTED for slot in response.slots)


async def freeze_holder(
    db_url: str, investigation: str, seams: Seams, *, deadline_s: float = _PIN_DEADLINE_S
) -> tuple[dict[str, Any], str]:
    """Freeze the worker holding ``investigation``'s pin; ``blocked`` when the race is lost.

    The holder is frozen outside its release transaction only when the pin row still exists and
    no transaction holds the holder's pin lock; otherwise a recovery call would wait on it. Any
    other outcome thaws the holder before it returns.
    """
    deadline = time.monotonic() + deadline_s
    while not (found := await seams.rows(db_url, PINS, ([investigation],))):
        assert time.monotonic() < deadline, f"no pin for {investigation} in {deadline_s:.0f} s"
        await seams.sleep(_POLL_S)
    pin = found[0]
    incarnation = await seams.rows(db_url, INCARNATION, (pin["holder_worker_id"],))
    unit = str(incarnation[0]["authority_binding"]["unit"]) if incarnation else ""
    # The unit name reaches `sudo systemctl`: only the eight fixed worker units qualify.
    assert _WORKER_UNIT.fullmatch(unit), f"holder {pin['holder_worker_id']} has no worker unit"
    seams.sudo("systemctl", "freeze", unit)
    frozen = False
    try:
        held = await seams.rows(db_url, PINS, ([investigation],)) == found
        frozen = held and await seams.lock_free(db_url, str(pin["holder_worker_id"]))
    finally:
        if not frozen:
            seams.sudo("systemctl", "thaw", unit)
    if not frozen:
        raise ScenarioStop(
            Outcome.BLOCKED, "the install released its pin before the holder froze; rerun the cell"
        )
    return pin, unit


async def restore(
    seams: Seams,
    db_url: str,
    stranded: Stranded | None,
    *,
    cancel: Callable[[], Awaitable[object]] | None,
    recover: Callable[[Stranded], Awaitable[None]],
    job_id: str,
    investigation: str,
    slots: int,
) -> str | None:
    """Thaw the live holder, or clear the killed holder's pin and job and replace the fleet.

    A live holder finishes its install and releases the pin itself: a job canceled under it
    would refuse that release (``release_investigation_build_use`` requires a running job). A
    killed holder's pin is recovered through ``recover`` when the body left it, its job is
    canceled so no restarted worker reclaims it, and the fleet is stopped, recovered (only
    ``recover`` resets the killed unit's ``failed`` identity) and started again. Then wait until
    the job is terminal and no pin is left. Returns the job's final state, or ``None`` when no
    install was enqueued.
    """
    if stranded is not None and stranded.killed:
        try:
            if await seams.rows(db_url, PINS, ([investigation],)):
                incarnation = await stranded.incarnation()
                assert incarnation["state"] == "terminated", (
                    f"pin {stranded.pin['use_id']} of {stranded.holder} is orphaned and its "
                    f"holder is {incarnation['state']}; recover it with ops.recover_build_use "
                    "once the lifecycle witness publishes the termination"
                )
                await recover(stranded)
        finally:
            # The job and the fleet are restored even when the pin could not be.
            if cancel is not None:
                await cancel()
            for operation in ("stop", "recover"):
                await asyncio.to_thread(seams.lifecycle, operation)
            response = await asyncio.to_thread(seams.lifecycle, "start", str(slots))
        assert started_slots(response) == slots, f"fleet restart: {response.model_dump_json()}"
    elif stranded is not None:
        seams.sudo("systemctl", "thaw", stranded.unit)
    deadline = time.monotonic() + _RESTORE_DEADLINE_S
    while True:
        state = (await seams.rows(db_url, _JOB_STATE, (job_id,)))[0]["state"] if job_id else None
        left = await seams.rows(db_url, PINS, ([investigation],))
        if not left and state in (None, *_TERMINAL_JOBS):
            return state
        assert time.monotonic() < deadline, f"install job {state}, {len(left)} pin(s) left"
        await seams.sleep(1.0)


async def on_stranded_pin(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    body: StrandedBody,
    recover: Callable[[Stranded], Awaitable[None]],
    seams: Seams | None = None,
) -> None:
    """Strand a pin in ``project``, prove ``effect`` with ``body``, then prove the cleanup.

    ``recover`` recovers a killed holder's pin through ``ops.recover_build_use`` when the body
    did not; it is cleanup, not evidence.

    The System frame runs under its own :class:`CellRun`, so the guest it observes never enters
    this service cell's context; its cleanup proof becomes this cell's ``cleanup``.
    """
    seams = seams or Seams()
    tree, manifest = _fixture()
    seams.sudo("systemctl", "--version")
    slots = started_slots(await asyncio.to_thread(seams.lifecycle, "status"))
    if not slots:
        raise ScenarioStop(Outcome.BLOCKED, "no started worker slot holds an install")
    frame = CellRun(run.cell, run.writer)
    lane = lane_for(run.cell)

    async def framed(op: LiveStackClient, system_id: str, owned: list[str]) -> None:
        investigation = ok(
            await scalar(op, "investigations.open", project=project, title="build-use recovery"),
            "open",
        ).object_id
        run_id = ok(
            await scalar(
                op,
                "runs.create",
                investigation_id=investigation,
                system_id=system_id,
                build_profile=build_profile(manifest["arch"]),
            ),
            "create-run",
        ).object_id
        stranded: Stranded | None = None
        job_id = ""
        try:
            await build_and_upload_kernel(
                op,
                run_id=run_id,
                arch=manifest["arch"],
                kernel_tree=tree,
                with_vmlinux=True,
                require_network=True,
                root_fs="ext4",
            )
            job_id = ok(await scalar(op, "runs.install", run_id=run_id), "install").object_id
            pin, unit = await freeze_holder(db_url, investigation, seams)
            stranded = Stranded(db_url, project, pin, unit, seams)
            observed = await body(stranded)
        finally:
            installed = await restore(
                seams,
                db_url,
                stranded,
                cancel=partial(scalar, op, "jobs.cancel", job_id=job_id) if job_id else None,
                recover=recover,
                job_id=job_id,
                investigation=investigation,
                slots=slots,
            )
            if installed == "succeeded":
                # The install finished before the freeze: its staged kernel joins the owned
                # paths the frame's cleanup proves absent.
                kernel = ET.fromstring(lane.xml(system_id)).findtext("./os/kernel")  # noqa: S314  # nosec B314
                owned.extend([kernel] if kernel else [])
            closed = await scalar(
                op, "investigations.close", investigation_id=investigation, summary="recovered"
            )
            assert closed.status == "closed", f"investigation not closed: {closed.status}"
        run.prove("effect", {"exposure": run.cell.exposure, **observed})

    try:
        await lane.frame(
            frame,
            base_url,
            issuer,
            db_url,
            project=project,
            body=framed,
            provision=provision_catalog,
        )
    finally:
        run.artifacts.extend(frame.artifacts)
    run.assertions["cleanup"] = frame.assertions["cleanup"]
