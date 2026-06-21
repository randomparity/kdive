# On-demand mutation testing tooling — `just mutate`

- **Type:** developer tooling · **Tool:** [mutmut 3.6.0](https://mutmut.readthedocs.io/) · **No CI gate, no new locked dependency.**

## Goal

Give developers a `just mutate <source-module> [test-path]` recipe that runs mutation testing
against one module at a time, reports surviving mutants (places where a behavioral change to the
code would not make any test fail), and is fast enough to use iteratively while strengthening a
test file. This validates that the ~4,859-test suite actually catches regressions in the code it
covers, not merely that lines execute.

This is **repeatable local tooling**, not a CI gate and not a whole-tree campaign. The recipe
takes any module; the documented default targets are the safety-critical cross-cutting invariants
and the pure-logic domain layer.

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
- Run ephemerally via `uv run --with 'mutmut==3.6.0'`, matching the repo's existing convention for
  occasional tools (zizmor, actionlint, git-cliff via `uv run --with` / `uvx`). mutmut depends on
  `coverage`, `pytest`, `libcst`, and `textual`, so no extra `--with` is needed. **Not** added to
  the locked dev dependency group.

cosmic-ray (heavier, session/DB-based, distributed) was rejected as overkill for on-demand local
runs. Static `[tool.mutmut]` config (edit-to-retarget) was rejected because it cannot deliver the
`just mutate <module>` ergonomics and forces every run through the full Postgres-backed suite.

## Default targets

The recipe accepts any module under `src/kdive/`. The documented starting targets, chosen because
they are high-blast-radius and pure-ish (fast, no Postgres/Docker):

- `src/kdive/domain/capacity/state.py` — the state-transition adjacency guard.
- `src/kdive/security/authz/` — the destructive-op gate, RBAC, actor/context.
- `src/kdive/security/secrets/redaction.py` — mandatory output redaction.
- `src/kdive/domain/` — state machines, error taxonomy, profiles, accounting math.

## Components

- **`scripts/mutate.py`** (~100 lines) — the wrapper, following the existing `scripts/*.py`
  guard-script convention. Responsibilities:
  1. Validate the target path exists and is under `src/kdive/`.
  2. Resolve the test scope (see Data flow).
  3. Write a transient `setup.cfg` `[mutmut]` section.
  4. Invoke mutmut via `uv run --with 'mutmut==3.6.0'`.
  5. Parse `mutmut results` and print a survivor summary.
  6. Always remove the transient config (`finally`).
- **`just mutate` recipe** — thin pass-through: `uv run python scripts/mutate.py "$@"`.
- **`.gitignore` additions** — `mutants/` (mutmut's working/cache directory) and the transient
  `setup.cfg`.
- **`docs/development/mutation-testing.md`** — usage, default targets, the Postgres caveat for
  db-layer modules, and how to read and act on survivors.

## Data flow (one invocation)

1. Developer runs e.g. `just mutate src/kdive/domain/capacity/state.py`
   (optional 2nd arg: an explicit test path).
2. **Test-scope resolution**, in order:
   - explicit test-path argument wins;
   - else heuristic `src/kdive/<X>/… → tests/<X>/…` when that test path exists;
   - else fall back to the full non-live suite **with a loud printed warning** — never a silent
     broadening (the fallback pulls in Postgres-backed tests).
3. Wrapper writes a transient `setup.cfg` `[mutmut]` with:
   - `source_paths` = the target module;
   - `pytest_add_cli_args_test_selection` = `-m "not live_vm and not live_stack"` plus the resolved
     test path;
   - `mutate_only_covered_lines = true`;
   - `also_copy = pyproject.toml` (mutmut runs the suite from an isolated copy under `mutants/`;
     pyproject carries pytest's `pythonpath` and marker definitions; all `conftest.py` live under
     `tests/` and are copied with the test tree);
   - `do_not_mutate_patterns` = `logger.\w+`, `raise \w+` (don't chase log-string and bare-raise
     mutants);
   - `max_stack_depth = 8` (keep relevant tests localized).
4. mutmut: one coverage stats pass over the scoped tests → mutant→test map → run each mutant
   against only its covering tests.
5. Wrapper runs `mutmut results` and prints counts (killed / survived / timeout / suspicious) plus
   the survivor list with `file:line`, and the hint to inspect via `mutmut show <id>` /
   `mutmut browse`.
6. `finally`: remove the transient `setup.cfg`.

## Error handling

- **Refuse to clobber a pre-existing `setup.cfg`.** The repo has none today; if one appears, fail
  with a clear message rather than overwrite it.
- **Validate the target path** exists and is under `src/kdive/`; clear, actionable error otherwise.
- **Stale transient config** from a hard kill: the generated file carries a marker header; the
  wrapper removes a stale *marked* file on the next start, and `.gitignore` is the backstop.
- **Survivors are data, not a crash.** A non-zero mutmut exit (survivors exist) is surfaced as the
  summary, not a stack trace.

## Testing

- `tests/test_mutate_script.py` — fast, Postgres-free unit tests of the wrapper's decision logic:
  module→test-path mapping (explicit / heuristic / fallback-with-warning), target-path validation,
  and transient-config rendering. Tests behavior (what config/decisions the wrapper produces for
  given inputs), not implementation details.
- mutmut itself is **not** run inside the test suite (too slow, needs the full environment). The
  wrapper's decisions are unit-tested; mutmut is exercised manually against the documented targets.

## Out of scope (YAGNI)

No CI gate, no mutation-score threshold, no whole-tree campaign, no written report artifact. The
terminal summary plus `mutmut browse` are sufficient; a report or CI gate can be added later if a
campaign is wanted.

## Assumptions

- Doc lives at `docs/development/mutation-testing.md`, alongside `releasing.md`.
- The heuristic module→test fallback is kept, guarded by a loud warning (rather than requiring an
  explicit test path on every run).
