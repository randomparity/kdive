# Ruff 0.16 formatter adoption — design

Implements #2003 under ADR-0569. Charter: `WORK:SCOPE` token `scope-2003-7c42` on the issue.

## Outcome

The tree formats clean under ruff 0.16.2: the dev-group pin moves 0.15.22 → 0.16.2,
`uv.lock` is relocked, and one mechanical `style:` commit carries the reformatted files —
so grouped Dependabot `python-dependencies` bumps containing ruff pass `lint · type · test`
again.

## Requirements (trace to issue acceptance criteria)

- **R1 (pin)** — `pyproject.toml` dev group pins `"ruff==0.16.2"`. No other dependency
  entry changes.
- **R2 (lock)** — `uv.lock` regenerated against the new pin; `uv lock --check` green.
- **R3 (format)** — `just format` applied repo-wide under 0.16.2; afterwards `just lint`
  green (`ruff check .` reports nothing, `ruff format --check .` exits 0).
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

Mechanical, in order: bump the pin → `uv sync` (relocks and installs 0.16.2) → `just
format` → inspect the diff shape → commit once → `just ci`. The only judgment call in the
whole change is the diff inspection in §Verification; everything else is prescribed.

## Verification

- `uv lock --check` exits 0 (R2).
- `git grep -n 'ruff==' pyproject.toml` shows exactly `ruff==0.16.2` (R1).
- Diff-shape check for R4: `git diff main --stat` lists only `.py`/docs/config files the
  formatter owns plus `pyproject.toml` and `uv.lock`; spot-check `git diff main -- '*.py'`
  hunks contain only whitespace/quote/string-prefix normalization, no identifier or literal
  changes. A hunk showing anything else stops the ship.
- `just ci` exits 0 (R3, R5).

## Threat model

Dependency-change trigger, scoped honestly: this is a dev-tooling bump; no runtime surface
changes.

- **Boundary inventory** — one: the supply-chain edge where `uv sync` fetches ruff 0.16.2
  from PyPI. Nothing is added to what the shipped service can reach; ruff does not ship in
  any artifact.
- **Actor model** — an attacker controlling PyPI package content (compromised release).
  Trust placement: PyPI plus uv's lockfile hash verification, the same trust the repo
  already extends to every other locked dependency.
- **Control per boundary** — `uv.lock` pins the exact version and hashes; `uv sync
  --locked` (what CI runs) refuses a mismatched artifact. No new control needed; the
  existing lockfile discipline covers the one boundary.
- **Out of scope** — runtime dependency risk (none touched), CI action references
  (untouched), secrets (none involved).

## Testing

No new tests: the change has no observable contract beyond "the gate passes", and R3–R5 are
verified by running the gate itself. The suite existing unchanged is the point — a test
added for a whitespace reformat would assert plumbing, not behavior.
