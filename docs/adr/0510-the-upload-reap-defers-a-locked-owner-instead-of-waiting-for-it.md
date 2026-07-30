# 0510 — The upload reap defers a locked owner instead of waiting for it

## Status

Accepted (2026-07-30)

## Context

`repair_abandoned_uploads` selects every past-deadline `upload_manifests` row and reaps the owners
one at a time, in a serial `for` loop, on one pooled connection. The first thing
`_claim_abandoned_prefix` does for each owner is take that owner's advisory lock with
`advisory_xact_lock`, which **blocks** — its docstring says so: "Blocks until any current holder's
transaction ends".

The holder that matters is the chunked `complete_build`. It takes `LockScope.RUN` before its
reassembly and deliberately holds it for the rest of the request, so on a multi-GiB upload the lock
is held for as long as the reassembly takes. When the reaper reaches that owner it parks inside
`_claim_abandoned_prefix` for that entire duration — and because the loop is serial, so does every
*remaining* candidate. The blocking is not scoped to the contended owner: it is head-of-line, and it
propagates one level further out, because `reconciler/loop.py`'s `_run_repair_plan` also runs its
repairs serially and keeps one pooled connection checked out across the whole call. No
`lock_timeout` is set on any reconciler connection, so the wait is unbounded.

The result is the defect in #1554: expired upload windows belonging to unrelated Runs and
investigations go unreaped for the duration of one slow finalize, their uncommitted objects linger
past their deadline, and the repairs sequenced after the reap in the plan are delayed with them.
This was recorded as a residual in [ADR-0448](0448-enforce-upload-deadline-at-run-finalize.md) §2
and is not new: it predates the #1534 / #1552 work that surfaced it.

[ADR-0502](0502-a-write-lease-closes-the-orphan-sweep-delete-race.md) already introduced the
mechanism this needs — `try_advisory_xact_lock`, the non-blocking sibling — for a caller "whose
correct response to contention is to do nothing and try again later rather than to wait". The upload
*orphan* sweep uses it, and since [ADR-0509](0509-upload-reap-sweep-rechecks-under-the-owner-lock.md)
so does the reaper's own phase 2. Phase 1 is the last acquisition on this path that still waits.

## Decision

**1. `_claim_abandoned_prefix` attempts the owner lock instead of waiting for it.**
It calls `try_advisory_xact_lock`, and on refusal returns without reading or writing anything. The
candidate is *deferred*: the manifest row is left exactly as found — still past its deadline, which
is the predicate the candidate select uses — so the next pass re-derives it. The loop continues to
the next candidate immediately, which is the whole of the fix: contention now costs the contended
owner one pass instead of costing every owner the holder's runtime.

**2. Deferred, declined and reaped are three distinct owner outcomes.**
`_claim_abandoned_prefix` returns a `_Claim` and `ReapOutcome` gains a `deferred` flag, because
"took the lock and found no past-deadline row" and "never took the lock" are opposite facts about
what the next pass will find. A decline is final for that owner — the window it would have reaped no
longer exists. A deferral is not. Collapsing them into a bare `reaped=False` would leave the pass
unable to report the one of the two that can starve.

A deferral is neither a reap nor a failure. It does not count toward the repair's return, it does
not feed ADR-0453 §3's end-of-pass raise, and it does not trip §4's brake on claiming further
candidates — the same treatment ADR-0509 §4 gives a declined *key*, for the same reason: the guard
working is not a fault. Feeding it into the brake would be strictly worse than blocking, because one
long-running finalize would then stop the pass claiming the rest of the backlog.

**3. The deferral is reported, at the owner and at the pass.**
Per owner, one `INFO` naming the owner. Once per pass when any owner was deferred, one `WARNING`
carrying the deferred count, the candidate count, and the **oldest** deferred candidate's age past
its deadline. The age is `now() - deadline` computed by Postgres in the candidate select, never from
a Python clock, which does not share the database session's timezone.

The summary exists because the property that makes deferring safe is also what makes its failure
mode invisible. Every individual pass looks locally fine: nothing failed, nothing was lost, and the
row is still there. An owner whose lock is *never* free is therefore never reaped and never
complained about. There is no per-owner state anywhere in the reconciler to count consecutive
deferrals against, and the age past deadline needs none — it is monotonic in exactly the situation
that matters, so a starved owner appears as a number that grows pass over pass while a healthy
contended one appears once and stops. This is the log line and the counter
[ADR-0453](0453-row-first-upload-reap.md) asked a skip to carry.

**4. The sweep stays serial. #1554 is closed without fanning out.**
The issue offered two directions, bounded concurrency across owners or skip-and-continue. The stall
it reports is caused by *waiting*, not by serialism: with the wait removed, a pass that would have
blocked for minutes completes in the time its uncontended owners take. Fan-out is an independent
throughput change that would need one pooled connection per worker, a `_run_repair_plan` change, and
a share of a ten-slot pool that other repairs draw on — cost with no defect behind it. ADR-0509
§Consequences' constraints on any future fan-out stand unchanged and unconsumed.

## Consequences

**ADR-0509 §Consequences' "phase 1 still blocks" is superseded.** That paragraph justified the
asymmetry between the phases on the ground that "a reap that gave up on a contended owner would
never claim it — the manifest row is the pass's only record that the window is past its deadline".
The premise does not hold. A deferral does not consume the row; it does not read it. The row remains
and remains past its deadline, so it is re-selected on the next pass, thirty seconds later. What
ADR-0509 correctly identified is that a deferral must not *silently* forgo the claim, which is what
decision 3 addresses. The phases are now symmetric: neither waits, for the same reason, and the
asymmetry ADR-0509 described as deliberate is gone rather than inverted.

**A contended owner's window lives one pass longer, and possibly many.** A window whose owner is
locked at every pass boundary is never reaped, and its uncommitted objects stay in the store. What
bounds this is that the holders are bounded operations — a finalize, a `capture_traffic` PUT,
another reaper — not indefinite ones, so a free moment arrives in ordinary operation.

The second collector does **not** bound it, and this ADR does not claim it does.
`repair_leaked_upload_objects` (ADR-0455) drains rowless objects under these roots once past
`orphan_grace + upload_ttl`, and ADR-0509 §4 rests on it for a declined *key* — but since ADR-0502
that sweep takes the same owner lock with the same `try`, so whatever holds the lock across the
reaper's pass holds it across the sweep's too. An owner whose lock is genuinely never free is
therefore skipped by both deleters, not caught by the second. That exposure is not new — it is the
one ADR-0502 accepted for the orphan sweep and ADR-0509 §4 extended to a declined key — but this
change is what puts an owner's whole window behind it rather than individual keys, and it is the
reason decision 3's pass summary is a `WARNING` with a growing age rather than a line at `INFO`.
Nothing in the tree collects an object whose owner lock is held forever; the summary is the signal
that a human has to.

**A pass can now return 0 with candidates outstanding, and that is not an error.** Anything reading
the `abandoned_uploads` count as "the backlog was drained" would be wrong. Nothing does today — the
count feeds ADR-0190 group A as a repair tally, and the group-E error counter is fed only by a raise
— but the count and the backlog were previously equal in the absence of failures and are no longer.

**The two lock helpers are now used consistently on this path.** Every acquisition in the reap and
the orphan sweep is a `try`, so no reconciler repair can be parked behind a foreground request
holding an owner lock. `advisory_xact_lock` is no longer imported by `reconciler/cleanup/uploads.py`.

**Cost.** One `pg_try_advisory_xact_lock` in place of one `pg_advisory_xact_lock`, and one extra
expression in the candidate select. The deferred branch does strictly less work than the blocking
one did: it opens a transaction, runs one statement, and commits.

## Considered & rejected

**Bounded concurrent fan-out across owners.** The issue's other direction, and the one ADR-0509
§Consequences anticipated. It removes the head-of-line stall too — a blocked worker no longer holds
up the others — and it raises throughput on a large backlog. Rejected as the fix for this defect
because it does not remove the wait, only hide it behind N-1 other workers: a holder that outlives
the pass still consumes a worker, a connection and a pool slot for its whole duration, so at N
contended owners the stall returns in full. It also costs one of ten pooled connections per worker,
which other repairs draw on, and a `_run_repair_plan` change outside this defect's blast radius.
Deferring is the smaller change and the complete one; the two are independent and fan-out remains
available on its own merits.

**Blocking with a `lock_timeout` on the reconciler connection.** Bounds the wait without any code
change to the reap. Rejected: it converts contention into a `psycopg` error, so a routine finalize
would present as a repair failure in ADR-0190's group-E counter, and the timeout is a number nothing
in the tree bounds — too short and it is `try` with extra steps and a spurious error, too long and
the stall is merely capped. It would also apply to every statement on that connection, not just this
acquisition.

**Deferring by ordering: sort candidates so contended owners come last.** No wait for the owners
ahead of the contended one. Rejected — there is no way to know an owner is contended without
attempting its lock, so this is `try` plus a retry pass, and the contended owner at the end still
blocks the pass's tail and the repairs after it.

**Counting consecutive deferrals per owner to escalate a starving one.** A precise starvation
signal. Rejected: it needs per-owner state that survives passes, which is a table, a migration and a
cleanup obligation for a signal the age past deadline already carries — that age grows exactly when
consecutive deferrals accumulate, is already in the row, and costs one expression in a select
already being run.

**Reporting the deferral only at `INFO`, with no pass summary.** Matches how a declined key is
logged in ADR-0509 §4. Rejected because the two are not equivalent: a declined key is drained by the
orphan sweep on a bounded schedule, whereas a deferred *owner* is the reaper's own obligation
carried forward indefinitely with nothing counting how long. The pass summary is the only place the
deferral is visible as a number rather than as a line to be grepped for.

**Raising at the end of a pass that deferred any owner.** Puts the deferral in front of an operator
through ADR-0190's group-E error counter rather than a log. Rejected: a deferral is a normal outcome
of a finalize overlapping a pass, so this would report a healthy deployment as erroring every thirty
seconds, and — because `_run_repair_plan` records a raising repair as failed — would misattribute a
correctly-working guard to a broken repair.
