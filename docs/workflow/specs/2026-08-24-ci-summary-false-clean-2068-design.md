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

1. **Scrub `PYTEST_ADDOPTS` from the environment in a `pytest_collection` hook**, defined in
   `tests/_addopts_scrub.py` and re-exported from `tests/conftest.py` so pytest registers it.
   It runs after each process has configured itself from the variable and before that process
   imports any test module.
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
| XML does not parse (includes a zero-byte file: `no element found`) | prose | prose (unchanged) |
| truncated mid-multibyte | **raises, exit 1** | prose |
| parses, sums to zero tests, no failing cases | **`0 failed`** | prose, with its own reason |
| `tests` attribute unparseable **but failing cases present** | totals wrong, failures listed | unchanged — must NOT hit the floor |
| root is not a testsuite (e.g. an HTML error page) | `0 failed` | prose |
| valid report describing only skips | totals | unchanged (a real result) |
| parses, describes a real run | totals + failures | unchanged |

`_int` returns `0` for an unparseable attribute, so `tests="abc"` sums to zero while
`_failing_cases` still finds the failures. A naive `tests == 0` floor would therefore discard
a real failure list and claim the report describes no tests — a second false-clean, in the
opposite direction. **The floor condition is `tests == 0 and not _failing_cases(root)`.**

## Components

**`tests/_addopts_scrub.py`** — a `pytest_collection` hook that calls
`os.environ.pop("PYTEST_ADDOPTS", None)` and returns `None` so normal collection proceeds,
with a comment naming #2068 and the timing reason. `tests/conftest.py` re-exports it
(`from tests._addopts_scrub import pytest_collection`), which is what registers it: a
conftest hook has to be an attribute of the conftest module.

It lives in its own module rather than inline in the conftest so the scrub's own nested-pytest
tests can import the *shipped* hook without importing `tests.conftest`, which pulls in the
whole `kdive` package — 1.45s per nested run against a 0.03s baseline, and it would couple
those tests to product-package import health.

The `None` default is load-bearing: the variable is unset on every local run, and a bare
`pop("PYTEST_ADDOPTS")` would raise `KeyError` and break `just test` for everyone.

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
spawning workers. The fixture runs too late: only in a process assigned at least one test
(two of four workers in the measured run), and only after test modules are imported.

Under xdist the hook **never runs on the controller** — `DSession.pytest_collection` returns
`True` and the hookspec is `firstresult`, so the chain stops first. That is exactly why
workers keep their configuration: the controller still holds the variable when it spawns them.
Two consequences for the implementation: it must **not** be marked `tryfirst` (that would
order it ahead of `DSession` and restore the regression), and the controller keeps
`PYTEST_ADDOPTS` for the whole session, so a controller-side spawn is outside what this
covers.

The table was measured in a minimal synthetic repo. It transfers because, in the tree this
change starts from, `tests/conftest.py` defined no `pytest_*` hooks, no `pytest_collection`
existed anywhere, and no plugins beyond `pytest-xdist` are installed — all three checked. The
hook this change adds is the only `pytest_collection` in the tree, so nothing competes with it
for the `firstresult` chain.

**`scripts/pytest_summary.py`**

- `summarize()` reads with `Path.read_bytes()` and parses from bytes, so the parser honours
  the XML encoding declaration and a truncated multibyte tail arrives as the `ET.ParseError`
  already handled. Verified: `ET.fromstring` on a report cut mid-character raises
  `ParseError: partial character`; a well-formed non-ASCII report parses from bytes.
- `_render()` returns `_no_report(...)` prose when `tests == 0 and not _failing_cases(root)`,
  with this exact `why`:

  > `it parsed but totals zero tests — the run collected nothing, or the report is not this run's`

  Worded as what the code *observed*, not as a claim about the run: the renderer cannot tell
  an exit-5 collection from a foreign report, and must not assert either. The two existing
  reasons ("the test step wrote none", "its XML did not parse") are both false here, so
  reusing either would replace a misleading totals line with a misleading cause.

**`tests/scripts/test_pytest_summary.py`** — cases for the zero-test floor and the truncated
non-ASCII report, and the non-ASCII case added to `test_main_returns_zero_for_every_input`'s
argv table so that property stops being true by construction of its own inputs.

**Two new tests for the scrub, both nested-pytest runs.** A direct in-process test cannot
express this: the hook pops at collection, before any test body runs, so a test that
`monkeypatch.setenv`s the variable and spawns a child sees the child inherit it (the test
fails), and one that does not set it passes vacuously (the test proves nothing).

The construction is an outer test that writes a small fixture module to `tmp_path`, then runs
`pytest` on it as a subprocess with `PYTEST_ADDOPTS` present in an explicit `env=`, and
asserts on that nested run's exit code. The inner module is what carries the real assertion,
against a *grandchild* process:

- one inner test spawns a grandchild from inside a test body and asserts it sees no
  `PYTEST_ADDOPTS`;
- one inner module spawns a grandchild **at module import time** and asserts the same. This is
  the case that distinguishes `pytest_collection` from a session fixture, so it is the one
  worth pinning.

The nested run needs the hook in scope. **Chosen:** write a conftest beside the fixture module
under `tmp_path` that imports the shipped hook from `tests._addopts_scrub` (with the repo root
on `sys.path`), so the nested run exercises the real implementation rather than a copy of it.
Doing so means these tests assert the *mechanism*, not the repo wiring — the hook works when
registered, not that this repo registers it. That second half is a separate guard, below;
without it, deleting the re-export from `tests/conftest.py` leaves every test here green while
real runs leak again.

**One guard for the registration** — `tests/guards/`. Assert
`tests.conftest.pytest_collection is tests._addopts_scrub.pytest_collection`. This is the half
the nested runs structurally cannot cover, per the paragraph above.

**One guard for the `tryfirst` constraint** — `tests/guards/`. It asserts on pluggy's runtime
metadata, not on source text: `getattr(hook, "pytest_impl", {})` is `{}` for an undecorated
function and carries the flags when marked (verified on pytest 9.1.1). Assert that
`tryfirst` is falsey **and** that `wrapper` and `hookwrapper` are falsey — a wrapper reorders
ahead of `DSession.pytest_collection` exactly as `tryfirst` does, so guarding only `tryfirst`
leaves the same regression reachable.

This is guarded rather than tested behaviourally because the observable it would need — a
worker process's option state — is not reachable from an assertion in this change's own
tests without a second nested `-n 2` run. A nested run *could* catch it; the guard is chosen
as the cheaper instrument for the same defect, and `tests/guards/` already holds several
source- and metadata-inspecting guards.

**Three corrections carried from the same review** (non-blocking, same subsystem):

- `AGENTS.md` — the commit guidance ends in `git add -A`, which stages every untracked and
  unstaged file `prek run` just restored, turning a targeted commit into a whole-tree one.
  Contradicts the repo's one-logical-change-per-commit rule. The replacement is **not**
  `git add -u`, which is still whole-tree over tracked files. The guidance must say: capture
  the staged set *before* running the hooks (`git diff --cached --name-only`), then re-add
  exactly those paths — `git add -- <the paths you staged>`. The existing guard's
  `assert "git add" in paragraph` survives this, so no guard change is needed for it.
- `tests/guards/test_commit_hook_guidance.py` — `_paragraph()` at line 36 splits on `"\n\n"`,
  so a routine reflow splitting today's single unbroken paragraph fails the guard on intact
  guidance. Anchor the slice on the next bold lead-in, which is `**Running the live tiers**`.
  If that lead-in is ever renamed the guard must fail loudly rather than silently widening its
  slice to the rest of the file — assert the anchor was found before slicing on it.
- `.github/workflows/ci.yml` — the summary step runs `uv run python`, which attempts a project
  sync. Use **`uv run --no-sync python`**, not `--no-project`. `--no-sync` keeps the project
  environment and its interpreter while skipping the resolve; `--no-project` discards the
  project entirely, and since `pyproject.toml` pins `requires-python = "==3.14.*"` while
  `ci.yml` installs no Python (`setup-uv` is called with no `python-version`, and there is no
  `actions/setup-python`), it would fall back to the runner's ambient interpreter or trigger a
  download — the opposite of the goal, and against the script's own docstring, which records
  that it is not portable to an arbitrary interpreter.

## Testing

Every case below is a test in this change, not a manual check.

- Zero-test report → the output contains the floor's exact `why` phrase, and does **not**
  contain the totals line. Assert the totals' absence by its separator `" tests · "`, not by
  the literal `0 failed`: a renderer emitting `0 failures` would slip that assertion.
- The floor's `why` phrase appears in the zero-test prose and in **neither** the
  missing-report nor the unparseable-report prose. `assert a != b` is not sufficient — it
  passes on any one-word difference.
- A report whose `tests` attribute is unparseable **but which carries failing cases** renders
  its failures and does *not* hit the floor. This is the regression the floor could introduce.
- Report truncated mid-multibyte → `summarize()` returns prose, `main` returns 0.
- Zero-byte report → prose (`ParseError: no element found`), `main` returns 0.
- Well-formed non-ASCII report → parses, and the failure's reason text survives to the output.
- Real report with failures → unchanged rendering (regression guard for the floor).
- Nested-pytest run: a grandchild spawned from a test body sees no `PYTEST_ADDOPTS`.
- Nested-pytest run: a grandchild spawned at module import time sees none either.
- Guard: the hook carries no `tryfirst`, `wrapper`, or `hookwrapper` in its `pytest_impl`.
- Each new assertion mutation-verified: break the behaviour, watch the test redden, restore.
  For the floor, that includes reverting the `and not _failing_cases(root)` clause and
  confirming the unparseable-attribute test reddens.

## Residual, accepted

The floor covers only *zero*. A nested pytest that runs N>0 tests and passes would write a
plausible `N passed` report that nothing here detects. The scrub is what prevents that, so
the residual is a pytest spawned before the hook runs — from a conftest at import or a
`pytest_configure` hook — from controller-side code under `-n` (the controller never runs the
hook, so it holds the variable all session), or by a separate tool the workflow step invokes.
None exists in this tree today, and catching them would require independently attesting the
totals, which costs more than the exposure.

## Out of scope

- #2065 (`test-ordering.yml` does not record its `PYTHONHASHSEED`) — its own issue.
- Any `src/` change. The defect is in `scripts/` and `tests/`.
- What the #2067 review cleared: `xunit1` safety on pytest 9.1.1, xdist writing the JUnit
  report only from the controller, and the gate's exit code being structurally immovable.

## Guardrail

`env -u FORCE_COLOR just ci` green before the PR.
