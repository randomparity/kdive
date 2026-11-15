# 0817 — Evaluate CPython 3.15 in a separate, minimally re-resolved, non-gating lane

## Status

Accepted (2026-11-14)

## Context

kdive pins `requires-python = "==3.14.*"`, and CI, the release images and the live hosts all
assume 3.14. Nothing tells the project whether 3.15 works or what blocks adopting it; the only
signal is a bump such as Dependabot #2096 failing on the 3.14 contract itself (#3600). Three
facts shape any lane. The repository's uv `0.11.18` offers only `3.15.0b1` for download, while
uv `0.13.x` offers `3.15.0`–`3.15.2`. With the pins as they are, `uv sync` on 3.15 dies on
psycopg-binary 3.3.4, which has neither a cp315 wheel nor an sdist, so a pins-only lane reports
one install error and no test result. And unconstrained re-resolution moves fastmcp, mcp and
protobuf across major versions, so its failures would not be 3.15's.

## Decision

A separate workflow, `.github/workflows/python-315.yml`, runs one job on `pull_request` paths
that decide the answer (`pyproject.toml`, `uv.lock`, the Dockerfiles, itself), weekly, and on
dispatch. It never runs on `push` and no `ci.yml` job depends on it.

Within its own checkout only, the job relaxes `requires-python` to `>=3.14` and every `==`
list pin to `>=`, and appends `[tool.uv] required-environments` for linux/x86_64/3.15. `uv
lock` then keeps every locked version as a preference and moves only packages with no
installable cp315 artifact — the minimal bump set. The job installs with uv `0.13.6` and runs
`lock`, `sync`, `ruff --target-version py315`, `ty --python-version 3.15` and `just test` as
`continue-on-error` probes. An always-green report step writes their outcomes, the forced bumps
and the source-built packages to the job summary and warns per failure.

## Consequences

- The required check, the committed pins, `uv.lock` and the images are unchanged; the lane
  answers "what would adopting 3.15 take on linux/x86_64" without touching what ships.
- Tests that read `pyproject.toml` or `uv.lock` fail in this lane by design, because they read
  the relaxed copy. Its findings must separate those from genuine 3.15 failures.
- The job is green whenever its infrastructure works, so a reader must open its summary; a red
  run means the lane itself broke.
- Packages lacking cp315 wheels with an sdist (cffi, pydantic-core, grpcio at today's locks)
  build from source, costing minutes on every run: the lane keeps no uv cache, and the trigger
  set bounds the cost.
- Relaxed `>=` pins let `uv lock` take releases nobody reviewed for packages lacking cp315
  artifacts, and their sdists are built and executed on the runner. The exposure is a
  compromised upstream release reaching a runner that holds a `contents: read` token and no
  secrets, in a job no required check depends on. `uv lock --exclude-newer` holds the lane to
  the 7-day cooldown `.github/dependabot.yml` applies. `enable-cache: false` stops only
  setup-uv's own save: code on the runner can still reach the Actions cache service, and
  schedule and dispatch runs share the default branch's cache scope, which `ci.yml` restores.
- The lane uses a newer uv than the rest of CI. Nothing it produces is committed, so the skew
  cannot reach a lockfile.

## Considered & rejected

- **A matrix leg in `ci.yml`'s `tests` job.** judgment: it would share the required check's
  workflow, concurrency group and guard, and run on every PR; a separate path-filtered workflow
  cannot reach the gate by construction.
- **Pins-only resolution (relax `requires-python` alone).** verified: `uv sync --locked` under
  CPython 3.15.2 with uv 0.13.7 exits on `psycopg-binary==3.3.4` "doesn't have a source
  distribution or wheel for the current platform" (kdive d1bb5e642); no test runs.
- **`uv lock --upgrade` of the relaxed pins.** verified: the same setup reports
  `fastmcp-slim v3.4.4 -> v4.2.0`, `mcp v1.30.0 -> v2.3.0`, `protobuf v6.33.6 -> v7.36.2`
  among 60+ updates, so a failure could not be attributed to the interpreter.
- **`actions/setup-python` for the interpreter.** judgment: a new action pin for what uv
  already downloads once its version is new enough.
- **Job-level `continue-on-error` alone.** judgment: it governs only the run's conclusion; with
  step-level probes one failing probe does not stop the others, and the report names each.
- **A container smoke building `tests/container/Dockerfile` on a 3.15 base.** verified:
  that image installs no Python dependencies (`tests/container/run-suite.sh` syncs at run
  time), so it proves nothing the `sync` probe does not; the root `Dockerfile` re-enforces the
  pin the lane exists to bypass.
- **Do nothing; read Dependabot failures.** verified: #2096 fails only on 3.14-contract tests
  and the image smoke, which say nothing about 3.15 itself (#3600).
