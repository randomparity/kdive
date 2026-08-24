# 0577 — `just test` bounds its failure output with `--tb=short`

## Status

Accepted (2026-08-24)

## Context

`just test` is the pre-push gate and the command CI runs (`.github/workflows/ci.yml:254`),
and it is the command an agent runs most often. Its green path is already minimal. Its red
path is not bounded at all.

The belief that it was bounded came from `-q`. It is wrong: `-q` drops the per-test progress
line and the run header, and nothing else. Tracebacks print in full, with complete assertion
introspection, one per failing test. Over a suite that collects 13,382 tests, one broken
conftest or shared fixture therefore emits a full traceback per failing test. This has
already happened — `.github/workflows/ci.yml:240` records a run of `3 failed, 8938 passed,
3448 errors` with the real cause several screens down (#1913). The fix there was pre-pulling
the testcontainer images, which treats one cause of a mass failure, not the class.

Two documents stated the opposite, which is what kept the default unexamined: `AGENTS.md`
claimed the default recipes print "pass/fail counts and the short failure summary — enough
to see what broke without flooding the terminal", and the `justfile` comment on
`test-verbose` claimed `-vv --tb=long` restore detail "where `-q` trims both". Neither is
true of `-q`.

Measured on this tree (pytest 9.1.1, the `just test` flag set, 48-core host capped at 16
workers), against a real one-line mutation to `is_retryable` in `src/kdive/domain/errors.py`
so the tracebacks carry real frames, fixtures, and assertion diffs — 12 failures across
`tests/domain tests/mcp/core tests/jobs`:

| flags | total | per failure |
|---|---|---|
| `-q` (the previous default) | 35,379 B | ~2.95 KB |
| `-q --tb=short` | 16,644 B | ~1.39 KB |

An earlier synthetic benchmark — a parametrized module whose failures had a single traceback
frame — put the per-failure cost near 1 KB and made `--tb=short` look worthless, saving 24%
at 200 failures. Both conclusions were artifacts of the synthetic traceback. Real failures
cost about 3x more and `--tb=short` saves about 55% of them, so the option that framing
dismissed is the one the real measurement selects.

The escalation path had its own defect. `test-verbose` took its flags from a single
`_TEST_SELECT` that bundled the marker exclusion with `-n auto --maxprocesses=16`, so the
recipe you reach for in order to *read* a failure interleaved up to 16 workers' output. That
bundling was deliberate — it is what stops the two recipes' *selection* from drifting — so
the fix has to keep selection shared while letting parallelism differ.

## Decision

We will bound the default failure path with `--tb=short` and make an argument-carrying
`test-verbose` run serial, splitting the shared justfile variable along the
selection/parallelism seam.

1. `just test` runs `-q --tb=short`. Every failure keeps its `file:line`, the failing
   expression, the assertion message with introspection, the call chain at one line per
   frame — individually diagnosable, at roughly half the bytes. Captured log and stdout
   sections read the same under `short` as under `long`; only `--tb=no` drops them.
2. `test-lf` and `test-changed` carry the same bound. Both fall back to the whole suite — an
   empty or stale `--lf` cache, an unmappable change — so both have the gate's mass-failure
   shape, and they are the recipes the guidance tells an agent to iterate with. Bounding only
   the gate would leave the inner loop, which runs more often, unbounded.
3. `just test-verbose` stays the single escalation to `-vv --tb=long`: full frames and full
   assertion diffs, on the paths you name.
4. `just test-verbose` drops xdist and runs serially whenever it is given **any** argument,
   not only a path. `-x` and `--pdb` narrow nothing, and both want the same serial ordering;
   the recipe's condition therefore tests for arguments, and the documentation says so rather
   than describing a paths-only rule the recipe does not implement. A bare `just test-verbose`
   keeps the parallelism, because the whole suite serially is not a loop anyone waits on.
   Arguments interpolate unquoted and the shell re-splits them, so one containing a space does
   not survive: `-k retryable` works, `-k "a or b"` does not, and direct pytest covers that.
5. `_TEST_SELECT` splits into `_TEST_MARKERS` (the gated-tier marker expression) and
   `_TEST_XDIST` (parallelism and the worker cap). `test`, `test-verbose`, `test-lf`, and
   `test-changed` all take the marker expression from `_TEST_MARKERS`, so selection cannot
   drift between them — three of those carried their own literal copy before. `test-lf`
   keeps its own parallelism flags, without `--dist worksteal`: worksteal shortens the
   straggler tail of a full run, and `--lf` reruns a handful of tests with no tail to
   shorten. Only parallelism and output flags differ per recipe.
6. `AGENTS.md` and the `justfile` comments describe what `-q` actually does and what bounds
   the failure path.

## Consequences

A red `just test` costs about half what it did, and the cost per additional failure falls
with it, so a mass failure degrades more slowly. It stays long — `--tb=short` bounds the
per-failure cost, not the failure count.

CI runs the same recipe and gains less than the local measurement above. The measurement is
local, where pytest truncates a long assertion explanation to 8 lines; with `CI` set in the
environment it does not truncate at all
(`_pytest/assertion/truncate.py`), and no `--tb` style bounds that explanation. What CI saves
is the frame and source-context share only — real, but smaller than 55%.

CI also pays a cost this ADR accepts rather than mitigates. A CI failure is the one an
engineer most often cannot reproduce locally, and it is exactly there that the per-frame
source context and argument values `--tb=long` printed are gone; re-running the job is the
only recovery. That is the trade: a run whose cause is buried under thousands of tracebacks
is unreadable at any level of per-frame detail, and the readable-but-thinner run is the
better failure mode. A CI-only `--tb` override would be a second mechanism doing the job of
the first, which is what the last rejected option below argues against.

A failure whose diagnosis needs a full frame or an assertion diff now takes a second command.
That is the trade accepted: it taxes the uncommon case rather than the common one, where
`--tb=no` would have taxed every single-failure run. The escalation is one recipe away and
its output is now readable, which it was not before.

The escalation no longer runs the gate's topology. A failure caused by xdist itself —
worker-scoped container resources, cross-worker contention, worksteal ordering, the class
#2063 was — can pass under `just test-verbose <paths>` and keep failing under `just test`.
The fallback is direct pytest with the gate's parallelism and verbose output
(`uv run python -m pytest <paths> -n auto --maxprocesses=16 --dist worksteal -vv`); the
justfile comment and `AGENTS.md` both name it, because the trap is that escalation looks
green.

The `test` and `test-verbose` recipes no longer share one variable, so a reviewer must check
that a new selection flag goes into `_TEST_MARKERS` rather than into one recipe. That is the
price of letting the two differ on parallelism at all; the alternative was a shared variable
that forced them to agree on something they should not agree on.

CI and pre-push output shrink, so a workflow-log excerpt quoted in an older issue will not
match a fresh run byte for byte.

## Considered & rejected

**`--maxfail=N`** — the option this work originally leaned toward. It truncates the failure
set rather than the per-failure cost: you stop learning which tests broke, which is the one
thing a mass failure most needs to tell you. Its bound is also loose and confusing under
xdist — `N x workers` as a *ceiling*, not a floor, because workers that never hit a failure
do not stop. `--maxfail=1` produced 2 failures on a 10-failure run, not 16.

**`--tb=no`** — cheapest (~0.35 KB per failure) and rejected on the common case. It leaves
only `FAILED <nodeid>` lines, so the single-failure run — by far the most frequent red run —
would need a second command to learn anything at all. `--tb=short` keeps that case
self-service and still costs a quarter of the pathological tail.

**`--tb=line`** — one line per failure with the exception message. Between `short` and `no`,
and it loses the call chain, which is exactly what distinguishes a broken fixture from a
broken assertion when many tests fail at once. That distinction is the case this ADR exists
for.

**Leaving `just test` alone and documenting the escalation** — correcting only the false
prose. It is the independently valuable half of this change and it landed, but on its own it
leaves the gate CI runs unbounded and relies on every agent choosing to bound it by hand,
which is what did not happen.

**Putting `--tb=short` in `addopts`** — it would reach bare-pytest invocations too, including
the ones a developer types deliberately to see a full traceback, and the image-smoke CI step
that runs pytest with `--no-project`. The flag belongs to the recipe whose output budget it
governs, for the same reason `--maxprocesses` lives in the justfile rather than in `addopts`.

**Keeping one `_TEST_SELECT` and appending `-p no:xdist`-style overrides in `test-verbose`** —
a second mechanism that cancels the first, and it depends on flag-precedence behaviour that
is not obvious from reading the recipe. Splitting the variable states the seam directly.
