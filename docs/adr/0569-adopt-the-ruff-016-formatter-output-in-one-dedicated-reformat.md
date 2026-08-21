# 0569 — Adopt the ruff 0.16 formatter output in one dedicated reformat

## Status

Accepted (2026-08-21)

## Context

`pyproject.toml` pins `ruff==0.15.22` in the dev group, and `just lint` gates PRs on
`ruff format --check .`. Ruff 0.16 changed formatter style, and 0.16 also formats Python
code blocks in Markdown files by default. On this tree the visible delta is exactly that
Markdown dimension: running 0.16.2's formatter reports ~207 files would be reformatted, and
every one of them is `.md` — comment spacing and blank-line normalization inside
```python fences (the issue's "blank-line handling inside docstring code fences"). CI run
32313994208 on #1999's head reported `205 files would be reformatted, 2886 files already
formatted`; a local reproduction counted 207. The two-count delta was never reconciled (the
runs saw different trees); this change's own verification run supersedes both numbers.

Dependabot's grouped `python-dependencies` bump carries ruff with everything else on its
weekly cadence. Until the tree adopts the 0.16 output once, deliberately, every grouped bump
containing a ruff change reds the required `lint · type · test` check — #1999 closed
unmergeable for exactly this reason, and each following week repeats it.

## Decision

Adopt the ruff 0.16 formatter output in one dedicated, formatter-output-only change: pin
`ruff==0.16.2` in the dev group, relock `uv.lock`, run `uv run ruff format .` — the
formatter alone, deliberately not `just format`, whose `ruff check --fix` half applies
semantic lint autofixes — and commit the result as a single mechanical `style:` commit
verified by `just ci`. No functional changes mix in, so review reduces to confirming the
commit shape is exactly that — mechanically: `uv run ruff format --check .` green
(idempotence), and `git diff --name-only` against the parent commit listing only files the
formatter touches plus `pyproject.toml`, `uv.lock`, and the `.pre-commit-config.yaml`
`rev:` line, with hunks carrying formatter output only. Formatter output here is not
strictly whitespace: inside Markdown ```python fences it normalizes comment spacing and
blank lines — edits to served-docs content, verified against the actual 0.16.2 run before
merge. The test run in `just ci` remains the behavioral backstop; no Python source file is
in the reformat set.

## Consequences

- The diff is ~207 files of pure formatter output plus the pin and lockfile — all Markdown;
  blame noise is
  real but concentrated in one commit; adding that commit to a `.git-blame-ignore-revs`
  file is a post-merge follow-up tracked as #2004 — the file cannot name its own commit.
- Future grouped Dependabot bumps carrying ruff 0.16.x patch/minor changes format clean and
  merge again, provided `ruff check .` stays clean under 0.16.2 — verified before this
  change merges, since `just lint` gates on the check before the format check. The weekly
  format-check failure mode ends; a future ruff version promoting new lint rules can still
  red a grouped bump via `ruff check`, which is lint-rule drift and outside this decision.
- The repo's second ruff installation — the `ruff-pre-commit` hooks in
  `.pre-commit-config.yaml`, invisible to Dependabot's ecosystems — moves v0.15.15 →
  v0.16.2 in the same change, so the local `ruff-format` hook cannot revert adopted files
  to 0.15 style and the hook and the gate run one version.
- Residual file-set skew, accepted: the pre-commit `ruff-format` hook filters to
  python/pyi/jupyter, so it never formats `.md` — docs-only PRs rely on `just format` and
  CI to keep fences formatted. And the manually bumped rev drifts again at the next ruff
  bump in `pyproject.toml`, noticed only when a local hook disagrees with the gate; accepted
  because no Dependabot ecosystem covers pre-commit revs and a watcher would cost more than
  the residual.
- In-flight branches formatted under 0.15 red `format --check` once each until they rebase
  and reformat; only Dependabot's weekly cadence stops churning.
- Formatter style is now governed by 0.16.x output. The next formatter-style change repeats
  this pattern: one dedicated reformat, never folded into a functional change.
- The reformat touches ~207 Markdown docs files, so it must land alone or early in any
  stack; rebasing functional work over it is a conflict-only exercise best avoided by
  merging it promptly.

## Considered & rejected

- **Exclude ruff from the Dependabot group.** All other weekly bumps stay green immediately
  and no large mechanical diff ever lands, but it costs a standing config exception, a
  linter stale between dedicated ruff PRs, and two mechanisms managing one dependency —
  each ruff PR still carries its own small reformat. One deliberate reformat beats a
  permanent exception.
- **Do nothing; keep 0.15.22.** Every weekly grouped bump containing ruff stays red, and
  the linter stops receiving upstream fixes.
- **Adopt incrementally, per file as touched.** The tree stays mixed-style for months, every
  intervening weekly bump still reds `format --check`, and reviewers cannot distinguish
  mechanical from intentional formatting changes in ordinary PRs.
- **Suppress the guard (`--check` exemption or `# fmt: skip` sweeps).** Defeats the gate
  that keeps the tree formatted at all; a lint guard everyone opts out of is worse than the
  churn it was meant to avoid.
- **Fold the reformat into the grouped bump PR (#1999).** Reaches the same end state with
  no new branch, but shares one diff with ten unrelated version bumps and loses the
  mechanical shape check that makes review trivial; the dedicated commit keeps review
  mechanical.
