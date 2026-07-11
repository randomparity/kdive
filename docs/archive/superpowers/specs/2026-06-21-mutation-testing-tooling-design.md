# On-demand mutation testing tooling — `just mutate`

- **Type:** developer tooling · **Tool:** [mutmut 3.6.0](https://mutmut.readthedocs.io/) · **No CI gate, no new locked dependency.**

## Goal

Give developers a `just mutate <source-module> <test-path>` recipe that runs mutation testing
against one module at a time and reports surviving mutants (places where a behavioral change to the
code would not make any test fail). This validates that the ~4,859-test suite actually catches
regressions in the code it covers, not merely that lines execute.

Runtime depends entirely on the target's tests. Against **container-free** targets (the tests don't
touch Postgres) a run is fast enough to use iteratively while strengthening a test file. Against
**Postgres-backed** targets each run pays disposable-container cost (once for mutmut's stats pass,
then per mutant's covering tests), so those are a deliberate "run sparingly" case, not the fast
inner loop. The doc labels each default target accordingly.

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

| target module | covering tests (explicit arg) | cost |
|---|---|---|
| `src/kdive/domain/errors.py` | `tests/domain/test_errors.py` | container-free (fast loop) |
| `src/kdive/domain/capacity/state.py` | `tests/services/allocation tests/services/systems` | Postgres-backed |
| `src/kdive/security/authz/` | `tests/security/authz` | Postgres-backed |
| `src/kdive/security/secrets/redaction.py` | `tests/security` | Postgres-backed |

These paths and cost labels are illustrative and **must be confirmed during implementation** by
locating the tests that actually exercise each module (the suite layout does not mirror `src/`) and
checking whether they request the Postgres fixtures. `domain/errors.py` is the verified
container-free starting point; the broader `src/kdive/domain/` set is mixed.

**Postgres caveat — these targets are not all Postgres-free.** `tests/domain/conftest.py` and
`tests/security/conftest.py` import `migrated_url`/`pg_conn`/`postgres_url` from
`tests/db/conftest.py`, so any selected test that requests those fixtures starts a disposable
Postgres container per run. The earlier "pure logic, no Postgres" framing was wrong. The doc states
the real per-target cost; truly container-free targets are a narrower set (e.g. `domain/errors.py`,
`domain/` cost math) that the implementation identifies and labels. **Prefer container-free targets
for iterative use.**

**Container-leak warning (Postgres targets).** mutmut kills slow mutants on timeout, and
testcontainers relies on normal process exit to stop the container it started. A mutant killed
mid-test will not run that cleanup, so a Postgres-backed run can orphan containers that accumulate
over many mutants. The doc warns about this and gives a cleanup hint (`docker ps` / `docker rm`);
the exact testcontainers-under-mutmut lifecycle is pinned by spike 5.

## Components

- **`scripts/mutate.py`** (~150 lines) — the wrapper, following the existing `scripts/*.py`
  guard-script convention. Responsibilities:
  1. Require both args; validate the source path exists and is under `src/kdive/`, and that the
     test path(s) exist.
  2. Write a transient `setup.cfg` `[mutmut]` section.
  3. **Validity guard, two layers** (mutation results are meaningless unless the unmutated suite
     passes):
     - *Repo-root collection check* — `pytest --co -q` over the scoped selection at repo root;
       abort on a collection error or zero tests collected. This catches a wrong/empty test path
       **cheaply** (no test execution, no Postgres), failing fast before the copy. It deliberately
       does **not** run the tests — "do they pass" is left to the authoritative in-copy baseline,
       so a Postgres target isn't billed a full extra suite run here.
     - *In-copy baseline* — mutmut's own first pass runs the unmutated suite inside the `mutants/`
       copy. The wrapper parses mutmut's output and aborts if that baseline fails or finds no tests.
       This is the layer that catches both genuinely failing tests and `also_copy`/copy-scope
       breakage (the failure mode the repo-root check cannot see). Copy-scope correctness is also
       pinned by spike 2.
  4. **Isolate the result store per target** — see step 6; clean or namespace `mutants/` when the
     target differs from the last run so summaries never conflate targets.
  5. Invoke mutmut via `uv run --with 'mutmut==3.6.0'`.
  6. Parse `mutmut results`, filtered to the current `source_paths`, and print a survivor summary.
  7. Always remove the transient config (`finally`).
- **`just mutate` recipe** — thin pass-through: `uv run python scripts/mutate.py "$@"`.
- **`.gitignore` additions** — `mutants/` (mutmut's working/cache directory) and the transient
  `setup.cfg`.
- **`docs/development/mutation-testing.md`** — usage, default targets with their explicit test
  paths, the per-target Postgres cost, the libcst/Rust host prerequisite, and how to read and act
  on survivors.

## Data flow (one invocation)

1. Developer runs e.g. `just mutate src/kdive/domain/errors.py tests/domain/test_errors.py`
   (container-free, fast). Both args are required; the test path is taken verbatim (no inference, no
   full-suite fallback). Multiple test paths are passed space-separated and split by the wrapper.
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
3. **Validity guard** (Components step 3): repo-root `pytest --co` aborts on a bad/empty test path
   cheaply; mutmut's in-copy baseline pass is parsed to abort on failing tests or copy-scope
   breakage.
4. **Store isolation** (Components step 4): if the target differs from the last run, clean/namespace
   `mutants/` so this run's `mutmut results` reflects only this target.
5. mutmut: one coverage stats pass over the scoped tests → mutant→test map → run each mutant
   against only its covering tests.
6. Wrapper runs `mutmut results` (filtered to the current `source_paths`) and prints counts
   (killed / survived / timeout / suspicious), the survivor list with `file:line`, and the hint to
   inspect via `mutmut show <id>` / `mutmut browse`. The summary also reports **coverage context** so
   a low survivor count on a poorly-covered module can't be misread as strong testing. Its source
   depends on spike 4: if mutmut exposes covered/total lines parseably, use that; otherwise the
   summary prints the **mutated-line count** from `mutmut results` (always available) and the wrapper
   does not run a separate `coverage` pass solely for the ratio — the extra cost (and, for Postgres
   targets, extra container time) isn't worth it. It states the result is **relative to the test
   path supplied**.
7. `finally`: remove the transient `setup.cfg`.

## Error handling

- **Refuse to clobber a pre-existing `setup.cfg`.** The repo has none today; if one appears, fail
  with a clear message rather than overwrite it.
- **Validate both paths.** Source exists and is under `src/kdive/`; every test path exists. Missing
  test path → clear error, not a silent broadening to the full suite.
- **Broken baseline / zero tests collected** → abort, via both validity layers (Components step 3):
  the repo-root `pytest --co` for a bad/empty path, and the parsed in-copy mutmut baseline for
  failing tests or copy-scope breakage. Invalid runs can never masquerade as a clean result.
- **In-flight / stale transient config.** The generated `setup.cfg` carries a marker header. If a
  marked config already exists on start, the wrapper **refuses to run** and tells the user another
  run is in flight (or to delete the stale file if none is). It never auto-deletes — that would nuke
  a concurrent run's config. This single rule covers both the concurrency clash and a hard-kill
  leftover; `.gitignore` is the backstop against committing it.
- **Survivors are data, not a crash.** A non-zero mutmut exit (survivors exist) is surfaced as the
  summary, not a stack trace.

## Testing

- `tests/test_mutate_script.py` — fast, Postgres-free unit tests of the wrapper's decision logic:
  required-argument enforcement, source-path validation (must exist, under `src/kdive/`), multi
  test-path splitting and validation (each must exist), transient-config rendering (correct
  `source_paths`, test selection, `also_copy`, `do_not_mutate_patterns` *without* `raise`), the
  in-flight-config refusal, and the store-isolation decision (target-changed vs. same-target). The
  two validity layers are tested by stubbing the subprocess to simulate pass / fail / zero-collected
  for both the repo-root pre-flight and a parsed in-copy mutmut baseline, asserting the wrapper
  aborts in each failure case. Summary formatting is tested against canned `mutmut results` output,
  including the covered-vs-uncovered coverage line. Tests behavior, not implementation.
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
4. **Result-store and output behavior.** Confirm how mutmut keys/invalidates the `mutants/` store
   when `source_paths` changes between runs, and whether `mutmut results` can be filtered to one
   `source_paths` (decides the store-isolation mechanism in Components step 4: clean vs. namespace
   vs. filter). Confirm mutmut surfaces a failing/empty in-copy baseline in a parseable way (needed
   for the validity guard). Confirm whether mutmut exposes covered/total line counts parseably; if
   not, the summary falls back to the mutated-line count (Data-flow step 6).
5. **testcontainers lifecycle under mutmut.** For a Postgres-backed target, confirm whether a
   timed-out/killed mutant orphans a Postgres container, and whether containers start per fork or
   once per run. This decides how strong the container-leak warning must be and whether the wrapper
   should attempt post-run cleanup.
