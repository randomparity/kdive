# 0002 — The ADR-0482 skew preflight grades one worker, not every worker

## Status

Open
review-by: 2027-01-30

## Concern

`readyz_urls` in `tests/integration/live_stack/skew.py` builds its probe set from
`PROCESS_DEFAULT_PORTS` alone — one URL each for `server`, `worker` and `reconciler` — and
collapses to a single `stack` URL when `KDIVE_HEALTH_BIND_ADDR` is set explicitly. It has
no notion of a worker *count*.

`KDIVE_WORKER_COUNT` (added alongside this record) starts N worker processes. Worker 1
keeps the registered `worker` default, 9465, precisely so the preflight still finds it;
each additional worker binds above it. So on a two-worker stack `probe_stack_skew` grades
worker 1 and is structurally blind to worker 2. It returns `fresh` for a stack whose second
worker was started from a different checkout.

That is the scenario ADR-0482 exists to catch, stated in its own Context: a spine driven
against a worker that predated its own fix, and the resulting green read as a pass. The
multi-worker case reopens exactly that hole for every worker past the first. Producing it
needs no exotic sequence — `up.sh`, a rebase, then a second `up.sh` that fails partway,
leaves workers at two different commits with a `fresh` verdict.

Exposure is bounded: CI runs the default single worker, so only a `live_stack` run against
a deliberately multi-worker stack is affected. That is why this is a real gap rather than a
release blocker.

## Why deferred

The fix belongs to the skew preflight, not to the worker-count knob. `skew.py` is the
ADR-0482 surface — its probe set, its verdict vocabulary, and the policy table that grades
them — and #1551's charter is the live-stack shell library and the live-testing runbook.
Extending the probe set means deciding how the preflight learns the worker count (read the
env var it cannot see from a bare `status.sh`, scan the port range, or enumerate the process
table), which is an ADR-0482 design question with a wrong answer available in each
direction: an env-var read reproduces the same blind spot one layer up, and a port scan
bakes this change's port arithmetic into the preflight.

This change does not make the concern worse for any existing configuration. The default
stack is one worker on 9465, probed exactly as before.

## Non-regression boundary

- Worker 1 must keep the registered `PROCESS_DEFAULT_PORTS["worker"]` port whenever
  `KDIVE_HEALTH_BIND_ADDR` is unset, so the preflight's existing single-worker coverage is
  never lost. `extra_worker_health_bind` returns empty for index 1 for this reason, and
  `tests/scripts/test_live_stack_scripts.py` asserts it.
- Extra-worker ports must stay disjoint from every value in `PROCESS_DEFAULT_PORTS`, so an
  extra worker can never answer a probe intended for the server or the reconciler and turn
  a blind spot into a *wrong* grade.
- The default stack must remain exactly one worker, so no CI or operator run silently
  acquires unprobed processes.

## What would resolve it

Give the preflight the whole worker set. Either have `readyz_urls` enumerate the extra
workers, or have `probe_stack_skew` compare the number of URLs it graded against the number
of live `kdive worker` processes and return `unknown` (never `fresh`) when the process table
shows more workers than it probed — the second is strictly safer, since it fails toward the
verdict ADR-0482 already defines for "cannot tell".

Done when a stack brought up with `KDIVE_WORKER_COUNT=2`, whose second worker runs a
different commit, does not report `fresh`.

Until then the mitigation is in the live-testing runbook's fetch-lock contention arm, which
states the gap and points the operator at the `=== build stamps ===` block — that block
prints a row per worker log and does show the divergence.

## Provenance

target: tests/integration/live_stack/skew.py
target: scripts/live-stack/lib.sh
Found by the `/review-loop` adversarial pass on the #1551 branch on 2026-07-30, while adding
`KDIVE_WORKER_COUNT` to the local live-stack bring-up. Not a defect the knob introduced in
any existing configuration — a pre-existing single-worker assumption in the preflight that
the knob makes reachable for the first time.
tracker: #1551
