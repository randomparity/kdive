# CI test summary must not report a red run as clean (#2068)

Design for issue [#2068](https://github.com/randomparity/kdive/issues/2068).
Decision record: [ADR-0578](../../adr/0578-the-ci-test-summary-fails-closed-on-an-unusable-report.md).

## Goal

`scripts/pytest_summary.py` promises, in its own module docstring, that "an unreadable report
must never render as a run with no failures". Two defects break that promise. Close both, and
remove the cause of the one that has a cause.

## Problem

Both were found by an independent adversarial review of PR #2067 and reproduced against
`main` at `144aed2e6`.

**A red run can render `0 failed`.** `_render` has no zero-test floor. Two independent paths
produce a zero-test report:

- A nested pytest inherits the step-wide `PYTEST_ADDOPTS` and writes its own report over the
  shared path. `tests/live_vm/test_systemd_worker_lifecycle_support.py:19-26` spawns
  `pytest --collect-only` with no `env=`. Despite its path it is in the default gate
  selection: 13 tests collect under `-m "not live_vm and not live_stack and not agent_smoke"`.
  The outer session normally overwrites at `sessionfinish`; when it never gets there
  (cancelled job, OOM-killed controller, step timeout — the cases `if: always()` exists for)
  the summary reads the leftover.
- pytest exit 5. Verified on 9.1.1: an empty selection exits 5 having written a well-formed
  report with `tests="0"`, which the current renderer prints as
  `0 tests · 0 passed · 0 skipped · 0 failed · 0 errors — 0.0s`. No subprocess involved, so
  no subprocess hygiene reaches it.

**`summarize()` raises on a truncated non-ASCII report.** `Path.read_text(encoding="utf-8")`
raises `UnicodeDecodeError`, a `ValueError`, so neither `except OSError` nor
`except ET.ParseError` catches it. Exit 1, on exactly the killed-mid-write case the docstring
claims to cover.

## Approach

Two independent parts, plus the decode fix. They are not redundant: the scrub removes the
foreign-write path at its cause, and the floor is the only thing covering exit 5.

1. **Scrub `PYTEST_ADDOPTS` from the environment in a `pytest_collection` hook** in
   `tests/conftest.py`. It runs after each process has configured itself from the variable
   and before that process imports any test module.
2. **Floor the renderer.** `_render` treats zero total tests as an unusable report.
3. **Parse from bytes**, so a truncated multibyte tail is a `ParseError` rather than an
   uncaught `UnicodeDecodeError`.

An earlier draft fixed each nested-pytest call site with an explicit `env=` and enforced it
with an AST guard over the test tree. ADR-0578 records why the scrub replaced it: one hook
instead of a per-site obligation plus a guard, and it covers a pytest spawned from `src/`,
which a test-tree guard cannot see.

## Failure contract

After this change `summarize()` returns path-naming prose, and `main` returns 0, for every
one of:

| report state | before | after |
|---|---|---|
| missing | prose | prose (unchanged) |
| XML does not parse | prose | prose (unchanged) |
| truncated mid-multibyte | **raises, exit 1** | prose |
| parses, describes zero tests | **`0 failed`** | prose, with its own reason |
| parses, describes a real run | totals + failures | unchanged |

## Components

**`tests/conftest.py`** — a `pytest_collection` hook that pops `PYTEST_ADDOPTS` from
`os.environ` and returns `None` so normal collection proceeds, with a comment naming #2068
and the timing reason.

The hook must be `pytest_collection`, and the timing is the whole point. It has to run late
enough that the process has configured itself from the variable, and early enough that
nothing has spawned a subprocess. Measured (pytest 9.1.1, xdist 3.8.0), with
`PYTEST_ADDOPTS="--tb=line -o junit_family=xunit1 --junit-xml=…"`:

| where the pop runs | worker config | fires in a worker with no tests | module-import spawn |
|---|---|---|---|
| conftest import | `tb=auto`, `xunit2` — **lost** | n/a | covered |
| `pytest_configure` | `tb=auto`, `xunit2` — **lost** | — | covered |
| `pytest_sessionstart` | `tb=auto`, `xunit2` — **lost** | — | covered |
| session autouse fixture | `tb=line`, `xunit1` | **no** — `gw0`/`gw1` of 4 only | **leaks** |
| `pytest_collection` | `tb=line`, `xunit1` | **yes** — all 4 | covered |

The three early hooks lose worker configuration because the xdist controller runs them before
spawning workers. The fixture runs too late: only in a process assigned at least one test, and
only after test modules are imported.

**`scripts/pytest_summary.py`**

- `summarize()` reads with `Path.read_bytes()` and parses from bytes, so the parser honours
  the XML encoding declaration and a truncated multibyte tail arrives as the `ET.ParseError`
  already handled. Verified: `ET.fromstring` on a report cut mid-character raises
  `ParseError: partial character`; a well-formed non-ASCII report parses from bytes.
- `_render()` returns `_no_report(...)` prose when the summed test count is zero, with its own
  `why` naming the actual condition — the report is present and well-formed and describes no
  tests. The two existing reasons ("the test step wrote none", "its XML did not parse") are
  both false for an exit-5 report, so reusing either would replace a misleading totals line
  with a misleading cause.

**`tests/scripts/test_pytest_summary.py`** — cases for the zero-test floor and the truncated
non-ASCII report, and the non-ASCII case added to `test_main_returns_zero_for_every_input`'s
argv table so that property stops being true by construction of its own inputs.

**One new test for the scrub** — that a subprocess spawned from a test does not see
`PYTEST_ADDOPTS`, asserted with the variable actually set in the parent. This is the
behaviour, so it is what gets asserted; asserting that `tests/conftest.py` contains a `pop`
call would be testing the implementation.

**Three corrections carried from the same review** (non-blocking, same subsystem):

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
- Zero-test prose is distinguishable from the missing-report and unparseable-report prose.
- Report truncated mid-multibyte → `summarize()` returns prose, `main` returns 0.
- Well-formed non-ASCII report → parses, and the failure's reason text survives to the output.
- Real report with failures → unchanged rendering (regression guard for the floor).
- A subprocess spawned from a test does not see `PYTEST_ADDOPTS`, asserted with the variable
  actually set in the parent.
- A subprocess spawned at test-module import time does not see it either — this is the case
  that distinguishes the chosen hook from a session fixture, so it is the one worth pinning.
- Each new assertion mutation-verified: break the behaviour, watch the test redden, restore.

## Residual, accepted

The floor covers only *zero*. A nested pytest that runs N>0 tests and passes would write a
plausible `N passed` report that nothing here detects. The scrub is what prevents that, so
the residual is a pytest spawned before collection begins — from a conftest at import, or
from a `pytest_configure` hook — or by a separate tool the workflow step invokes. None exists
in this tree today, and catching them would require independently attesting the totals, which
costs more than the exposure.

## Out of scope

- #2065 (`test-ordering.yml` does not record its `PYTHONHASHSEED`) — its own issue.
- Any `src/` change. The defect is in `scripts/` and `tests/`.
- What the #2067 review cleared: `xunit1` safety on pytest 9.1.1, xdist writing the JUnit
  report only from the controller, and the gate's exit code being structurally immovable.

## Guardrail

`env -u FORCE_COLOR just ci` green before the PR.
