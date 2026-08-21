# Ruff 0.16 reformat — implementation plan

Implements #2003 per ADR-0569 (Proposed) and
`docs/workflow/specs/2026-08-21-ruff-016-reformat-design.md`. Charter token
`scope-2003-9d11`.

## Goal

One dedicated, formatter-output-only change that pins `ruff==0.16.2`, aligns the pre-commit
`ruff-pre-commit` rev to v0.16.2, relocks `uv.lock`, and reformats the tree (~207 Markdown
files), so grouped Dependabot bumps carrying ruff pass `lint · type · test` again.

## Architecture

No code architecture: this is a toolchain-pin change plus mechanical formatter output. The
only structural fact is commit shape — the design-record files (ADR, spec, this plan) are
already committed separately; the `style:` commit carries exactly the formatter output plus
the two version pins and the lockfile.

## Tech stack

Python 3.14 / `uv`, `just` recipes, `ruff` 0.15.22 → 0.16.2, `ruff-pre-commit`
v0.15.15 → v0.16.2.

## Global Constraints

- Exact pins: `"ruff==0.16.2"` in `pyproject.toml` dev group; `rev: v0.16.2` for
  `astral-sh/ruff-pre-commit` in `.pre-commit-config.yaml`. No other dependency entry
  changes.
- Formatter only: run `uv run ruff format .` — **never** `just format` (its `ruff check
  --fix` half applies semantic lint autofixes the commit shape forbids).
- One `style:` commit carries all formatter output + both pins + `uv.lock`. Design-record
  files never enter it.
- Diff hunks carry formatter output only. On this tree that means Markdown ```` ```python ````
  fence normalization (comment spacing, blank lines); expected extension mix: all `.md`.
  Any identifier or literal change outside fences stops the ship.
- Guardrails (run from the worktree root): `just lint`, `just type`, `just test`,
  `uv lock --check`, `just check-mermaid`, `just adr-status-check`, `just docs-links`,
  `just docs-paths`, and finally `just ci` (the local aggregate mirroring CI's individually
  gated recipes).
- Worktree quirk: `.github/scripts/mermaid-check/node_modules` is gitignored; symlink it
  from the primary checkout before running `just check-mermaid`:
  `ln -sfn /home/dave/src/kdive/.github/scripts/mermaid-check/node_modules
  .github/scripts/mermaid-check/node_modules`.

## Task 1 — Bump both ruff pins and relock

Files: `pyproject.toml`, `.pre-commit-config.yaml`, `uv.lock`.

Steps:

1. In `pyproject.toml` dev group, replace `"ruff==0.15.22"` with `"ruff==0.16.2"` (one line,
   currently line 44).
2. In `.pre-commit-config.yaml`, replace `rev: v0.15.15` with `rev: v0.16.2` (line 3). Touch
   nothing else in the file.
3. Run `uv lock` to relock against the new pin (updates `uv.lock` only).
4. Verify:
   - `uv lock --check` → exits 0, no output.
   - `git grep -n 'ruff==' pyproject.toml` → exactly one match: `ruff==0.16.2`.
   - `git diff --stat` → exactly three files: `pyproject.toml`, `.pre-commit-config.yaml`,
     `uv.lock`.
   - `git diff .pre-commit-config.yaml` → exactly one changed line (`rev:`).

Acceptance criteria: pins bumped, lock consistent, diff limited to the three files. Do not
commit yet — Task 3 owns the commit boundary.

## Task 2 — Format the tree and verify the diff shape

Files: every file `uv run ruff format .` touches (expected ~207 `.md` files).

Steps:

1. Run `uv sync` (installs ruff 0.16.2 into the environment; confirms the lock resolves).
2. Run `uv run ruff check .` → expect clean exit 0. If any diagnostic appears, STOP: a new
   0.16 lint rule firing on this tree is a ship-stopper for disposition, not something to
   autofix into the style commit.
3. Run `uv run ruff format .` → rewrites the files.
4. Verify idempotence and shape:
   - `uv run ruff format --check .` → `2892+ files already formatted`, exit 0.
   - `git diff --name-only | sed 's/.*\.//' | sort | uniq -c` → only `md` (plus the three
     Task-1 files if listed by full name rather than extension).
   - `git diff -- '*.py'` → empty. No Python source changes.
   - Spot-check three hunks: `git diff -- docs/adr/0021-reconciler-loop-drift-repair.md`
     and two others of your choosing — comment-spacing and blank-line normalization inside
     ```` ```python ```` fences only.

Acceptance criteria: formatter idempotent, no `.py` diffs, hunks are fence-only
normalization.

## Task 3 — Commit once, run the gate

Steps:

1. Stage explicit paths only:
   `git add pyproject.toml .pre-commit-config.yaml uv.lock && git add -u '*.md'`
   then confirm `git status --short` shows nothing unexpected staged.
2. Commit: subject `style: adopt ruff 0.16 formatter output (ADR-0569)`; body noting #2003
   and that the diff is formatter output over ~207 Markdown files plus the two pins and the
   lockfile.
3. Re-run guardrails: `just lint` (expect: `ruff check .` silent, `ruff format --check .`
   green), `just type`, `just test`, `just check-mermaid`, `just docs-links`,
   `just docs-paths`.
4. Run the full aggregate: `just ci` → exit 0.

Acceptance criteria: one `style:` commit containing exactly the four kinds of content above;
`just ci` green.

## Rollback

Single-commit revert of the `style:` commit restores the tree and pins wholesale; `uv sync`
afterwards restores the old environment. No data, schema, or external state involved.
