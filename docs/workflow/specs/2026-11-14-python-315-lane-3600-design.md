# Python 3.15 informational CI lane (#3600) — design

## Problem

kdive is 3.14-only by contract (`requires-python = "==3.14.*"`), and nothing runs it under
CPython 3.15. The only signal today is a dependency bump failing on its contract tests:
Dependabot PR #2096 (`python:3.15.0-slim-bookworm`) is red on the 3.14 contract itself, which
says nothing about whether 3.15 works or what blocks adopting it.

## Goal

A separate, non-gating workflow resolves the project for CPython 3.15 with the fewest version
changes it can, installs it, and runs `ruff`/`ty` probes at the 3.15 target and the ordinary
suite. It reports each probe's outcome, the forced version bumps, and the packages that had to
build from source, in its job summary. The answer is for linux/x86_64 only;
ppc64le, where cp315 wheel coverage is thinner, is not covered. The gating 3.14 lane, release images, and the
repository's pins stay untouched.

Decision record: [ADR-0817](../../adr/0817-python-315-informational-lane.md).

## Design

**Where it runs.** A new workflow, `.github/workflows/python-315.yml`, one job
(`python 3.15 (informational)`), on `ubuntu-latest`. Triggers:

- `pull_request` filtered to the files that decide the answer: `pyproject.toml`, `uv.lock`,
  `Dockerfile`, `tests/container/Dockerfile`, and the workflow itself — so a dependency or
  base-image bump such as #2096 gets the signal, and an unrelated PR pays nothing;
- `schedule`, weekly, so `main` keeps a current answer as upstream publishes cp315 wheels;
- `workflow_dispatch`.

No `push` trigger. `permissions: contents: read`. Its own `concurrency` group with
`cancel-in-progress`. It is in no `needs:` of `ci.yml`, so the required `lint · type · test`
check cannot see it.

**Interpreter.** setup-uv at uv `0.13.8` (the repository pins `0.11.18`, whose download index
offers only `3.15.0b1`; `0.13.x` offers `3.15.0`–`3.15.2`). `UV_PYTHON: "3.15"` is set at job
level; uv downloads the managed interpreter. No `actions/setup-python`.

**Override (the only place the 3.14 contract is relaxed).** A step edits the job's own
checkout of `pyproject.toml`, never a committed file:

1. `requires-python = "==3.14.*"` → `">=3.14"`;
2. every list-entry pin `  "name==version",` → `  "name>=version",` (runtime, `dev`, `live`);
3. append `[tool.uv] required-environments` for `linux`/`x86_64`/Python `3.15`.

Without (2) and (3) the resolver keeps psycopg-binary 3.3.4, which has neither a cp315 wheel
nor an sdist, and the job dies at install with no test signal. With them, `uv lock` keeps every
locked version as a preference and moves only what has no installable cp315 artifact — the
minimal bump set. The step first asserts the `requires-python` line is present exactly and
fails otherwise, so a drifted `pyproject.toml` cannot silently test the 3.14 contract.

**Probes.** Each is `continue-on-error: true` with an `id`; later probes run only when the
install succeeded (`if: steps.sync.outcome == 'success'`), so a failed install reports the
rest as skipped rather than as misleading failures:

| id | command |
|---|---|
| `lock` | `uv lock` (log retained) |
| `sync` | `uv sync --locked` (log retained) |
| `ruff` | `uv run --no-sync ruff check --target-version py315` |
| `ty` | `uv run --no-sync ty check --python-version 3.15` |
| `test` | `just pull-test-images && just test`, with `KDIVE_REQUIRE_DOCKER=1` |

`just test` is the gate's own selection; pytest's warnings summary surfaces 3.15
deprecations. apt installs `libvirt-dev libffi-dev` through `scripts/apt-install.sh` (cffi
2.0.0 has no cp315 wheel and builds from sdist). setup-uv caching is on with a `py315`
suffix so source-built wheels (pydantic-core, grpcio) are reused across runs.

**Report.** A final `if: always()` step writes a Markdown table of the five outcomes, the
`uv lock` `Updated`/`Added`/`Removed` lines (forced bumps), and the `uv sync` `Built` lines
(packages without a cp315 wheel at the locked version) to `$GITHUB_STEP_SUMMARY`, and emits one
`::warning::` per non-success probe. It always exits 0. The job is therefore green whenever
its infrastructure steps work; its findings live in the summary and warnings.

**Cut.** No container smoke: `tests/container/Dockerfile` installs no Python dependencies (its
entrypoint syncs at run time), so a 3.15 base there proves nothing the `sync` probe does not,
and the root `Dockerfile` repeats the in-image `requires-python` contract the override exists
to bypass. No change to `Dockerfile`s, `requires-python`, or ruff/ty targets (exclusions).

## Failure model

1. **Actors and deployments** — GitHub Actions on `ubuntu-latest` x86_64, triggered by a
   same-repo or Dependabot PR touching the path filter, the weekly schedule, or a maintainer's
   dispatch. Fork PRs run with the read-only token like `ci.yml`.
2. **Invariants and assets at stake** — the required check's verdict and the 3.14 contract
   (`pyproject.toml`, `uv.lock`, images) on `main`; runner minutes.
3. **Accepted failure classes**
   - Contract tests that read `pyproject.toml`/`uv.lock` (the relaxed copy) fail by design; the
     findings classify them, they do not gate.
   - An infrastructure step (checkout, apt, setup-uv, the override) failing turns the job red:
     a non-required check, and the override's drift is caught earlier by the gating guard below.
   - Source builds (grpcio, pydantic-core) cost minutes per uncached run: bounded by the path
     filter, the weekly cadence, the cache, and a 60-minute timeout.
   - Upstream index churn changes the answer between runs: that is the signal.
4. **Covered elsewhere** — action pin consistency (`tests/guards/test_workflow_action_pins.py`),
   apt bounding (`tests/guards/test_apt_install_is_bounded.py`), workflow lint
   (`just lint-workflows`), the required check's shape (`tests/guards/test_ci_job_graph.py`).

## Validation

A new guard, `tests/guards/test_python_315_workflow.py`, in the gating suite:

- **Override applies to today's `pyproject.toml`**: run the override step's `run` block on a
  temp copy; `tomllib` reads `requires-python == ">=3.14"`, no `==` among
  `project.dependencies` and dependency groups, and the `required-environments` entry.
- **Override fails loud on drift**: on a copy whose `requires-python` differs, the block exits
  non-zero and leaves the file unchanged.
- **Non-gating and isolated**: the workflow has no `push` trigger and read-only permissions;
  each probe step (`lock`, `sync`, `ruff`, `ty`, `test`) is
  `continue-on-error: true`; the report step runs `always()`;
  `ci.yml` sets no `UV_PYTHON`, mentions no `3.15`, and its required check's `needs` are
  unchanged.
- **Report never fails and names failures**: run the report block with fake outcomes and logs;
  it exits 0, writes every outcome row, the `Updated`/`Built` lines, and one warning per
  non-success.

Plus `just lint-workflows`, and the first real run of the lane on this PR, whose findings the
PR body records.

Docs: a "Python 3.15 lane" section in `docs/development/cross-platform.md` (what it runs, when,
how to read the summary, that it never gates).
