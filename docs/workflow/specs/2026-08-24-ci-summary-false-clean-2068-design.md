# CI test summary must not report a red run as clean (#2068)

Design for issue [#2068](https://github.com/randomparity/kdive/issues/2068).
Decision record: [ADR-0578](../../adr/0578-a-zero-test-report-is-a-signal-not-a-result.md).

## Goal

`scripts/pytest_summary.py` promises, in its own module docstring, that "an unreadable report
must never render as a run with no failures". Two defects break that promise. Close both, and
make the first structurally unable to return.

## Problem

Both were found by an independent adversarial review of PR #2067 and reproduced against
`main` at `144aed2e6`.

**A red run can render `0 failed`.** `_render` has no zero-test floor. Two paths produce a
zero-test report:

- A nested pytest inherits the step-wide `PYTEST_ADDOPTS` and writes its own report over the
  shared path. `tests/live_vm/test_systemd_worker_lifecycle_support.py:19-26` spawns
  `pytest --collect-only` with no `env=`. Despite its path it is in the default gate
  selection: 13 tests collect under `-m "not live_vm and not live_stack and not agent_smoke"`.
  The outer session normally overwrites at `sessionfinish`; when it never gets there
  (cancelled job, OOM-killed controller, step timeout — the cases `if: always()` exists for)
  the summary reads the leftover.
- pytest exit 5. Verified on 9.1.1: an empty selection exits 5 having written a well-formed
  report with `tests="0"`, which the current renderer prints as
  `0 tests · 0 passed · 0 skipped · 0 failed · 0 errors — 0.0s`. No subprocess involved.

**`summarize()` raises on a truncated non-ASCII report.** `Path.read_text(encoding="utf-8")`
raises `UnicodeDecodeError`, a `ValueError`, so neither `except OSError` nor
`except ET.ParseError` catches it. Exit 1, on exactly the killed-mid-write case the docstring
claims to cover.

## Approach — three layers

Ordered so each covers what the others cannot:

1. **Remove the cause.** The one offending call site passes an explicit `env=` with
   `PYTEST_ADDOPTS` removed.
2. **Stop it returning.** A guard over the test tree fails when a test spawns pytest as a
   subprocess without an explicit `env=`.
3. **Backstop.** `_render` treats zero total tests as an unusable report. This is the only
   layer covering pytest exit 5, which no subprocess hygiene reaches.

Layer 3 alone would suppress the visible symptom of both paths, which is why layers 1 and 2
are not optional: a foreign process overwriting the run's report is a defect whether or not
the renderer hides its effect.

## Failure contract

After this change `summarize()` returns path-naming prose, and `main` returns 0, for every
one of:

| report state | before | after |
|---|---|---|
| missing | prose | prose (unchanged) |
| XML does not parse | prose | prose (unchanged) |
| truncated mid-multibyte | **raises, exit 1** | prose |
| parses, describes zero tests | **`0 failed`** | prose |
| parses, describes a real run | totals + failures | unchanged |

## Components

**`scripts/pytest_summary.py`**

- `summarize()` reads with `Path.read_bytes()` and parses from bytes, so the parser honours
  the XML encoding declaration and a truncated multibyte tail arrives as the `ET.ParseError`
  already handled. Verified: `ET.fromstring` on a report cut mid-character raises
  `ParseError: partial character`; a well-formed non-ASCII report parses from bytes.
- `_render()` returns `_no_report(...)` prose when the summed test count is zero. The message
  must say the report described no tests and name the path, distinguishably from the missing
  and unparseable messages, so the job log tells a reader which of the three happened.

**`tests/live_vm/test_systemd_worker_lifecycle_support.py`** — the `subprocess.run` at
line 19 gains `env={k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}`. The full
environment minus that one variable, not the minimal dict
`tests/test_conftest_s3_env.py:42` uses: that test is deliberately isolating the environment,
this one just needs the leak closed.

**`tests/guards/` — one new guard.** Walks the `tests/` tree with `ast`, finds
`subprocess.run` / `Popen` / `check_output` / `check_call` calls whose argument list contains
the literal `"pytest"`, and requires each to pass an `env=` keyword. Reports offenders as
`file:line` with the reason. Static, so it costs nothing at runtime and cannot itself be
defeated by the environment it is guarding.

The two other nested-pytest sites are already compliant and must stay green under the guard:
`tests/test_conftest_s3_env.py:36,62` pass explicit `env=`;
`tests/scripts/test_justfile_test_recipes.py:69` runs `just --dry-run`, not pytest, so the
guard's `"pytest"` predicate does not match it.

**`tests/scripts/test_pytest_summary.py`** — cases for the zero-test floor and the truncated
non-ASCII report, and the non-ASCII case added to `test_main_returns_zero_for_every_input`'s
argv table so that property stops being true by construction of its own inputs.

**Three corrections carried from the same review** (non-blocking, same files):

- `AGENTS.md` — the commit guidance ends in `git add -A`, which stages every untracked and
  unstaged file `prek run` just restored, turning a targeted commit into a whole-tree one.
  Contradicts the repo's one-logical-change-per-commit rule. Becomes path-scoped.
- `tests/guards/test_commit_hook_guidance.py` — slices to the first blank line, so a routine
  reflow splitting today's single 959-char paragraph fails the guard on intact guidance.
  Anchor on the next bold lead-in instead.
- `.github/workflows/ci.yml` — the summary step runs `uv run python`, which attempts a project
  sync for a stdlib-only script. `uv run --no-project python` avoids a re-resolve on a runner
  where the install step already failed, which is exactly when the summary matters most.

## Testing

Every case below is a test in this change, not a manual check.

- Zero-test report → prose naming the path, and *not* containing `0 failed`. Asserting the
  absence matters: asserting only that prose appears would pass if both were emitted.
- Report truncated mid-multibyte → `summarize()` returns prose, `main` returns 0.
- Well-formed non-ASCII report → parses, and the failure's reason text survives to the output.
- Real report with failures → unchanged rendering (regression guard for the floor).
- Guard: compliant call sites pass; a fixture representing a non-compliant call is reported
  with its line number.
- The guard must be mutation-verified against the pre-fix tree — restore the offending call
  site, confirm the guard reddens and names line 19, restore.

## Out of scope

- #2065 (`test-ordering.yml` does not record its `PYTHONHASHSEED`) — its own issue.
- Any `src/` change. The defect is in `scripts/` and `tests/`.
- What the #2067 review cleared: `xunit1` safety on pytest 9.1.1, xdist writing the JUnit
  report only from the controller, and the gate's exit code being structurally immovable.

## Guardrail

`env -u FORCE_COLOR just ci` green before the PR.
