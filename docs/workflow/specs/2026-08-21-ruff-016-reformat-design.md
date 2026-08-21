# Ruff 0.16 formatter adoption — design

Implements #2003 under ADR-0569. Charter: `WORK:SCOPE` token `scope-2003-9d11`
(supersedes `scope-2003-7c42`; see the superseding WORK:SCOPE block on the issue).

## Outcome

The tree formats clean under ruff 0.16.2: the dev-group pin moves 0.15.22 → 0.16.2,
`uv.lock` is relocked, and one mechanical `style:` commit carries the reformatted files —
so grouped Dependabot `python-dependencies` bumps containing ruff pass `lint · type · test`
again.

## Requirements (trace to issue acceptance criteria)

- **R1 (pins)** — `pyproject.toml` dev group pins `"ruff==0.16.2"` and
  `.pre-commit-config.yaml` moves the `ruff-pre-commit` `rev:` v0.15.15 → v0.16.2
  (operator decision, 2026-08-21: the local `ruff-format` hook at 0.15 would otherwise
  revert adopted files to 0.15 style). No other dependency entry changes.
- **R2 (lock)** — `uv.lock` regenerated against the new pin; `uv lock --check` green.
- **R3 (format)** — `uv run ruff format .` applied repo-wide under 0.16.2 — the formatter
  alone, not `just format`, whose `ruff check --fix` half applies semantic lint autofixes
  the commit shape forbids; afterwards `just lint` green (`ruff check .` reports nothing,
  `ruff format --check .` exits 0). Empirically (verified with `uvx ruff@0.16.2`) the
  reformat set is ~207 Markdown files — 0.16 formats Python code blocks in `.md` fences by
  default — with no `.py` file in it; `docs/adr` is subsequently excluded from the formatter
  (operator decision 2026-08-21: the records gate forbids line loss in merged records), so
  the landed set is ~199 files. The spec's steps do not depend on the mix staying true,
  but the diff inspection below is where it gets re-checked.
- **R4 (commit shape)** — all reformatted files land in a single `style:` commit together
  with the pin and lockfile change; `git diff` against `main` contains no semantic edits.
  Verified mechanically: the diff touches only formatting (see §Verification) and the full
  gate passes.
- **R5 (gate)** — `just ci` green before the branch ships.

## Non-goals

- No lint-rule configuration changes (`[tool.ruff]` sections untouched beyond what the pin
  bump implies — there are none).
- No other dependency bumps; those stay with Dependabot's group flow.
- No functional code changes; any file where `ruff format` would change semantics is a bug
  to investigate, not to hand-patch (ruff's formatter is semantics-preserving by contract).

## Approach
Mechanical, in order: bump the pin → bump the pre-commit rev → `uv sync` (relocks and
installs 0.16.2) → `uv run ruff format .` → inspect the diff shape → commit once →
`just ci`. The only judgment call in the whole change is the diff inspection in
§Verification; everything else is prescribed.

## Verification

- `uv lock --check` exits 0 (R2).
- `git grep -n 'ruff==' pyproject.toml` shows exactly `ruff==0.16.2` (R1).
- Diff-shape check for R4, scoped to the `style:` commit alone (the design-record files —
  ADR, spec, plan — land in their own commits outside this check): `git diff --name-only`
  for that commit lists only files the formatter owns plus
  `pyproject.toml`, `uv.lock`, and the `.pre-commit-config.yaml` `rev:` line; check the
  extension mix against the fresh `ruff format --check` run (currently: all `.md`), and
  spot-check hunks carry formatter output only — inside Markdown ```python fences:
  comment-spacing and blank-line normalization (served-docs content edits, expected and
  backed by the `just ci` test run). No identifier or other literal changes. Anything else
  stops the ship.
- `.pre-commit-config.yaml` diff is exactly the `rev:` line, now `rev: v0.16.2` (R1);
  `prek validate-config` or `prek run --help` unaffected — no hook id or arg changes.
- `uv run ruff check .` exits 0 under 0.16.2 before merge — the headline benefit (weekly
  bumps merging) depends on the check half of `just lint`, not only the format half; a new
  0.16 diagnostic against this tree stops the ship for disposition, not a hand-patch inside
  the style commit.
- `just ci` exits 0 (R3, R5).

## Threat model

Dependency-change trigger, scoped honestly: this is a dev-tooling bump; no runtime surface
changes.

- **Boundary inventory** — three edges: `uv sync` fetching ruff 0.16.2 from
  PyPI; prek fetching the `ruff-pre-commit` v0.16.2 hook repo from GitHub; and the
  secrets-scanning guardrail — the reformat splits inline `# pragma: allowlist secret`
  comments off their secret lines in doc fences, so `.secrets.baseline` gains three audited
  false-positive entries (example credentials in plan docs, recorded decision #2005).
  Nothing is
  added to what the shipped service can reach; neither ruff copy ships in any artifact.
- **Actor model** — an attacker controlling PyPI package content or the hook repo's tag.
  Trust placement: PyPI plus uv's lockfile hash verification for the first edge; for the
  second, GitHub tag integrity on `astral-sh/ruff-pre-commit` — the same trust every other
  pre-commit hook in this repo already extends, unchanged by moving the rev.

- **Control per boundary** — `uv.lock` pins the exact version and hashes; `uv sync
  --locked` (what CI runs) refuses a mismatched artifact. The hook edge is controlled by
  the pinned `rev:` tag on a first-party (astral-sh) repository; no new control added.
- **Out of scope** — runtime dependency risk (none touched), CI action references
  (untouched). Secrets are involved only as scanner false positives in docs (above); no
  runtime secret path changes.

## Testing

No new tests: the change has no observable contract beyond "the gate passes", and R3–R5 are
verified by running the gate itself. The suite existing unchanged is the point — a test
added for a whitespace reformat would assert plumbing, not behavior.
