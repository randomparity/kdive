"""Guard: the weekly test-ordering workflow covers reproducible assertions and collection.

ADR-0577's `--tb=short` trade is "escalate or re-run to get full detail" — except in the
weekly ordering job, where `PYTHONHASHSEED: random` once drew a per-process seed CPython
never records. The job now records one concrete per-run seed before running assertions, so
`PYTHONHASHSEED=<seed> just test` reproduces a red run (#2065).

Sharing that seed across xdist workers removed the old accidental collection-mismatch abort.
The companion `just test-collect-order` recipe replaces it directly: collect the shared
`_TEST_MARKERS` tier under fixed seeds 1 and 2, then diff ordered node IDs. These guards keep
both contracts reproducible and wired into the weekly workflow (#2072).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "test-ordering.yml"

#: The value this guard exists to ban: CPython draws a per-process seed for it and never
#: exposes it, so a run under it cannot be reproduced.
_UNRECORDABLE_SEED = "random"

#: The `just test` invocation, however it is spelled inside a longer `run:` line.
_JUST_TEST = re.compile(r"(?:^|[^\w-])just test(?:\s|$)")

#: The seed as the *expanded* shell variable — `$PYTHONHASHSEED` or `${PYTHONHASHSEED}`.
#: A record step must write this, not a literal: a hard-coded value satisfies a plain
#: substring check while naming a seed the suite did not run under.
_SEED_EXPANSION = re.compile(r"\$\{?PYTHONHASHSEED\b")

#: The shell verbs that write a value out. A step that only mentions the variable records
#: nothing.
_RECORDING_VERB = re.compile(r"\b(?:echo|printf|tee)\b")

#: The just recipe added beside the recorded-seed suite to detect collection order drift.
_JUST_COLLECT_ORDER = re.compile(r"(?:^|[^\w-])just test-collect-order(?:\s|$)")


def _collection_order_recipe() -> str:
    """Return the collection-order recipe body without accepting a similarly named recipe."""
    justfile = (_ROOT / "justfile").read_text(encoding="utf-8")
    match = re.search(
        r"(?m)^test-collect-order(?: [^:\n]*)?:\n"
        r"(?P<body>(?:(?:^[ \t].*|^)\n)*)",
        justfile,
    )
    assert match is not None, "justfile has no `test-collect-order` recipe"
    return match.group("body")


def _write_collection_fixture(tmp_path: Path, parametrization: str) -> Path:
    """Write one isolated parametrized test module for the recipe's behavioral guard."""
    test_file = tmp_path / "test_hash_collection.py"
    test_file.write_text(
        "import pytest\n\n"
        f"VALUES = {parametrization}\n\n"
        '@pytest.mark.parametrize("value", VALUES)\n'
        "def test_value(value):\n"
        "    assert value\n",
        encoding="utf-8",
    )
    return test_file


def _run_collection_order(test_file: Path) -> subprocess.CompletedProcess[str]:
    """Run the public recipe against one isolated module and retain its useful diff."""
    return subprocess.run(
        ["just", "test-collect-order", str(test_file)],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


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
    return list(steps)


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
            and _SEED_EXPANSION.search(run)
            and "GITHUB_STEP_SUMMARY" in run
            and _RECORDING_VERB.search(run)
        ):
            record_index = index
    assert test_index is not None, "no step in the ordering job runs `just test`"
    assert record_index is not None, (
        "no step writes the expanded PYTHONHASHSEED to the step log and the job summary, "
        "so a red run cannot tell the reader which seed to reproduce (#2065). A literal "
        "value does not count: it names a seed the suite may not have run under"
    )
    assert record_index < test_index, (
        "the seed is recorded after the test step, so a red test run never records the "
        "seed it ran under"
    )


def test_the_ordering_job_runs_the_collection_order_detector() -> None:
    """The weekly workflow must check collection drift as well as assertion ordering."""
    job = _ordering_job()
    runs: list[str] = []
    for step in _job_steps(job):
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if isinstance(run, str):
            runs.append(run)
    assert any(_JUST_COLLECT_ORDER.search(run) for run in runs), (
        "the weekly ordering job never runs `just test-collect-order`, so a parametrization "
        "backed by an unsorted set can change collection order without failing"
    )


def test_collection_order_recipe_reuses_the_gated_marker_tier_and_fixed_seeds() -> None:
    """Direct collection must share `_TEST_MARKERS` and remain exactly reproducible."""
    recipe = _collection_order_recipe()
    assert "{{_TEST_MARKERS}}" in recipe, (
        "`test-collect-order` re-types or omits the gated marker expression instead of reusing "
        "`_TEST_MARKERS`"
    )
    assert re.search(r"PYTHONHASHSEED=[\"']?1[\"']?", recipe), (
        "`test-collect-order` does not collect under the concrete valid seed 1"
    )
    assert re.search(r"PYTHONHASHSEED=[\"']?2[\"']?", recipe), (
        "`test-collect-order` does not collect under the concrete valid seed 2"
    )


def test_collection_order_recipe_passes_stable_collection(tmp_path: Path) -> None:
    """A list-backed parametrization has the same ordered node IDs under both seeds."""
    test_file = _write_collection_fixture(tmp_path, '["alpha", "bravo", "charlie"]')

    result = _run_collection_order(test_file)

    assert result.returncode == 0, result.stdout + result.stderr


def test_collection_order_recipe_fails_reproducibly_with_node_id_diff(tmp_path: Path) -> None:
    """An unsorted set must fail identically and name the reordered parametrized tests."""
    test_file = _write_collection_fixture(
        tmp_path,
        '{"alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"}',
    )

    first = _run_collection_order(test_file)
    second = _run_collection_order(test_file)

    assert first.returncode == 1, first.stdout + first.stderr
    assert second.returncode == 1, second.stdout + second.stderr
    assert first.stdout == second.stdout
    assert "--- PYTHONHASHSEED=1" in first.stdout
    assert "+++ PYTHONHASHSEED=2" in first.stdout
    assert "-test_hash_collection.py::test_value[" in first.stdout
    assert "+test_hash_collection.py::test_value[" in first.stdout
