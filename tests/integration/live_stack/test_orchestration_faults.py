"""Unit tests for the orchestration-fault carrier's verdicts (#3117)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import job_envelope
from kdive.services.systems.admission import ProvisionJobAdmitted, stored_admission_result
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack import orchestration_faults as of
from tests.integration.live_stack.resource_limits import cells, failure
from tests.integration.live_stack.scenario import ScenarioStop
from tests.live_vm.installed_local_authority_support import RunningJobClaim

_NOW = datetime(2026, 10, 20, tzinfo=UTC)
_JOB = "0b9ee7c4-5d0e-4c58-9a51-6c1f1b1f0a01"
_OTHER = "7f3b1c55-21a4-4a8e-8c3a-0d0d6a4f9b22"
_W1 = "local-systemd:kdive-live-worker@1.service:" + "a" * 32
_W2 = "local-systemd:kdive-live-worker@2.service:" + "b" * 32
_UUID = "5d1c2f9a-8f0e-4f4e-9d3c-2b1a0e9f8c7d"


def _line(message: str) -> str:
    # The fixed worker logs JSON lines; journalctl -o cat prints them verbatim.
    return json.dumps({"logger": "kdive.jobs.worker", "msg": message, "level": "warning"})


def _claim(worker: str, attempt: int) -> RunningJobClaim:
    return RunningJobClaim(worker, attempt, _NOW, _NOW)


def _refused(call: Callable[[], object]) -> str:
    with pytest.raises(ScenarioStop) as stop:
        call()
    assert stop.value.outcome is Outcome.FAILURE
    return str(stop.value)


# --- the in-flight window --------------------------------------------------------------


@pytest.mark.parametrize(
    ("active", "row", "reached"),
    [
        (True, of.JobRow("running", 1, _W1), True),
        (False, of.JobRow("running", 1, _W1), False),
        (True, of.JobRow("running", 0, None), False),
        (True, of.JobRow("queued", 0, None), False),
        (True, of.JobRow("running", 2, _W2), False),
    ],
    ids=["reached", "domain-down", "attempt-0", "queued", "already-reclaimed"],
)
def test_window_is_attempt_one_running_with_an_active_domain(
    active: bool, row: of.JobRow, reached: bool
) -> None:
    assert of.window_reached(active, row) is reached


@pytest.mark.parametrize("state", ["succeeded", "failed", "canceled"])
def test_a_terminal_job_misses_the_window(state: str) -> None:
    assert of.window_missed(of.JobRow(state, 1, _W1))
    assert not of.window_missed(of.JobRow("running", 1, _W1))


# --- worker interruption ---------------------------------------------------------------

_FINAL = of.JobRow("succeeded", 2, _W2)
_GOOD = of.Interruption(
    original=_claim(_W1, 1),
    replacement=_claim(_W2, 2),
    final=_FINAL,
    after_resume=_FINAL,
    system_state="ready",
    domain_uuid_before=_UUID,
    domain_uuid_after=_UUID,
    domain_active_after=True,
    overlay_present=True,
    stale_journal=_line(f"job {_JOB} completed but was reclaimed; result dropped"),
    job_id=_JOB,
)


def test_interruption_accepts_a_dropped_stale_result() -> None:
    proof = of.interruption_verdict(_GOOD)
    assert proof["stale_outcome"] == "dropped"
    assert proof["replacement"] == {"worker": _W2, "attempt": 2}


def test_interruption_accepts_a_stale_failure_that_changed_nothing() -> None:
    journal = _line(f"job {_JOB} failed: provisioning_failure")
    seen = replace(_GOOD, stale_journal=journal)
    assert of.interruption_verdict(seen)["stale_outcome"] == "failed:provisioning_failure"


@pytest.mark.parametrize(
    ("seen", "reason"),
    [
        (replace(_GOOD, replacement=_claim(_W1, 2)), "own paused worker"),
        (replace(_GOOD, replacement=_claim(_W2, 1)), "is not above"),
        (
            replace(
                _GOOD, final=of.JobRow("failed", 2, _W2), after_resume=of.JobRow("failed", 2, _W2)
            ),
            "reclaimed job ended",
        ),
        (replace(_GOOD, after_resume=of.JobRow("succeeded", 2, _W1)), "changed the job row"),
        (replace(_GOOD, system_state="failed"), "failed the System"),
        (replace(_GOOD, system_state="provisioning"), "not ready"),
        (replace(_GOOD, domain_uuid_after=None), "tore down"),
        (replace(_GOOD, domain_uuid_after="00000000-0000-4000-8000-000000000000"), "UUID changed"),
        (replace(_GOOD, domain_active_after=False), "stopped the reclaimed System's domain"),
        (replace(_GOOD, overlay_present=False), "reclaimed the overlay"),
        (replace(_GOOD, stale_journal=""), "logged no outcome"),
        (
            replace(
                _GOOD,
                stale_journal=_line(f"job {_OTHER} completed but was reclaimed; result dropped"),
            ),
            "logged no outcome",
        ),
        (
            replace(_GOOD, stale_journal=_line(f"job {_OTHER} failed: provisioning_failure")),
            "no outcome",
        ),
    ],
    ids=[
        "same-worker",
        "same-attempt",
        "job-failed",
        "row-changed",
        "system-failed",
        "system-not-ready",
        "domain-gone",
        "domain-replaced",
        "domain-stopped",
        "overlay-gone",
        "no-journal",
        "other-job-dropped",
        "other-job-failed",
    ],
)
def test_interruption_refuses(seen: of.Interruption, reason: str) -> None:
    assert reason in _refused(lambda: of.interruption_verdict(seen))


# --- lost response ---------------------------------------------------------------------


def _admitted(system_id: UUID) -> tuple[dict[str, object], ToolResponse]:
    job = Job(
        id=UUID(_JOB),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.PROVISION,
        payload={"system_id": str(system_id)},
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "p", "agent_session": None, "project": of.PROJECT},
        dedup_key="alloc:provision",
    )
    stored = {"envelope": stored_admission_result(ProvisionJobAdmitted(job, system_id)).document}
    return stored, job_envelope(job, "system_id", system_id)


def test_lost_response_accepts_identical_replays() -> None:
    system = uuid4()
    stored, replay = _admitted(system)
    proof = of.lost_response_verdict(
        of.LostResponse(
            stored, str(system), [replay, replay], 1, 1, of.JobRow("succeeded", 1, _W1), "ready"
        )
    )
    assert (proof["job_id"], proof["system_id"]) == (_JOB, str(system))


def test_lost_response_refuses() -> None:
    system = uuid4()
    stored, replay = _admitted(system)
    done = of.JobRow("succeeded", 1, _W1)
    other_job = replay.model_copy(update={"object_id": _OTHER})
    other_system = replay.model_copy(update={"data": {**replay.data, "system_id": str(uuid4())}})
    cases = {
        "replay returned job": (stored, str(system), [replay, other_job], 1, 1, done, "ready"),
        "replay returned system": (stored, str(system), [other_system], 1, 1, done, "ready"),
        "not the provisioned": (stored, str(uuid4()), [replay], 1, 1, done, "ready"),
        "2 provision jobs": (stored, str(system), [replay], 2, 1, done, "ready"),
        "2 Systems": (stored, str(system), [replay], 1, 2, done, "ready"),
        "provision job failed": (
            stored,
            str(system),
            [replay],
            1,
            1,
            of.JobRow("failed", 3, _W1),
            "failed",
        ),
        "not a provision admission": ({"envelope": {}}, str(system), [replay], 1, 1, done, "ready"),
        "no replay": (stored, str(system), [], 1, 1, done, "ready"),
    }
    for reason, args in cases.items():
        seen = of.LostResponse(*args)
        assert reason in _refused(lambda seen=seen: of.lost_response_verdict(seen)), reason


# --- backing service -------------------------------------------------------------------

_UNITS = {"kdive-live-worker@1.service": ("a" * 32, "4100")}
_HEARTBEAT = _line(f"heartbeat for job {_JOB} failed; stopping (lease will lapse)")


_OUTAGE = of.Outage(
    container_before="c0ffee",
    container_after="c0ffee",
    units_before=_UNITS,
    units_after=dict(_UNITS),
    ready_after_s=12.5,
    final=of.JobRow("succeeded", 2, _W2),
    system_state="ready",
    domain_active=True,
    journal=_HEARTBEAT + "\n" + _line(f"job {_JOB} failed: infrastructure_failure"),
    job_id=_JOB,
)


@pytest.mark.parametrize(
    ("journal", "path"),
    [
        (_line(f"job {_JOB} failed: infrastructure_failure"), "requeued"),
        (_HEARTBEAT, "lease-reclaimed"),
        (_line(f"heartbeat for job {_OTHER} failed; stopping (lease will lapse)"), "unknown"),
        ("", "unknown"),
    ],
    ids=["requeued", "lease-reclaimed", "other-job", "silent"],
)
def test_outage_labels_the_resume_path(journal: str, path: str) -> None:
    assert of.outage_verdict(replace(_OUTAGE, journal=journal))["resume_path"] == path


@pytest.mark.parametrize(
    ("seen", "reason"),
    [
        (replace(_OUTAGE, container_after="deadbeef"), "container changed"),
        (
            replace(_OUTAGE, units_after={"kdive-live-worker@1.service": ("c" * 32, "4100")}),
            "units changed",
        ),
        (
            replace(_OUTAGE, units_after={"kdive-live-worker@1.service": ("a" * 32, "4200")}),
            "units changed",
        ),
        (replace(_OUTAGE, ready_after_s=None), "did not return to ready"),
        (
            replace(_OUTAGE, final=of.JobRow("failed", 3, _W2), system_state="failed"),
            "after the outage",
        ),
        (replace(_OUTAGE, final=of.JobRow("succeeded", 1, _W1)), "never reached"),
        (replace(_OUTAGE, domain_active=False), "not running after the outage"),
    ],
    ids=[
        "container",
        "invocation",
        "main-pid",
        "not-ready",
        "job-failed",
        "attempt-1",
        "domain-down",
    ],
)
def test_outage_refuses(seen: of.Outage, reason: str) -> None:
    assert reason in _refused(lambda: of.outage_verdict(seen))


# --- the compose argv ------------------------------------------------------------------


def test_compose_argv_is_fixed_to_postgres(tmp_path: Path) -> None:
    base = ["docker", "compose", "--project-directory", str(tmp_path)]
    assert of.compose_argv(tmp_path, "stop") == [*base, "stop", "postgres"]
    assert of.compose_argv(tmp_path, "start") == [*base, "start", "postgres"]
    assert of.compose_argv(tmp_path, "ps") == [*base, "ps", "-q", "postgres"]
    with pytest.raises(AssertionError, match="refused"):
        of.compose_argv(tmp_path, "restart")  # ty: ignore[invalid-argument-type]


def test_cells_are_the_six_orchestration_cells() -> None:
    assert {(c.provider, failure(c)) for c in cells(owner=of.OWNER)} == {
        (p, f)
        for p in ("local-libvirt", "remote-libvirt")
        for f in ("worker-interruption", "lost-response", "backing-service")
    }
