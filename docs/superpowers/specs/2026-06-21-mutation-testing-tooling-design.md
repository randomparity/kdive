# On-demand mutation testing tooling — `just mutate`

- **Type:** developer tooling · **Tool:** [mutmut 3.6.0](https://mutmut.readthedocs.io/) · **No CI gate, no new locked dependency.**

## Goal

Give developers a `just mutate <source-module> <test-path>` recipe that runs mutation testing
against one module at a time, reports surviving mutants (places where a behavioral change to the
code would not make any test fail), and is fast enough to use iteratively while strengthening a
test file. This validates that the ~4,859-test suite actually catches regressions in the code it
covers, not merely that lines execute.

Both arguments are **required**. The test path is not inferred from the source path: this repo's
test tree does **not** mirror the source tree (e.g. `tests/domain/` holds flat `test_*.py` files,
not a `tests/domain/capacity/` subdir, and the tests exercising
`src/kdive/domain/capacity/state.py` live under `tests/services/allocation/` and
`tests/services/systems/`). A path-convention guess would silently mis-target and fall back to the
full Postgres-backed suite, so the developer states the covering test path explicitly. The doc
records the right test path for each default target.

This is **repeatable local tooling**, not a CI gate and not a whole-tree campaign. The recipe
takes any module; the documented default targets are the safety-critical cross-cutting invariants
and the domain layer.

## Why mutation testing here

Line/branch coverage shows a line *ran* during tests. Mutation testing shows whether the tests
would *fail* if that line were wrong. A suite can have high coverage and still let a mutant such as
`x > 0` → `x >= 0` survive. Surviving mutants point directly at weak or missing assertions.

The suite is large (~71k LOC source, 500 modules, ~5,452 non-live tests), so the whole game is
scope and test selection. mutmut 3's execution model fits this: it runs the scoped suite once under
coverage.py to build a mutant→test map, then for each mutant runs only the tests that cover the
mutated line.

## Tool choice

mutmut **3.6.0** (released 2026-06-06):

- Python 3.14 support landed in 3.4.0; the project runs on 3.14.
- Mutant→test mapping via coverage.py (`mutate_only_covered_lines`) is what makes per-mutant runs
  tractable on this suite.
- Requires `os.fork` — fine on the dev box (darwin) and Linux CI; native Windows would need WSL.
- Host prerequisite: mutmut depends on `libcst`, which has no prebuilt wheel on some architectures
  (mutmut's docs call out `x86_64-darwin`) and then needs the Rust toolchain (`rustc`/`cargo`) to
  build. The doc and `scripts/check-setup-deps.sh` must note this so first-run installs don't fail
  cryptically.
- Run ephemerally via `uv run --with 'mutmut==3.6.0'`, matching the repo's existing convention for
  occasional tools (zizmor, actionlint, git-cliff via `uv run --with` / `uvx`). mutmut depends on
  `coverage`, `pytest`, `libcst`, and `textual`, so no extra `--with` is needed. **Not** added to
  the locked dev dependency group.

cosmic-ray (heavier, session/DB-based, distributed) was rejected as overkill for on-demand local
runs. Static `[tool.mutmut]` config (edit-to-retarget) was rejected because it cannot deliver the
`just mutate <module>` ergonomics and forces every run through the full Postgres-backed suite.

## Default targets

The recipe accepts any module under `src/kdive/`. The documented starting targets are chosen for
high blast radius, with the **covering test path stated explicitly** (no inference):

| target module | covering tests (explicit arg) |
|---|---|
| `src/kdive/domain/capacity/state.py` | `tests/services/allocation tests/services/systems` |
| `src/kdive/security/authz/` | `tests/security/authz` |
| `src/kdive/security/secrets/redaction.py` | `tests/security` |
| `src/kdive/domain/` | `tests/domain` |

These paths are illustrative and **must be confirmed during implementation** by locating the tests
that actually exercise each module (the suite layout does not mirror `src/`).

**Postgres caveat — these targets are not all Postgres-free.** `tests/domain/conftest.py` and
`tests/security/conftest.py` import `migrated_url`/`pg_conn`/`postgres_url` from
`tests/db/conftest.py`, so any selected test that requests those fixtures starts a disposable
Postgres container per run. The earlier "pure logic, no Postgres" framing was wrong. The doc states
the real per-target cost; truly container-free targets are a narrower set (e.g. `domain/errors.py`,
`domain/` cost math) that the implementation identifies and labels.

## Components

- **`scripts/mutate.py`** (~120 lines) — the wrapper, following the existing `scripts/*.py`
  guard-script convention. Responsibilities:
  1. Require both args; validate the source path exists and is under `src/kdive/`, and that the
     test path(s) exist.
  2. Write a transient `setup.cfg` `[mutmut]` section.
  3. **Green-baseline pre-flight:** run the scoped test selection once on unmutated code and abort
     with a clear error if it errors, fails, or collects zero tests. Mutation results are only
     meaningful when the baseline suite passes; a broken import or a wrong test path otherwise
     surfaces as bogus "killed" mutants.
  4. Invoke mutmut via `uv run --with 'mutmut==3.6.0'`.
  5. Parse `mutmut results` and print a survivor summary.
  6. Always remove the transient config (`finally`).
- **`just mutate` recipe** — thin pass-through: `uv run python scripts/mutate.py "$@"`.
- **`.gitignore` additions** — `mutants/` (mutmut's working/cache directory) and the transient
  `setup.cfg`.
- **`docs/development/mutation-testing.md`** — usage, default targets with their explicit test
  paths, the per-target Postgres cost, the libcst/Rust host prerequisite, and how to read and act
  on survivors.

## Data flow (one invocation)

1. Developer runs e.g.
   `just mutate src/kdive/domain/capacity/state.py "tests/services/allocation tests/services/systems"`.
   Both args are required; the test path is taken verbatim (no inference, no full-suite fallback).
2. Wrapper writes a transient `setup.cfg` `[mutmut]` with:
   - `source_paths` = the target module;
   - `pytest_add_cli_args_test_selection` = `-m "not live_vm and not live_stack"` plus the given
     test path(s);
   - `mutate_only_covered_lines = true`;
   - `also_copy = ["pyproject.toml", "tests"]` — mutmut runs the suite from an isolated copy under
     `mutants/`, so it needs pyproject (pytest's `pythonpath`/markers) **and the whole `tests/`
     tree**, because conftests import across packages (`tests/domain/conftest.py` and
     `tests/security/conftest.py` both do `from tests.db.conftest import …`). Copying only the
     scoped subtree would break collection. The exact minimal closure is confirmed empirically in
     the plan; `tests/` is the safe default.
   - `do_not_mutate_patterns` = `logger.\w+` only (skip log-string mutants). **`raise \w+` is
     deliberately *not* suppressed:** the security/state-guard targets are dominated by
     `raise IllegalTransition(...)` / denial raises, and "removed/weakened raise" (deny→allow,
     illegal-edge-allowed) is exactly the mutation those tests must catch.
   - `max_stack_depth = 8` (keep relevant tests localized).
3. **Green-baseline pre-flight** (see Components step 3): run the scoped selection unmutated; abort
   if it errors, fails, or collects zero tests.
4. mutmut: one coverage stats pass over the scoped tests → mutant→test map → run each mutant
   against only its covering tests.
5. Wrapper runs `mutmut results` and prints counts (killed / survived / timeout / suspicious) plus
   the survivor list with `file:line`, and the hint to inspect via `mutmut show <id>` /
   `mutmut browse`. The summary states that the score is **relative to the test path supplied** —
   a narrow path can flatter the result.
6. `finally`: remove the transient `setup.cfg`.

## Error handling

- **Refuse to clobber a pre-existing `setup.cfg`.** The repo has none today; if one appears, fail
  with a clear message rather than overwrite it.
- **Validate both paths.** Source exists and is under `src/kdive/`; every test path exists. Missing
  test path → clear error, not a silent broadening to the full suite.
- **Broken baseline / zero tests collected** → abort before mutating (Components step 3), so invalid
  runs can never masquerade as a clean result.
- **In-flight / stale transient config.** The generated `setup.cfg` carries a marker header. If a
  marked config already exists on start, the wrapper **refuses to run** and tells the user another
  run is in flight (or to delete the stale file if none is). It never auto-deletes — that would nuke
  a concurrent run's config. This single rule covers both the concurrency clash and a hard-kill
  leftover; `.gitignore` is the backstop against committing it.
- **Survivors are data, not a crash.** A non-zero mutmut exit (survivors exist) is surfaced as the
  summary, not a stack trace.

## Testing

- `tests/test_mutate_script.py` — fast, Postgres-free unit tests of the wrapper's decision logic:
  required-argument enforcement, source-path validation (must exist, under `src/kdive/`), test-path
  validation (must exist), transient-config rendering (correct `source_paths`, test selection,
  `also_copy`, `do_not_mutate_patterns` *without* `raise`), and the in-flight-config refusal. The
  green-baseline pre-flight is tested by stubbing the subprocess to simulate pass / fail / zero-
  collected and asserting the wrapper aborts on the latter two. Tests behavior, not implementation.
- mutmut itself is **not** run inside the test suite (too slow, needs the full environment). The
  wrapper's decisions are unit-tested; mutmut is exercised manually against the documented targets.

## Out of scope (YAGNI)

No CI gate, no mutation-score threshold, no whole-tree campaign, no written report artifact. The
terminal summary plus `mutmut browse` are sufficient; a report or CI gate can be added later if a
campaign is wanted.

## Assumptions

- Doc lives at `docs/development/mutation-testing.md`, alongside `releasing.md`.
- Both arguments are required; the test path is always explicit (no inference, no full-suite
  fallback). This is the safe, predictable choice given the non-mirroring test layout.

## Open spikes (validate as the first plan step, before writing the wrapper)

These are mechanics the design depends on but that need a quick empirical check rather than more
design:

1. **`source_paths` as a single file.** mutmut's docs only show directory `source_paths`. Confirm
   `just mutate …/state.py` mutates just that file; if mutmut requires a directory, the wrapper
   passes the parent dir plus `only_mutate` scoped to the file.
2. **mutmut's working-copy scope.** Confirm `also_copy = ["pyproject.toml", "tests"]` makes the
   cross-package conftest imports resolve in the `mutants/` copy, and trim to the real minimal
   closure if `tests/` is excessive.
3. **`libcst` install on the dev arch.** Confirm `uv run --with 'mutmut==3.6.0'` resolves without a
   Rust toolchain on the target darwin arch; if not, document the `rustc`/`cargo` prerequisite in
   `check-setup-deps.sh`.
