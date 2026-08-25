"""Guard: the weekly test-ordering workflow records a concrete, reproducible hash seed.

ADR-0577's `--tb=short` trade is "escalate or re-run to get full detail" — except in the
weekly ordering job, where `PYTHONHASHSEED: random` drew a per-process seed CPython never
records, so re-running the job was a different run, not a reproduction (#2065). The job now
seeds with the run's number and records that value in the step log and the job summary before
the suite runs, so a red run names the concrete seed: `PYTHONHASHSEED=<seed> just test`
locally reproduces the collection and assertion order. This guard keeps that contract from
regressing to an unrecordable seed.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "test-ordering.yml"

#: The value this guard exists to ban: CPython draws a per-process seed for it and never
#: exposes it, so a run under it cannot be reproduced.
_UNRECORDABLE_SEED = "random"

#: The `just test` invocation, however it is spelled inside a longer `run:` line.
_JUST_TEST = re.compile(r"(?:^|[^\w-])just test(?:\s|$)")


def _jobs() -> dict[str, object]:
    """Parse the workflow into ``{job name: that job's mapping}``.

    The values are typed `object`, not `dict`: nothing here has validated them, and the
    callers do — the same rule as the apt-bounding guard.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert text.strip(), "test-ordering.yml is empty, so every assertion would pass over nothing"
    document = yaml.safe_load(text)
    jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
    assert isinstance(jobs, dict) and jobs, "test-ordering.yml has no jobs"
    return jobs


def _job_steps(job: object) -> list[object]:
    """The job's steps in execution order; empty when the job declares none."""
    if not isinstance(job, dict):
        return []
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps]


def _ordering_job() -> dict[str, object]:
    """The job whose steps run `just test` — the ordering job, however it is named."""
    for job in _jobs().values():
        if not isinstance(job, dict):
            continue
        for step in _job_steps(job):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if isinstance(run, str) and _JUST_TEST.search(run):
                return {str(key): value for key, value in job.items()}
    raise AssertionError("no job in test-ordering.yml runs `just test`")


def _test_step(job: dict[str, object]) -> dict[str, object]:
    """The step of the ordering job that runs `just test`."""
    for step in _job_steps(job):
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if isinstance(run, str) and _JUST_TEST.search(run):
            return {str(key): value for key, value in step.items()}
    raise AssertionError("no step in the ordering job runs `just test`")


def _effective_seed(job: object, step: object) -> object | None:
    """The PYTHONHASHSEED the step's `just test` inherits: step env beats job env."""
    step_env = step.get("env") if isinstance(step, dict) else None
    if isinstance(step_env, dict) and "PYTHONHASHSEED" in step_env:
        return step_env.get("PYTHONHASHSEED")
    job_env = job.get("env") if isinstance(job, dict) else None
    if isinstance(job_env, dict):
        return job_env.get("PYTHONHASHSEED")
    return None


def test_the_ordering_job_runs_with_a_recordable_seed() -> None:
    """The suite must run under a seed value the run can state, not CPython's private draw."""
    job = _ordering_job()
    seed = _effective_seed(job, _test_step(job))
    assert seed is not None, (
        "the `just test` step sets no PYTHONHASHSEED, so the recipe's default of 0 runs "
        "and the weekly job no longer exercises an unpinned order"
    )
    assert str(seed) != _UNRECORDABLE_SEED, (
        "the ordering job runs with PYTHONHASHSEED=random: the seed CPython draws is "
        "never recorded, so a red run is a different run, not a reproduction (#2065)"
    )


def test_the_seed_changes_with_the_run() -> None:
    """The seed must derive from the run, or one order is exercised forever.

    `github.run_number` (a per-workflow counter), not `github.run_id`: run ids already
    exceed CPython's PYTHONHASHSEED range [0, 4294967295], and an out-of-range value is a
    fatal error at interpreter startup.
    """
    job = _ordering_job()
    seed = _effective_seed(job, _test_step(job))
    assert seed is not None, "the `just test` step sets no PYTHONHASHSEED at all"
    assert "github.run_number" in str(seed), (
        f"the seed {seed!r} does not derive from the run, so consecutive weekly runs "
        "share one order and mask every order-dependent test that order happens to pass"
    )


def test_the_seed_is_recorded_before_the_suite_runs() -> None:
    """A red run must name the seed it ran under, recorded before the failure buries it."""
    job = _ordering_job()
    test_index: int | None = None
    record_index: int | None = None
    for index, step in enumerate(_job_steps(job)):
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if not isinstance(run, str):
            continue
        if test_index is None and _JUST_TEST.search(run):
            test_index = index
        if (
            record_index is None
            and "PYTHONHASHSEED" in run
            and "GITHUB_STEP_SUMMARY" in run
            and re.search(r"\b(?:echo|tee)\b", run)
        ):
            record_index = index
    assert test_index is not None, "no step in the ordering job runs `just test`"
    assert record_index is not None, (
        "no step records PYTHONHASHSEED to the step log and the job summary, so a red "
        "run cannot tell the reader which seed to reproduce (#2065)"
    )
    assert record_index < test_index, (
        "the seed is recorded after the test step, so a red test run never records the "
        "seed it ran under"
    )
