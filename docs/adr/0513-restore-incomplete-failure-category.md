# 0513 — The reconciler's stalled-restore repair records `restore_incomplete`, not the default

## Status

Accepted (2026-07-30)

## Context

#1560 was filed against `systems.list`, and its literal premise no longer holds. It says every
`failed` System in a listing renders `infrastructure_failure` because `system_envelope` gets no
failing job on the list path, and proposes a `LATERAL` join plus a supporting index to fetch each
page's failing jobs in one set-based query. ADR-0492 (migration 0083) has since put
`failure_category` on the `systems` row that `list_systems` already `SELECT`s, and
`_resolve_failure_verdict` reads it in preference to the job's. The list path already reports the
truthful category for every System that records one, at no query cost. Building the join would be
dead code.

What survived the issue is the case ADR-0492 §3 disclosed and left open, and it is the one case a
join could never have fixed. `repair_stalled_restoring_systems`
(`reconciler/repairs/systems.py`) drives a stalled `restoring` System to `failed` via
`SYSTEMS.update_state` and records no category, so the row lands on NULL. ADR-0492 §3 listed it
first among "three real paths that still record no category on the System" and pointed at the
job-lookup fallback for all three. That pointer does not reach this one:

- The repair's *precondition* is that no `restore` job for the System is still active. In the
  common shape the job is `failed` with `lease_expired` — bookkeeping about an expired lease, not
  a verdict on the guest — and in the `attempt < max_attempts` shape it is re-dispatched, hits
  the terminal-state early return, and ends `succeeded` with `error_category` NULL, which
  ADR-0454 §2 attributes nothing from. Neither is the reason the System failed.
- There is no exception to classify. This is not a handler catching a provider error; it is a
  sweep observing that a transition can never complete.

So a `failed` System from this path reports `infrastructure_failure` — "an unclassified failure
in the underlying infrastructure layer", which is not what happened — and, because retryability
is a pure function of the category (ADR-0118), `retryable: true`. That is the actively harmful
half. A half-reverted guest is indeterminate by the repair's own reasoning (its docstring is why
it resolves to `failed` and never back to `ready`), and a `failed` System is fenced from every
lifecycle op, so there is nothing for the agent to retry. The envelope tells it to try anyway.

The condition is also not rare enough to leave: it is the R3 limbo ADR-0378 built the repair for
— any worker killed mid-revert produces it.

## Decision

### 1. A distinct category, `restore_incomplete`

`ErrorCategory` gains `RESTORE_INCOMPLETE = "restore_incomplete"`, and
`repair_stalled_restoring_systems` stamps it on every System it resolves.

The cheaper option — have the repair write `infrastructure_failure` explicitly, so the value is
recorded rather than inferred — was considered and rejected (§Considered & rejected). It fixes
the provenance and none of the meaning: the operator still cannot tell a System whose restore was
abandoned from one whose host actually faulted, and `retryable` stays wrong, which is the part
that misdirects an agent.

The name states the System-facing fact (the revert did not complete, so the disk is between two
defined states) rather than the queue-internal cause (a worker died). That is what determines the
recovery: the System is not recoverable, so tear it down and provision a replacement. Its
snapshots do not survive it — `teardown_handler` calls `delete_snapshots_for_system` and a
best-effort `delete_all` — so the replacement starts with none, and the served errors guide says
so rather than implying the checkpoint can be carried across. `symbol_not_found` (ADR-0307) is the
precedent for a category this narrow — one condition, one raise site, added because the general
category it was flattened into carried the wrong retryability.

### 1a. The repair records its verdict only when the job explains nothing

Stamping unconditionally would be wrong, and this is the one place this ADR's shape differs from
ADR-0492's. `_record_system_failure` classifies a **caught exception**, so its verdict is the
best information anyone has, which is why ADR-0492 §3 gave a recorded category precedence over
the job's. This repair's evidence is an **absence** — no `restore` job can run again — so it can
be holding strictly weaker information than the job row, and the same precedence would let
`restore_incomplete` displace a real reason. Two live paths produce one:

- `restore_handler` calls `load_payload` and `_bind_snapshotter` *before* the `try` that routes
  `restoring -> failed`. `_require_snapshotter` raises `CategorizedError(CONFIGURATION_ERROR)`
  for a provider with no snapshot plane, and `load_payload` raises `PayloadValidationError`
  (`configuration_error`). Either dead-letters the job with its own category and never touches
  System state, leaving the System `restoring` for this repair to find.
- `_record_system_failure` is best-effort by contract, so a swallowed write leaves the System
  `restoring` beside a job that holds both the real category and the redacted failure message.
  Overwriting there would put a generic category next to a specific `detail` in one envelope.

So `_restore_limbo_category` consults the newest terminal `restore` job first and records
`restore_incomplete` only when that job explains nothing: absent, carrying no category, or
carrying `lease_expired` — which `repair_abandoned_jobs` stamps unconditionally over a job that
never recorded a reason of its own, and which is a statement about the lease rather than about
the guest. Otherwise the column is left NULL, which is exactly the ADR-0454 job fallback this
repair should defer to. The System still leaves `restoring` either way; only the stamp differs.

The lookup is a keyed read on the `jobs.payload->>'system_id'` expression index (ADR-0491,
migration 0082) and runs once per System actually recovered, inside the transaction that already
holds that System's advisory lock. It is not the per-row list-path lookup ADR-0454 §4 declined —
it runs on the write path, on the failure path only, for the Systems this sweep is already
mutating.

### 2. `retryable: false`

Registered `False` in `RETRYABLE_BY_CATEGORY`. Three reasons, and the first alone is sufficient:

- The System is terminal `failed`. `systems.restore` requires `ready`, so a bare re-invocation of
  the identical request cannot succeed — the definition ADR-0118 gives the flag.
- The guest is indeterminate. Even if the fence were lifted, no operation on it has a defined
  starting point, which is the repair's own reason for refusing to return it to `ready`.
- The taxonomy's stated bias is non-retryable when transience is ambiguous (#430), and nothing
  here is ambiguous in the other direction.

`RETRYABLE_BY_CATEGORY` is the single table behind both retry seams (ADR-0483): the envelope's
`retryable` and the queue's dead-letter-vs-redispatch choice. The queue side is inert for this
value today — nothing raises it as a `CategorizedError`, so no job can fail with it. Should a
future raise site emit it, dead-lettering on the first attempt is the behaviour §2's first two
bullets already argue for, so the entry is correct rather than merely unreachable.

### 3. Migration 0086 widens all four CHECK constraints

`failure_category` is not a Postgres enum. It is `text` with a named CHECK per column, and one
Python enum backs four of them: `runs_failure_category_check`, `jobs_error_category_check`,
`allocations_failure_category_check`, `systems_failure_category_check`.

Only `systems` can hold `restore_incomplete` today — the reconciler writes it straight to that
column and no failure path carries it onto a Run, Job or Allocation. All four widen regardless,
because `test_migrate.py`'s `CHECK_ENUMS` ties each constraint to the whole of `ErrorCategory`,
not to the subset its table can observe; a value admitted by only one is a red gate. Migration
0059 made exactly this call for `symbol_not_found`, a value only a synchronous debug op could
produce, and widened the three constraints that existed then. 0086 follows it, drop-and-recreate
so the constraint names stay stable, and adds the fourth (`systems`, added by 0083 after 0059).

Migration 0085 is reserved by another in-flight change in the same campaign; the runner sorts by
version string and requires no contiguity, and the schema already has a 0073 gap.

### 4. One writer for the column

`_record_failure_category` moves out of `jobs/handlers/systems.py` to
`db/repositories.record_system_failure_category`, and both failure paths call it. The column is
now written from two subsystems, and its non-obvious safety argument — deliberately unguarded on
`state`, because the caller's transaction holds the row under `FOR UPDATE` and `FAILED` has no
outbound edges — has to be true at both. Two copies of that argument is how it stops being true
at one of them, on a column an agent reads to decide whether to retry.

### 5. This amends ADR-0492, and closes what #1560 can still close

ADR-0492 narrowed the ADR-0454 §4 list-path gap to "a System whose failure predates the column,
or whose category is only on its job". Its §3 named this repair as one of three NULL-category
paths and left it to the job fallback. This ADR removes it from that list for the shapes where
the fallback has nothing to offer — not by making the fallback reach them, but by giving the path
a verdict of its own — and, per §1a, leaves it on that list for the shapes where the fallback has
the better answer. Two other shapes remain and are deliberately untouched: rows written before
migration 0083, and a non-`CategorizedError` escape that dead-letters the job without reaching
`_record_system_failure`. Both have an attributable job, so both are what the job fallback is for.

The issue's own suggested direction is not built. Its cost analysis was correct for ADR-0454's
world and is moot in ADR-0492's: the `LATERAL` join and the partial index it wanted priced first
would buy the list path a lookup it no longer needs.

## Consequences

- `restore_incomplete` is a new agent-visible wire string. It reaches agents through
  `systems.get` and `systems.list` (the `error_category` field and the derived `retryable`), and
  through the served errors guide (`resource://kdive/docs/guide/errors.md`), which gains a row
  and a recovery pattern naming `systems.teardown`. No tool schema, argument, RBAC rule or
  exposure entry changes.
- An agent that previously read `infrastructure_failure` / `retryable: true` for a stalled
  restore now reads `restore_incomplete` / `retryable: false`. An agent branching on the literal
  string `infrastructure_failure` stops matching this System. That is the intended change and the
  categories are documented as a closed set an agent reads rather than exhausts, but it is a
  behaviour change on a live surface, not an addition beside the old answer.
- `test_get_system_after_the_real_abandoned_job_sweep` is re-baselined: that envelope reported
  `lease_expired` with `ABANDONED_JOB_SYSTEM_FAILURE_DETAIL`, and now reports
  `restore_incomplete` with `RECORDED_SYSTEM_FAILURE_DETAIL`, because the System's own verdict
  outranks the job's (ADR-0492 §3). The job is still cited by `failing_job_id`; only the category
  it lends is superseded. `retryable` was already `false` for `lease_expired`, so the fix here is
  to the reason rather than to the advice.
- `test_systems_list_keeps_the_flattened_category` keeps pinning the ADR-0454 §4 gap, but its
  System is now seeded directly: the reconciler no longer produces a category-less `failed` row,
  so the test can no longer describe itself as covering that path.
- Existing rows are untouched. There is no backfill: a System already resolved by this repair
  keeps its NULL column and reports exactly what it reports today. The correlation is not
  recoverable after the fact anyway — the evidence the repair acted on is the *absence* of an
  active job.
- One new query and one `UPDATE`, both on the failure path only, for a System this sweep is
  already mutating under its advisory lock, and the query is served by an existing index. No new
  index, config setting or dependency.
- **Migration 0086 holds an ACCESS EXCLUSIVE lock while it revalidates four tables**, including
  the append-only `jobs`, and `apply_migrations` runs every pending migration in one transaction,
  so the lock spans the deploy step. This is inherited from 0059 and 0083 rather than introduced
  here, and is stated so the next category addition prices it: at a `jobs` size where the scan
  matters, the shape to reach for is `ADD CONSTRAINT … NOT VALID` followed by a separate
  `VALIDATE CONSTRAINT`, which `CHECK_ENUMS` reads identically because it inspects
  `pg_get_constraintdef`.

## Considered & rejected

- **Have the repair stamp `infrastructure_failure` explicitly.** One line, no migration, no new
  wire string, and it fixes the provenance — the value would be recorded rather than fallen back
  to. Rejected because provenance was not the complaint. The operator still cannot separate an
  abandoned restore from a genuine host fault, and `retryable` stays `true` on a System that is
  terminal and fenced, so the envelope keeps giving an agent advice it cannot act on.
- **Build the issue's `LATERAL` join and its supporting partial index.** The issue's stated
  direction. Moot: ADR-0492 put the category on the row `list_systems` already reads, so the join
  would resolve, at the cost of a scan, a value the list path has in hand — and would still
  return nothing for this repair's Systems, which have no failed job to join to.
- **Reuse `stale_handle` or `configuration_error`.** `stale_handle` says the object is gone and
  the handle invalid; this System exists, is readable, and is the thing being reported on.
  `configuration_error` blames the request, and the request was valid — the restore was accepted
  and had begun. Both would be non-retryable, so both would fix the advice while misstating the
  reason, which is the failure mode #1550 and ADR-0492 exist to stop.
- **A general `recovery_abandoned` covering every reconciler limbo repair.** Speculative: the
  sibling repairs do not need it. `repair_stalled_crashing_systems` resolves *forward* to
  `crashed` and reports no failure at all, and `repair_stalled_creating_snapshots` fails a
  `snapshots` row, which has no `failure_category` column. A category shared by one caller is a
  narrow category with a broad name.
- **Widen only `systems_failure_category_check`.** The only constraint whose column can receive
  the value. Rejected: `CHECK_ENUMS` asserts every one of the four constraints admits every
  `ErrorCategory` member, so this is a red gate, and the tie it enforces is what stops SQL and
  the enum drifting apart in either direction.
- **Backfill the column for Systems this repair already failed.** The repair's evidence is that
  no active restore job exists — an absence, not a row — so a migration could only guess from a
  System being `failed` beside a terminal `restore` job, which is also the shape of a restore
  that failed inside its handler for a real provider reason. It would relabel those wrongly.
- **Record a failure *message* on the System as well.** ADR-0492 already declined this for the
  handler path (redaction is the worker's boundary). Here there is no message to record: no
  exception was caught, and the category is the whole of what this path knows.
- **Stamp `restore_incomplete` unconditionally and let ADR-0492 §3's precedence stand.** One
  line, no query. Rejected in §1a: this writer's evidence is an absence, so on the paths where
  `restore_handler` raises before its `try` — or where `_record_system_failure`'s best-effort
  write is swallowed — the job holds the real category and the generic verdict would displace it.
  ADR-0492's precedence rule was written for a writer that classifies a caught exception, and
  extending it to one that classifies an absence is not what it decided.
- **Have the reconciler re-derive ADR-0454's full attribution** (newest terminal job across
  `SYSTEM_FAILING_JOB_KINDS`) instead of consulting the restore job. A second copy of the
  attribution rule, in a subsystem that must not import the MCP view layer, to decide a write
  whose only job is to not clobber. The repair's own precondition is about the `restore` job, so
  that is the job it asks about; where some other kind's job wins the envelope's attribution, the
  ordering residual is the one ADR-0492 Consequences already disclosed.
