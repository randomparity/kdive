# 0569 — Adopt the ruff 0.16 formatter output in one dedicated reformat

## Status

Accepted (2026-08-21)

## Context

`pyproject.toml` pins `ruff==0.15.22` in the dev group, and `just lint` gates PRs on
`ruff format --check .`. Ruff 0.16 changed formatter style — visibly, blank-line handling
inside docstring code fences — so any tree formatted under 0.15 fails the check under 0.16:
CI run 32313994208 on #1999's head reported `205 files would be reformatted, 2886 files
already formatted`, reproduced locally as 207.

Dependabot's grouped `python-dependencies` bump carries ruff with everything else on its
weekly cadence. Until the tree adopts the 0.16 output once, deliberately, every grouped bump
containing a ruff change reds the required `lint · type · test` check — #1999 closed
unmergeable for exactly this reason, and each following week repeats it.

## Decision

Adopt the ruff 0.16 formatter output in one dedicated, whitespace-only change: pin
`ruff==0.16.2` in the dev group, relock `uv.lock`, run `just format`, and commit the result
as a single mechanical `style:` commit verified by `just ci`. No functional changes mix in,
so review reduces to confirming the commit shape is exactly that.

## Consequences

- The diff is ~207 files of pure formatter output plus the pin and lockfile. Blame noise is
  real but concentrated in one commit, which `git log --follow` and blame-ignore tooling can
  skip wholesale.
- Future grouped Dependabot bumps carrying ruff 0.16.x patch/minor changes format clean and
  merge again; the weekly failure mode ends.
- Formatter style is now governed by 0.16.x output. The next formatter-style change repeats
  this pattern: one dedicated reformat, never folded into a functional change.
- The reformat touches files across every plane, so it must land alone or early in any stack;
  rebasing functional work over it is a conflict-only exercise best avoided by merging it
  promptly.

## Considered & rejected

- **Do nothing; keep 0.15.22.** Every weekly grouped bump containing ruff stays red, or
  ruff must be excluded from the group — permanent churn either way, and the linter stops
  receiving upstream fixes.
- **Adopt incrementally, per file as touched.** The tree stays mixed-style for months, every
  intervening weekly bump still reds `format --check`, and reviewers cannot distinguish
  mechanical from intentional formatting changes in ordinary PRs.
- **Suppress the guard (`--check` exemption or `# fmt: skip` sweeps).** Defeats the gate
  that keeps the tree formatted at all; a lint guard everyone opts out of is worse than the
  churn it was meant to avoid.
