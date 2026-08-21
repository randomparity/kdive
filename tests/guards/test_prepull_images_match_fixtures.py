"""Guard: the CI pre-pull images match the tags the test fixtures actually start (ADR-0553).

`tests/db/conftest.py` and `tests/store/conftest.py` start their testcontainers lazily, from
inside the suite. When Docker Hub was unreachable that turned one dead registry into
`3 failed, 8938 passed, 16 skipped, 3448 errors` (#1913) — the cause was several screens down.

So CI pulls both images in a dedicated step before `just test`, and a registry outage fails
that step by itself. That step only means anything if it pulls the *same* tags the fixtures go
on to request: a pre-pull of a stale tag warms nothing, still passes, and leaves the cascade
exactly as it was — a guard-shaped no-op, which is worse than no step at all.

The tags are therefore duplicated (script + two conftests) and this guard owns the
duplication. It is deliberately **read-only** over all four files: it parses text and never
imports the fixtures, so it needs no Docker, no network, and no running daemon. It parses the
constants *by name* rather than by line number, so it survives edits around them.

Stdlib, `pyyaml` and pytest: this reads the tree, not the project. `pyyaml` is a declared
dependency and is how `tests/guards/test_apt_install_is_bounded.py` already parses these same
workflow files — the job-level checks below need a real parser, because a regex that recognises
a job key by its line shape cannot see one carrying a trailing comment and silently skips the
job it guards (#1993).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_PULL_SCRIPT = _ROOT / "scripts" / "pull-test-images.sh"
_WORKFLOWS = _ROOT / ".github" / "workflows"

#: Every workflow that runs the suite against real containers must pre-pull first.
_SUITE_WORKFLOWS = ("ci.yml", "test-ordering.yml")

#: ``_POSTGRES_IMAGE = "postgres:17"`` — the fixture constant, matched by name, not by line.
_FIXTURE_CONSTANTS = {
    "tests/db/conftest.py": "_POSTGRES_IMAGE",
    "tests/store/conftest.py": "_MINIO_IMAGE",
}

#: The ``readonly IMAGES=( ... )`` array in the pull script, up to its closing paren.
_IMAGES_ARRAY = re.compile(r"^(?:readonly\s+)?IMAGES=\((?P<body>.*?)^\)", re.MULTILINE | re.DOTALL)
_QUOTED = re.compile(r'"(?P<image>[^"\s]+)"')

#: ``run: just pull-test-images`` and ``run: just test`` as whole step values, compared exact
#: after whitespace strip so ``just test-compose-volumes`` and ``just test-ansible`` are not
#: mistaken for the suite.
_PREPULL_STEP = "just pull-test-images"
_TEST_STEP = "just test"


def _jobs(path: Path) -> dict[str, object]:
    """Parse a workflow into ``{job name: that job's mapping}``.

    The values are typed `object`, not `dict`, because nothing here has validated them — a
    malformed workflow can put anything under a job key, and claiming `dict` would move that
    check from the callers, which do it, into a signature, which cannot.

    Per job, not per file: a pre-pull step sitting in an unrelated job satisfies a whole-file
    ordering check while pre-pulling nothing for the job that runs the suite.

    `yaml.safe_load` rather than a line-anchored regex over the text (#1993). A regex that
    recognises a job by `^  name:$` cannot see `  image-build:  # pulls the base layer` or
    `"image-build":` — both ordinary YAML — and a job it cannot see is a job whose steps it
    never checks. That failure is silent in both directions: the job never lands in any
    assertion, and the per-workflow floor below only catches workflows that lose *all* their
    suite jobs, never one that lost one to invisibility. `pyyaml` is a declared dependency and
    `tests/guards/test_apt_install_is_bounded.py` already parses these same files with it, so
    this is one parser where the tree had two.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
    return jobs if isinstance(jobs, dict) else {}


def _job_run_steps(job: object) -> list[str]:
    """Every ``run:`` value directly under one job's ``steps:``, in step order, stripped.

    Typed ``object`` because nothing here has validated the job mapping — same reasoning as
    `_jobs`. Steps without their own ``run:`` (``uses:``, ``name:``-only) and anything that is
    not a step mapping contribute nothing; a job with no readable ``steps:`` has no run values.
    Stripping absorbs the trailing newline a block-scalar ``run: |`` value keeps after parsing,
    so the exact comparisons against `_TEST_STEP`/`_PREPULL_STEP` see the same string however
    the workflow spells the value.
    """
    if not isinstance(job, dict):
        return []
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    runs: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if isinstance(run, str):
            runs.append(run.strip())
    return runs


def _fixture_image(rel_path: str, constant: str) -> str:
    text = (_ROOT / rel_path).read_text(encoding="utf-8")
    # Accept both the inline form  CONST = "image"
    # and the parenthesized form   CONST = (
    #                                   "image"  # optional comment
    #                               )
    # The digest-pin convention (#1921) uses the parenthesized form to keep lines short.
    match = re.search(
        rf'^{re.escape(constant)}\s*=\s*(?:"(?P<image>[^"]+)"|'
        rf'\(\s*\n\s*"(?P<image2>[^"]+)")',
        text,
        re.MULTILINE,
    )
    assert match is not None, (
        f'{rel_path} no longer defines a module-level `{constant} = "..."`. The fixture image '
        "moved or was renamed; re-point this guard at its new home rather than deleting it — "
        "without it the CI pre-pull step can drift to a tag the suite never requests (ADR-0553)."
    )
    return match.group("image") or match.group("image2")


def _script_images() -> list[str]:
    text = _PULL_SCRIPT.read_text(encoding="utf-8")
    array = _IMAGES_ARRAY.search(text)
    assert array is not None, (
        f"{_PULL_SCRIPT.relative_to(_ROOT)} no longer declares an `IMAGES=( ... )` array, so this "
        "guard can no longer see what CI pre-pulls (ADR-0553)."
    )
    return [match.group("image") for match in _QUOTED.finditer(array.group("body"))]


def test_prepull_images_are_exactly_the_fixture_images() -> None:
    fixture_images = {
        _fixture_image(path, constant) for path, constant in _FIXTURE_CONSTANTS.items()
    }
    script_images = _script_images()

    # Non-empty first: two empty sets compare equal, which would make every assertion here
    # pass over nothing the moment a regex stopped matching.
    assert len(fixture_images) == len(_FIXTURE_CONSTANTS), (
        f"expected {len(_FIXTURE_CONSTANTS)} distinct fixture images, parsed {fixture_images}"
    )
    assert script_images, f"no images parsed from {_PULL_SCRIPT.relative_to(_ROOT)}"
    assert len(set(script_images)) == len(script_images), (
        f"{_PULL_SCRIPT.relative_to(_ROOT)} lists an image twice: {script_images}"
    )
    assert set(script_images) == fixture_images, (
        "the images CI pre-pulls and the images the test fixtures start have drifted, so the "
        "pre-pull step warms a tag the suite never requests and the registry cascade it exists "
        "to prevent is live again (ADR-0553, #1913). "
        f"Pre-pulled only: {sorted(set(script_images) - fixture_images)}; "
        f"started but never pre-pulled: {sorted(fixture_images - set(script_images))}. "
        f"Fixture constants: {_FIXTURE_CONSTANTS}"
    )


def test_every_suite_workflow_prepulls_before_it_runs_the_suite() -> None:
    # Per job, and every `just test` within it — not the first match in the file. Both
    # weaker forms pass while a suite-running job pre-pulls nothing.
    suite_jobs: dict[str, int] = {}
    for name in _SUITE_WORKFLOWS:
        jobs = _jobs(_WORKFLOWS / name)
        assert jobs, f"no jobs parsed out of {name} — the workflow layout changed (ADR-0553)"
        suite_jobs[name] = 0

        for job_name, job in jobs.items():
            runs = _job_run_steps(job)
            test_steps = [index for index, run in enumerate(runs) if run == _TEST_STEP]
            if not test_steps:
                continue
            suite_jobs[name] += 1
            prepull_steps = [index for index, run in enumerate(runs) if run == _PREPULL_STEP]

            assert len(prepull_steps) == len(test_steps), (
                f"{name} job `{job_name}` has {len(test_steps)} `run: just test` step(s) but "
                f"{len(prepull_steps)} `run: just pull-test-images` step(s) of its own. A "
                "pre-pull in a different job warms nothing for this one; without it an "
                "unreachable registry surfaces as thousands of downstream fixture errors "
                "instead of one red step (ADR-0553, #1913)."
            )
            for prepull, test in zip(prepull_steps, test_steps, strict=True):
                assert prepull < test, (
                    f"{name} job `{job_name}` runs `just pull-test-images` after `just test`, "
                    "which pre-pulls nothing: the fixtures have already tried the registry by "
                    "then (ADR-0553)."
                )

    # Per workflow, not in aggregate: a total would let one workflow lose its suite job while
    # another gained a second, and the loop above passes over nothing for a workflow with none.
    missing = sorted(name for name, count in suite_jobs.items() if count == 0)
    assert not missing, (
        f"no `run: just test` step found in {missing}, so this guard checks nothing there. "
        "If the suite moved to another recipe, re-point _TEST_STEP; if a workflow stopped "
        "running it, drop it from _SUITE_WORKFLOWS (ADR-0553)."
    )


def test_a_trailing_comment_does_not_hide_a_job_from_the_guard(tmp_path: Path) -> None:
    """The blind spot that motivated the real parser (#1993, sibling of PR #1992).

    A job key written with a trailing comment — `image-build:  # pulls the base layer` — is
    ordinary YAML but invisible to a regex that recognises a job only by `^  name:$`. Under
    the old splitter the plain key before it parsed fine and this one was silently absorbed
    into that job's text, so the ordering check saw one job where there were two and guarded
    only the wrong one; this test pins the parser to seeing both.
    """
    workflow = tmp_path / "commented.yml"
    workflow.write_text(
        "jobs:\n"
        "  lint:\n"
        "    steps:\n"
        "      - run: just lint\n"
        "  image-build:  # pulls the base layer\n"
        "    steps:\n"
        "      - run: just pull-test-images\n"
        "      - run: just test\n",
        encoding="utf-8",
    )

    jobs = _jobs(workflow)
    assert "image-build" in jobs, (
        "a job key carrying a trailing comment is invisible to the job splitter, so the "
        "ordering guard skips it entirely (#1993)"
    )
    assert _job_run_steps(jobs["image-build"]) == [_PREPULL_STEP, _TEST_STEP]
