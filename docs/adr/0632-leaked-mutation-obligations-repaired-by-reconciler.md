# 0632 — Leaked System mutation obligations are repaired by a reconciler lane

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

Only one code path can leave a System `torn_down` with an open mutation obligation. Every writer of
that state was read:

- `../../src/kdive/jobs/handlers/systems.py:700-706` commits `torn_down` in its own transaction,
  runs the provider teardown, and only then discharges through
  `reclaim_system_core_after_provider_teardown(..., discharge_mutation_obligations=True)`. Anything
  that ends the job between the two leaves the pair. This is the #2302 population and the ordering
  defect's continuing source; fixing that ordering is separate work and out of scope here.
- `../../src/kdive/db/schema/0147_external_boot_system_teardown.sql:440-443` discharges every open
  obligation for the System and sets `torn_down` in the same statement group of one
  `SECURITY DEFINER` function, and the `retained_quarantine` disposition returns at lines 404-409
  before either statement.
- `../../src/kdive/db/schema/0149_authority_owned_system_provisioning.sql:1154-1170` refuses with
  `cleanup-required` while an undischarged obligation exists, so the reconciler's authority-teardown
  finalizer cannot set `torn_down` over one. The worker receipt-consumption path at line 924 carries
  no such gate, and does not need one: its caller
  (`../../src/kdive/jobs/handlers/system_authority.py:222-229`) discharges before the finalizer runs
  at all.

So the detection predicate needs one exclusion rather than the two #2326 proposes: a teardown in
flight. There is no path that leaves an obligation legitimately open on a torn-down System.

The window that needs excluding sits entirely inside a live `teardown` job. Both teardown families
enqueue at the same key: `_teardown_dedup_key(system_id)` is `f"{system_id}:teardown"`, used by
`enqueue_control_teardown` and by `enqueue_preactivation_teardown`
(`../../src/kdive/services/systems/authority_owned.py:137-138,196-199,223-229`).

The affected-row count #2326 asks for cannot be taken from a development checkout, and the shape has
to be chosen without it.

## Decision

Repair the rows with a standing reconciler lane, `repair_leaked_mutation_obligations` in
`../../src/kdive/reconciler/repairs/systems.py`, and write no data migration.

The lane selects each System in `torn_down` carrying an obligation with
`mutation_discharged_at IS NULL` and no `queued` or `running` job at that System's teardown dedup
key, then discharges under the System advisory lock through
`RemoteModuleAttemptObligationRepository.worker_discharge_system_mutation_obligations`, which is
ADR-0629's `SECURITY DEFINER` function. It is registered in the reconciler's repair catalog after
`abandoned_jobs`, which dead-letters a lease-lapsed teardown job and so is what makes a stranded
candidate visible.

## Consequences

- The repair is idempotent and self-draining. It returns 0 against a database with no leaked rows,
  which is the resting state of every reconciler lane, and it repairs a leak created after deploy
  as readily as one created before it. That is the property a one-shot migration does not have while
  the teardown ordering defect stands unfixed.
- The lane's count reaches operators through the existing repairs counter, keyed by its
  repair-kind name, plus a per-System `INFO` log. That is the signal #2326 records as absent today.
  No new `ReconcileReport` scalar field is added; the three sibling stalled-state repairs carry
  their counts the same way.
- Migration `0153` was reserved for this change and is deliberately left unused, so the next
  migration takes it.
- The reconciler discharges obligations for a System it did not tear down. It already holds the
  `EXECUTE` grant that permits exactly this write and nothing else (ADR-0629), and the write is the
  same fixed `terminal_escape` statement the teardown path itself would have run.
- The exclusion is a predicate on job state, not a fence. The teardown handler releases the System
  lock before its provider call, so nothing serializes this lane against the window; re-reading the
  predicate inside the per-System locked transaction narrows it to the same width every other
  reconciler repair carries, and no narrower.
- A System in `failed` carrying an open obligation is not repaired. #2326's predicate is
  `state = 'torn_down'` and this record keeps that boundary; whether `failed` strands obligations
  the same way is unexamined follow-up work.

## Considered & rejected

- **A one-shot data migration, using the reserved number 0153.** verified: the teardown ordering
  defect that produces the leak is explicitly out of scope for #2326 and no issue has been filed for
  it, and `../../src/kdive/jobs/handlers/systems.py:700-706` still commits `torn_down` before the
  discharge, so the population keeps growing after the migration runs. A migration also has to
  encode the in-flight exclusion at deploy time, which is exactly when workers are most likely to be
  mid-teardown.
- **Ship the migration and the lane together.** judgment: the lane drains the historical backlog on
  its first pass, so the migration would repair only rows the lane repairs seconds later, at the
  cost of a second implementation of the same predicate.
- **A settle window on `systems.updated_at` instead of the job predicate.** verified: the column and
  its trigger exist (`../../src/kdive/db/schema/0001_init.sql:59-62`), so a window is
  implementable. It makes correctness depend on a tuning constant that has to exceed the longest
  provider teardown, and the trigger fires on any update to the row, so an unrelated write resets it.
  The job predicate reads the actual condition instead, and it is the idiom
  `repair_stalled_crashing_systems` already uses.
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
- **A new operator tool that discharges one System's obligations.** judgment: it needs an operator
  to notice a leak that produces no signal, which is the failure #2326 describes rather than a
  remedy for it.
- **Do nothing.** verified: no existing surface reaches the discharge for an already-torn-down
  System — `systems.teardown` short-circuits at
  `../../src/kdive/mcp/tools/lifecycle/systems/admin.py:451-462` without enqueueing a job, and the
  three other terminal paths cannot produce the pair — so the rows stay open and their volumes stay
  unreclaimable for the life of the row.
