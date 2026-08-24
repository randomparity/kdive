# 0578 — A zero-test JUnit report is a signal, not a result

## Status

Proposed

## Context

`scripts/pytest_summary.py` (ADR-less, added by #2062/PR #2067) renders the CI run's JUnit
report into the job summary. Its module docstring states the property it exists to hold:

> an unreadable report must never render as a run with no failures.

It holds that for a report that is *missing* and for one whose XML does not *parse*. It does
not hold it for a report that parses cleanly and describes nothing. `_render` sums the suite
counters with no floor, so a report carrying `tests="0"` renders

```
0 tests · 0 passed · 0 skipped · 0 failed · 0 errors — 0.0s
```

followed by the confirming `pytest_summary: wrote N bytes` trace — a red or aborted run
presented as a clean one. Two paths reach it, and they are not the same kind of problem:

1. **A foreign write.** `.github/workflows/ci.yml` sets `PYTEST_ADDOPTS` for the whole `Test`
   step, so any pytest a test spawns as a subprocess inherits it and writes its own report
   over the shared path.
   `tests/live_vm/test_systemd_worker_lifecycle_support.py` does exactly that, with no `env=`,
   and despite its path it is in the default gate selection — 13 tests collect under
   `-m "not live_vm and not live_stack and not agent_smoke"`. Normally the outer session
   overwrites the file at `sessionfinish` and the damage is invisible; when the outer session
   never reaches `sessionfinish` — a cancelled job, an OOM-killed controller, a step timeout,
   all of which `if: always()` deliberately covers — the summary step reads the leftover.

2. **pytest's own exit 5.** A marker or selection typo collecting nothing is a red gate that
   writes a genuine zero-test report of its own. No foreign writer is involved, so no amount
   of subprocess hygiene reaches this one.

A third defect sits beside them: `summarize` reads the report with
`Path.read_text(encoding="utf-8")`, which raises `UnicodeDecodeError` — a `ValueError` —
so neither the `except OSError` nor the `except ET.ParseError` beneath it catches a report
truncated mid-multibyte-character. That is precisely the killed-mid-write case the docstring
claims to cover, and pytest reports routinely carry non-ASCII in assertion reprs and
parametrized ids.

## Decision

**A JUnit report describing zero tests is treated as an unusable report, not as a run in
which nothing failed.** `_render` gains a floor: zero total tests renders the same
path-naming prose as a missing or unparseable report, pointing the reader at the job log.
The gate never legitimately runs zero tests — an empty selection is pytest exit 5, which is
red — so the floor cannot mask a real result.

Two supporting decisions follow from it:

- **The report is read as bytes and parsed from bytes.** `Path.read_bytes()` into
  `ET.fromstring` lets the parser honour the XML encoding declaration instead of assuming
  UTF-8, and a truncated multibyte tail surfaces as the `ET.ParseError` the existing handler
  already covers rather than as an uncaught `UnicodeDecodeError`.
- **A test that spawns pytest as a subprocess must not pass `PYTEST_ADDOPTS` down**, enforced
  by a guard over the test tree rather than by review attention.

The three are layered deliberately: the call-site fix removes the cause of path 1, the guard
stops it returning, and the floor is the backstop that also covers path 2, which the other
two cannot reach.

## Consequences

- A cancelled, OOM-killed, or timed-out run now says its report is unusable and names the
  job log, instead of showing a clean totals line. That is the behaviour the docstring
  already promised.
- The summary step's exit status is unchanged. It stays a separate step with
  `continue-on-error: true`, so none of this can move the gate's verdict in either direction.
- A future test that spawns pytest must pass an explicit `env=`. The guard names the file and
  line and says why, so the cost is one line at the call site rather than a debugging session
  in CI.
- The floor is a heuristic about *this* gate: it assumes a zero-test run is always
  pathological. A caller that legitimately summarised an empty selection would get prose
  instead of zeros. No such caller exists — `ci.yml` is the only one — and the alternative
  ranked worse (below).
- Reading bytes rather than text means a report in a non-UTF-8 encoding now parses instead of
  raising. This is strictly wider, and no behaviour depends on the previous narrowing.

## Considered & rejected

- **Leave `_render` alone and fix only the subprocess leak.** verified: on pytest 9.1.1,
  `pytest tests/domain/test_errors.py -m nosuchmarker --junit-xml=… -o junit_family=xunit1`
  exits **5** having written a well-formed report whose sole `<testsuite>` carries
  `tests="0" failures="0" errors="0"`, and the current `scripts/pytest_summary.py` renders it
  as `0 tests · 0 passed · 0 skipped · 0 failed · 0 errors — 0.0s`. No subprocess is involved,
  so the false-clean summary survives the subprocess fix entirely. The floor is the only
  layer that covers this path.
- **Fail the summary step on a zero-test report.** judgment: the step carries
  `continue-on-error: true` precisely so it cannot move the gate, so failing it would be
  invisible in the run's conclusion while costing the reader the prose that explains what
  happened.
- **Render the zero totals with a warning banner above them.** judgment: the totals line is
  what a reader skims, and `0 failed` beside a warning still reads as a clean run at a
  glance. The failure mode being fixed is misreading, so the remedy has to remove the
  misleading line rather than annotate it.
- **Move `--junit-xml` from `PYTEST_ADDOPTS` into the `just test` recipe.** verified: the
  recipe is pinned by `tests/scripts/test_justfile_test_recipes.py` (ADR-0577), whose
  assertions fix the flag set `just test` may carry; and CI invoking `just test` verbatim is
  the property #2062 chose the environment variable to preserve. It would also write a JUnit
  file into every developer's tree on every local run.
- **Catch `UnicodeDecodeError` beside `OSError` and keep reading text.** judgment: it fixes
  the crash but keeps the assumption that the report is UTF-8, which the XML declaration is
  there to state; parsing from bytes removes the assumption rather than handling its failure.
- **Give each pytest session a unique report path.** verified: `PYTEST_ADDOPTS` is inherited
  verbatim by a child process and pytest's `--junit-xml` takes a literal path with no
  per-session template, so a nested run would write to the same unique path as its parent.
  The mechanism cannot express the fix.
- **Do nothing; rely on the job log.** judgment: the job log is the 75 KB read the summary
  exists to replace, and a summary that is silently wrong is worse than no summary — it is
  trusted at exactly the moment it misleads.
