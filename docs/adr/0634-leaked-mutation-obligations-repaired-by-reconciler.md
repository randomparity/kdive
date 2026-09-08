# 0634 — Leaked System mutation obligations are repaired by a reconciler lane

## Status

Accepted (2026-09-08)

## Context

ADR-0629 stopped new mutation-obligation leaks on the System-teardown reclaim path and repaired
none, recording that "Obligations already leaked by #2302 on live Systems are not repaired by this
change". Each such row keeps `mutation_discharged_at IS NULL`, so `retained_owners`
(`../../src/kdive/db/remote_module_attempt_obligations.py:531-561`) keeps returning its attempt and
the module-volume sweep never reclaims that attempt's `source.ext4` and `scratch.ext4`. Nothing
raises and nothing logs at a level an operator sees, and `systems.teardown` returns `torn_down` from
its terminal short-circuit (`../../src/kdive/mcp/tools/lifecycle/systems/admin.py:451-462`) without
enqueueing a job, so no retry reaches the discharge.

**Which paths can leave the pair.** verified: `rg -n TORN_DOWN 'src/kdive/**/*.py'` and
`rg -n "torn_down" src/kdive/db/schema/*.sql` return five live writers of that state, plus
`0080_retire_defined_system_state.sql:51`, which predates the obligations table (created at 0126)
and cannot leak. Four of the five cannot produce the pair:

- `../../src/kdive/db/schema/0147_external_boot_system_teardown.sql:440-443` discharges every open
  obligation for the System and sets `torn_down` in the same statement group of one
  `SECURITY DEFINER` function; `retained_quarantine` returns at lines 404-409 before either.
- `../../src/kdive/db/schema/0122_external_boot_authority.sql:1453` sets `torn_down` in
  `commit_external_boot_authority_result`, which is still live because 0132 and 0147 patch it
  through `pg_get_functiondef` + `replace` rather than redefining it;
  `0132_external_boot_terminal_mutation_discharge.sql:16-22` splices the discharge in immediately
  after that same `UPDATE`, in the same transaction.
- `../../src/kdive/db/schema/0149_authority_owned_system_provisioning.sql:1154-1170` refuses with
  `cleanup-required` while an undischarged obligation exists.
- The worker receipt-consumption path at `0149:924` carries no such gate and does not need one: its
  caller (`../../src/kdive/jobs/handlers/system_authority.py:222-229`) discharges first.

The fifth is `../../src/kdive/jobs/handlers/systems.py:700-706`, which commits `torn_down` in its
own transaction, runs the provider teardown, and only then discharges through
`reclaim_system_core_after_provider_teardown(..., discharge_mutation_obligations=True)`. Anything
that ends the job between the two leaves the pair. This is the #2302 population and the ordering
defect's continuing source; fixing that ordering is separate work and out of scope here.

**Why that inventory is not the whole claim.** It rules out a terminal transition committing over an
open obligation. It says nothing about an obligation being *opened* on an already-terminal System,
and the sole live opener does not close that gap itself: `public.open_external_boot_remote_module_attempt`
(`../../src/kdive/db/schema/0146_external_boot_remote_module_attempt.sql:110-130`) fences on the
authority, the worker incarnation, the job lease, the journal head, and an activation in `preparing`
with `NOT cleanup_complete` — never on `systems.state`, and it takes no System advisory lock. So the
property "no obligation is legitimately open on a torn-down System" is carried by those activation
fences, which every terminal path clears in the transaction that writes `torn_down`, and by the
ordinary teardown handler refusing to run under a restricting activation
(`../../src/kdive/jobs/handlers/systems.py:684-696`). It is not a property of the obligations table.
A change that lets an activation stay `preparing` across a terminal System transition moves this
invariant, and this record is where a later reader should find that out.

**The window that has to be excluded.** Both teardown families enqueue at the same key —
`_teardown_dedup_key(system_id)` is `f"{system_id}:teardown"`, used by `enqueue_control_teardown`
and `enqueue_preactivation_teardown`
(`../../src/kdive/services/systems/authority_owned.py:137-138,196-199,223-229`) — and `jobs.dedup_key`
is `NOT NULL UNIQUE` (`0001_init.sql:166,169`), so one row carries the whole history of a System's
teardown. But job *state* alone does not bound the window. verified: `RUNNING -> CANCELED` is a legal
transition (`../../src/kdive/domain/capacity/state.py:303-304`), `teardown` is not in
`PLATFORM_INTERNAL_JOB_KINDS` (`../../src/kdive/domain/operations/jobs.py:95-97`), `jobs.cancel`
fences only the authority-owned preactivation teardown
(`../../src/kdive/mcp/tools/jobs.py:312-320`), and `rg -n 'canceled|CANCELED' src/kdive/jobs/worker.py`
returns nothing — so an operator cancel writes `canceled` while the handler keeps running to
completion through `provisioner.teardown`. A predicate reading only `queued`/`running` goes false for
that entire remaining teardown.

**There may be no backlog at all, and the lane is still worth having.** The affected-row count
#2326 asks for cannot be taken from a development checkout, so the shape was chosen without it, by
operator decision (#2326, 2026-09-08). Say plainly what that leaves: the only path that produces
`torn_down` beside an open obligation is the ordering defect above, which is explicitly out of scope
here, so against a database whose teardowns have all completed this lane discharges nothing and
returns 0. Its justification is the leaks that defect keeps producing, not a backlog anyone has
demonstrated. A repair that claimed a population it cannot show would be the worse record.

## Decision

Repair the rows with a standing reconciler lane, `repair_leaked_mutation_obligations` in
`../../src/kdive/reconciler/repairs/systems.py`, and write no data migration.

The lane selects each System in `torn_down` carrying an obligation with
`mutation_discharged_at IS NULL` and **no teardown job at that System's dedup key that is either
`queued`/`running` or terminal within a settle window** (`_TEARDOWN_SETTLE`, 15 minutes), then
discharges under the System advisory lock through
`RemoteModuleAttemptObligationRepository.worker_discharge_system_mutation_obligations`, which is
ADR-0629's `SECURITY DEFINER` function. Each candidate is isolated so one failure does not starve
the rest of the batch, matching `repair_orphaned_systems` in the same module. It is registered in
the reconciler's repair catalog after `abandoned_jobs`, which is what makes a stuck teardown
repairable at all: a teardown whose worker died keeps its job `running` with a lapsed lease, and a
`running` job defers this repair's candidate indefinitely.

## Consequences

- The repair is idempotent and self-draining. It returns 0 against a database with no leaked rows,
  which is the resting state of every reconciler lane, and it repairs a leak created after deploy
  as readily as one created before it. That is the property a one-shot migration does not have while
  the teardown ordering defect stands unfixed. The first pass after deploy is unbounded and serial,
  so its duration scales with a backlog nobody has measured. The lane it delays is worth naming:
  `module_volume_reap_jobs_enqueued` sits about thirty catalog entries later and is the sweep that
  consumes these discharges, so a long first pass postpones by one interval exactly the work this
  repair exists to unblock. Self-limiting, because pass two sees an empty set; the bounded-batch
  idiom at `0149:939` is the remedy if an operator ever reports one.
- The deferral is keyed on a teardown job existing. A torn-down System with no job row at that
  dedup key is repaired on the first pass that sees it, and that is correct rather than a gap: the
  one path that produces `torn_down` beside an open obligation always enqueues at
  `<system_id>:teardown` first, so a candidate with no such row is one whose job row is already gone
  — an old leak, not an in-flight teardown. An age floor on the obligation's own `created_at` would
  not bound this: that column records when the attempt opened, not when its teardown ran.
- The dead-letter that frees a stuck candidate also defers it once more. `repair_abandoned_jobs`
  moves a lease-lapsed teardown to `failed`, and that write stamps `jobs.updated_at`, so the System
  it frees is repaired a settle window later rather than in the pass that dead-lettered it. The
  catalog ordering buys eventual repairability, not same-pass repair.
- **The settle window is pacing with a stated limit, not a fence.** It bounds the operator-cancel
  window above, and it is measured on the teardown job's own `updated_at` — a row unrelated traffic
  does not write, which is what makes it usable where a window on `systems.updated_at` is not. A
  provider teardown that runs longer than 15 minutes after its job left `queued`/`running` is not
  covered, and nothing serializes this lane against the handler in any case, because the handler
  releases the System lock before its provider call. Underneath that, ADR-0588's sweep still refuses
  to delete any volume whose path backs a live domain, so an early discharge does not by itself
  reclaim storage a running teardown is using.
- The lane's count reaches operators through the existing repairs counter, keyed by its
  repair-kind name, plus a per-System `INFO` log. The count is **obligation rows**, matching the
  kind name, not Systems. No new `ReconcileReport` scalar field is added; the three sibling
  stalled-state repairs carry their counts the same way. That counter is a repair signal, not a
  health signal: a pass whose candidates all failed also returns 0, which is byte-identical to a
  pass with nothing to repair, so the lane emits one `ERROR` naming the failure count when it
  discharged nothing and something failed. Per-candidate isolation would otherwise turn a
  systematic denial — the reconciler login losing its role membership, a statement or lock
  timeout — into exactly the silent failure #2326 was filed about, one level up.
- Migration `0153` was reserved for this change and is deliberately left unused, so the next
  migration takes it.
- The reconciler discharges obligations for a System it did not tear down. It already holds the
  `EXECUTE` grant that permits exactly this write and nothing else (ADR-0629), and the write is the
  same fixed `terminal_escape` statement the teardown path itself would have run.
- A System in `failed` carrying an open obligation is not repaired, and that is a permanent leak
  rather than an open question: `retained_owners` filters only on the discharge columns and never on
  System state, while `repair_orphaned_systems` treats `FAILED` as terminal
  (`_ORPHANED_SYSTEM_TERMINAL_STATES`, `../../src/kdive/reconciler/repairs/systems.py:34`) and so
  never enqueues a teardown for it. #2326's predicate is `state = 'torn_down'` and this record keeps
  that boundary; the sized follow-up is reported to the campaign that dispatched this work.

## Considered & rejected

- **A one-shot data migration, using the reserved number 0153 — alone or beside the lane.**
  verified: the teardown ordering defect that produces the leak is explicitly out of scope for #2326
  and no issue has been filed for it, and `../../src/kdive/jobs/handlers/systems.py:700-706` still
  commits `torn_down` before the discharge, so the population keeps growing after the migration
  runs. A migration also has to encode the in-flight exclusion at deploy time, which is exactly when
  workers are most likely to be mid-teardown. Shipping both adds a second implementation of the same
  predicate to repair rows the lane's first pass repairs seconds later.
- **A settle window on `systems.updated_at` rather than on the teardown job's.** verified: the
  column and its trigger exist (`../../src/kdive/db/schema/0001_init.sql:59-62`), but that trigger
  fires on any update to the System row, so unrelated traffic resets the window. The job row has the
  same trigger (`0001_init.sql:171-172`) and a `UNIQUE` `dedup_key`, and nothing but that System's
  teardown writes it — so the window measures the thing it names.
- **Job state alone, with no settle window.** verified: refuted by the cancel route in Context —
  `RUNNING -> CANCELED` is legal, `jobs.cancel` does not fence the ordinary teardown, and the worker
  never aborts the running handler, so the predicate goes false for the whole remaining teardown.
  This was the first shape of this decision and the review that found it is why the window exists.
- **Encode #2326's second exclusion, for Systems torn down through the external-boot lifecycle
  path.** verified: that caller does pass `discharge_mutation_obligations=False`
  (`../../src/kdive/jobs/handlers/external_boot/lifecycle.py:1078`), but
  `0147_external_boot_system_teardown.sql:440-442` discharges every open obligation for the System
  in the same statement group that sets `torn_down` at line 443, and `retained_quarantine` returns
  at lines 404-409 before either. The path leaves nothing open, so the exclusion would select no
  rows and would assert a property of the schema that the schema does not have.
- **Drop torn-down Systems from `retained_owners` rather than writing a discharge.** verified: the
  read at `../../src/kdive/db/remote_module_attempt_obligations.py:531-561` selects on
  `mutation_discharged_at IS NULL`, and ADR-0588 defines the retained set as the attempts with an
  un-discharged durable obligation. Filtering by System state there would release the volumes while
  leaving the row saying the obligation is open, so the durable record and the sweep would disagree
  and no evidence of why the attempt stopped being retained would exist anywhere.
- **Do nothing.** verified: no existing surface reaches the discharge for an already-torn-down
  System — `systems.teardown` short-circuits at
  `../../src/kdive/mcp/tools/lifecycle/systems/admin.py:451-462` without enqueueing a job, and the
  four other terminal paths above cannot produce the pair — so the rows stay open and their volumes
  stay unreclaimable for the life of the row. An operator command instead of a lane fails the same
  way, one step later: it needs an operator to notice a leak that produces no signal.
