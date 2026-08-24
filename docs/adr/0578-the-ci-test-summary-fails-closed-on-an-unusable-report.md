# 0578 — The CI test summary fails closed on an unusable JUnit report

## Status

Proposed

## Context

`scripts/pytest_summary.py` (added by #2062/PR #2067, no ADR) renders the CI run's JUnit
report into the job summary. Its module docstring states the property it exists to hold:

> an unreadable report must never render as a run with no failures.

It holds for a report that is *missing* and for one whose XML does not *parse*. It fails in
two other shapes.

**A report that parses and describes nothing.** `_render` sums the suite counters with no
floor, so `tests="0"` renders `0 tests · 0 passed · 0 skipped · 0 failed · 0 errors — 0.0s`
plus a confirming `pytest_summary: wrote N bytes` trace — a red or aborted run presented as a
clean one. Two independent paths reach it:

1. **A foreign write.** `.github/workflows/ci.yml` sets `PYTEST_ADDOPTS` for the whole `Test`
   step, so any pytest a test spawns as a subprocess inherits it and writes its own report
   over the shared path. `tests/live_vm/test_systemd_worker_lifecycle_support.py:19` does
   exactly that. Despite its path it is in the default gate selection — 13 tests collect
   under `-m "not live_vm and not live_stack and not agent_smoke"`. The outer session
   normally overwrites at `sessionfinish`, hiding it; when it never gets there (a cancelled
   job, an OOM-killed controller, a step timeout — the cases `if: always()` exists to cover)
   the summary step reads the leftover.
2. **pytest's own exit 5.** An empty selection is a red gate that writes a genuine zero-test
   report. No foreign process is involved, so no amount of subprocess hygiene reaches it.

**A report truncated mid-multibyte-character.** `summarize` reads with
`Path.read_text(encoding="utf-8")`, which raises `UnicodeDecodeError` — a `ValueError` — so
neither the `except OSError` nor the `except ET.ParseError` beneath it catches it. That is
precisely the killed-mid-write case the docstring claims to cover, and pytest reports
routinely carry non-ASCII in assertion reprs and parametrized ids.

## Decision

**The summary fails closed: any report it cannot trust renders as prose naming the path and
the reason, never as totals.** Three parts.

**1. A zero-test report is treated as unusable.** `_render` returns path-naming prose when
the summed test count is zero. The gate never legitimately runs zero tests — an empty
selection is exit 5, which is red — so the floor cannot mask a real result. It carries its
own distinct reason, naming the actual condition (the report is present and well-formed and
describes no tests); reusing the "wrote none" or "did not parse" wording would replace a
misleading totals line with a misleading cause, since both are false for an exit-5 report.

**2. `PYTEST_ADDOPTS` is scrubbed from the environment in a `pytest_collection` hook** in
`tests/conftest.py`, rather than each nested-pytest call site passing an explicit `env=`. One
hook neutralises every subprocess a test spawns — from a test, a helper, or `src/` — and it
holds even against `env=os.environ.copy()`, because the variable is gone from the source.

The hook is `pytest_collection` specifically, and the timing is the whole decision. It has to
run **late enough** that the process has already configured itself from the variable, and
**early enough** that nothing has spawned a subprocess yet. `pytest_collection` is the only
one of the candidates that is both: on the xdist controller it runs after the workers are
spawned, and in each worker it runs before any test module is imported. Because
`testpaths = ["tests"]`, `tests/conftest.py` is an initial conftest, so the hook is registered
before collection begins.

**3. The report is read as bytes and parsed from bytes.** `Path.read_bytes()` into
`ET.fromstring` lets the parser honour the XML encoding declaration instead of assuming
UTF-8, and a truncated multibyte tail surfaces as the `ET.ParseError` the existing handler
already covers.

Parts 1 and 2 are independent, not redundant: part 2 removes the foreign-write path at its
cause, and part 1 is the only thing covering exit 5.

## Consequences

- A cancelled, OOM-killed, or timed-out run now says its report is unusable and names the job
  log, instead of showing a clean totals line — the behaviour the docstring already promised.
- The summary step's exit status is unchanged. It stays a separate step with
  `continue-on-error: true`, so none of this can move the gate's verdict either way.
- **Residual: the floor covers only zero.** A nested pytest that runs N>0 tests and passes
  writes a plausible `N passed` report, and nothing here detects that. The scrub is what
  prevents it, so the residual is any pytest spawned by a process that never ran the hook —
  a separate tool invoked by the workflow step, not a test. Accepted:
  no such caller exists in `ci.yml` today, and the totals would have to be independently
  attested to catch it, which costs more than the exposure.
- The scrub is action at a distance: a test that wanted the parent's `PYTEST_ADDOPTS`
  propagated to a child would silently not get one. No test does, and a test that needs
  specific addopts in a child can pass them explicitly.
- **Residual: a subprocess spawned before collection begins** — from a conftest at import, or
  from a `pytest_configure` hook — still inherits the variable. Nothing in this tree does
  that; the offending call spawns from inside a test, and test modules are imported during
  collection, after the hook has run.
- Reading bytes rather than text means a report in a non-UTF-8 encoding now parses instead of
  raising. Strictly wider; nothing depended on the previous narrowing.

## Considered & rejected

- **Fix each nested-pytest call site with an explicit `env=`, enforced by an AST guard over
  the test tree.** verified: this was the original design; the conftest scrub replaces it
  because it is one line instead of a per-site obligation plus a guard, and it covers strictly
  more. A/B on this branch, pytest 9.1.1: with the scrub a child process reports
  `PYTEST_ADDOPTS` as `None`; without it the child inherits
  `--junit-xml=… -o junit_family=xunit1`. The guard's scope is the test tree, so a pytest
  spawned from `src/` would defeat it while the scrub covers it. (A fixture would *not* defeat
  it — fixtures live in the test tree.)
- **Scrub in a session-scoped autouse fixture.** verified: it fires only in a process that is
  assigned at least one test, and it fires after test modules are imported. Measured with
  `MODE=fixture pytest tests -n 4 -q` over a two-test suite (pytest 9.1.1, xdist 3.8.0): the
  pop ran in `gw0` and `gw1` only — `gw2` and `gw3` imported every test module and never
  popped. Serially, a module-level `subprocess.run` in a test module saw the full
  `--tb=line --junit-xml=…`, where the `pytest_collection` hook gave it `None`. Its ordering
  against sibling session-scoped autouse fixtures is also unspecified by pytest, and
  `tests/conftest.py` already defines one.
- **Pop `PYTEST_ADDOPTS` at conftest import.** verified: a tradeoff, not a worse-on-every-axis
  option — it has no collection-time residual at all, because it runs before anything. It
  loses on worker configuration: measured
  `MODE=import pytest tests/test_a.py tests/test_b.py -n 2 -q` with
  `PYTEST_ADDOPTS="--tb=line -o junit_family=xunit1 --junit-xml=…"`, the workers report
  `tb=auto junit_family=xunit2 xmlpath=None` — the controller pops before spawning them, so
  they lose every option supplied only that way. `pytest_collection` wins both axes at once,
  which is why it is chosen over both this and the fixture.
- **Scrub in `pytest_configure` or `pytest_sessionstart`.** verified: both reproduce the
  import-time worker regression. Same command at `-n 2`, workers report
  `tb=auto junit_family=xunit2` under each, because the xdist controller runs both hooks
  before it spawns the workers. This is why the hook has to be `pytest_collection` and not
  merely "a hook".
- **Leave `_render` alone and fix only the subprocess leak.** verified: on pytest 9.1.1,
  `pytest tests/domain/test_errors.py -m nosuchmarker --junit-xml=… -o junit_family=xunit1`
  exits **5** having written a well-formed report whose sole `<testsuite>` carries
  `tests="0" failures="0" errors="0"`, and the current `scripts/pytest_summary.py` renders it
  as `0 tests · 0 passed · 0 skipped · 0 failed · 0 errors — 0.0s`. Also exits 5 under
  `-n auto`. No subprocess is involved, so the false-clean summary survives the leak fix.
- **Fail the summary step on a zero-test report.** judgment: the step carries
  `continue-on-error: true` precisely so it cannot move the gate, so failing it would be
  invisible in the run's conclusion while costing the reader the prose explaining what
  happened.
- **Render the zero totals with a warning banner above them.** judgment: the totals line is
  what a reader skims, and `0 failed` beside a warning still reads as clean at a glance. The
  failure mode is misreading, so the remedy has to remove the misleading line, not annotate
  it.
- **Move `--junit-xml` from `PYTEST_ADDOPTS` into the `just test` recipe.** judgment: it would
  write a JUnit file into every developer's tree on every local run, and CI invoking
  `just test` verbatim — so the gate's command keeps its single definition in the justfile —
  is the property #2062 chose the environment variable to preserve (`ci.yml:254-266`). Note
  the recipe guard `tests/scripts/test_justfile_test_recipes.py` would *not* catch the change:
  its assertions are presence and effective-value checks (its `_TB` pattern matches only `--tb=WORD` or `--tb WORD`, never
  `--junit-xml`), so this rests on the two grounds above and not on that guard.
- **Give each pytest session a unique report path.** verified: `pytest --help` documents
  `--junit-xml` as "create junit-xml style report file at given path" — a literal path with no
  per-session template — and `PYTEST_ADDOPTS` is inherited verbatim, so a nested run would
  write to the same unique path as its parent. The mechanism cannot express the fix.
- **Catch `UnicodeDecodeError` beside `OSError` and keep reading text.** judgment: it stops
  the crash but keeps the assumption that the report is UTF-8, which the XML declaration
  exists to state; parsing from bytes removes the assumption rather than handling its failure.
- **Do nothing; rely on the job log.** judgment: the job log is the 75 KB read the summary
  exists to replace, and a summary that is silently wrong is worse than none — it is trusted
  at exactly the moment it misleads.
