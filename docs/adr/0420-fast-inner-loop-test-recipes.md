# ADR 0420 — Fast inner-loop test recipes (test-lf, test-changed)

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-21
- **Deciders:** D. Christensen (core platform)

## Context

The only test recipe is `just test` — the full ~8,800-test suite (`justfile:79-80`).
During an edit loop a developer wants feedback on just what a change touches, so today
they either rerun the whole suite or hand-craft a `pytest` invocation. There is no
`--lf`, `--stepwise`, or changed-files selection recipe alongside it (#1334).

The full suite stays the pre-push gate and the CI gate; this is purely an inner-loop
convenience. The design constraint is that a selection recipe must not give false
confidence: under-running (silently running zero relevant tests) is worse than
over-running, because a green `test-changed` that skipped the affected test reads as a
pass. So the mapping errs toward running more, and falls back to the full suite whenever
a change cannot be mapped.

## Decision

Add two additive recipes to the `justfile`. Neither changes `just test`, `just ci`, or CI.

### `test-lf` — rerun last failures first

`PYTHONHASHSEED="${PYTHONHASHSEED:-0}" uv run python -m pytest -m "not live_vm and not
live_stack and not agent_smoke" --lf -n auto -q`. `--lf` reruns the tests that failed on
the previous run (all tests when the cache is empty or nothing failed — pytest's
documented behavior). The same marker exclusion as `test:` keeps a stale-cache full run
from pulling in the gated live tiers on a host without a stack, and `PYTHONHASHSEED` is
pinned as in `test:` for collection stability.

### `test-changed` — run only what the diff touches

A stdlib-only helper, `scripts/select_changed_tests.py`, prints the pytest target list on
stdout, or the single sentinel line `__ALL__` when the change set is unmappable. The
recipe runs the printed targets, or falls back to the full-suite command on `__ALL__`, or
reports "nothing to run" on empty output.

The helper's selection contract (a pure function, `select_targets`, unit-tested with
injected inputs the way `schema_immutable_guard.find_violations` is):

- **Changed set** = files differing between the branch's merge-base with the base branch
  and the working tree (`git diff --name-only <merge-base>`, which covers both committed
  branch work and uncommitted edits), plus untracked files
  (`git ls-files --others --exclude-standard`). The base ref is `origin/<base>` when
  present, else `<base>`, else the diff is taken against `HEAD` alone. No fetch — the
  inner loop stays offline and fast.
- **A changed test file** (`tests/**/test_*.py` or `tests/**/*_test.py`) that still exists
  on disk is a direct target.
- **A changed `src/kdive/**/*.py` file** maps to every `tests/**/test_<stem>.py` on disk
  (basename glob). Zero, one, or many matches; all matches run. A stem with duplicate
  basenames across the tree (30 exist today, e.g. `test_control.py`) runs all of them —
  the intended over-running.
- **Zero matches for a changed src file, or any other changed path** (a non-`test_*` file
  under `tests/` such as a `conftest.py`, a non-Python file, `pyproject.toml`, the
  `justfile`, docs) makes the set unmappable → `__ALL__` → full suite.

**Known limitation, stated on purpose.** The map is by **name**, not by import graph.
A change to a widely-imported module maps only to its own `test_<stem>.py` (e.g.
`db/repositories.py` → `tests/db/test_repositories.py`), so `test-changed` runs that one
file, not every test that transitively imports it. `just test` remains the gate that
catches transitive breakage; `test-changed` is a pre-filter for fast local iteration, not
a substitute for it. Import-graph tracing (pytest-testmon) was rejected below.

## Alternatives considered

- **Strict path-mirror** (`src/kdive/a/b.py` → `tests/a/test_b.py`). Rejected: the tree is
  not a perfect mirror (`src/kdive/images/` ↔ `tests/image/` *and* `tests/images/`;
  `src/kdive/health/processes/` ↔ `tests/processes/`), so exact-path mapping misses real
  tests and risks running nothing — the false-confidence failure mode.
- **pytest-testmon** (coverage-map-based selection). Rejected: most precise, but adds a
  runtime dependency and a stored `.testmondata` map whose staleness silently changes
  selection — disproportionate to a P3 convenience, and opaque versus a name map a
  developer can predict.

## Consequences

- Two new recipes; one new stdlib-only script with unit tests under `tests/scripts/`.
- No change to the pre-push gate or CI. `test-changed` is a heuristic pre-filter whose
  contract and known limitation are documented so a green run is not mistaken for the gate.
