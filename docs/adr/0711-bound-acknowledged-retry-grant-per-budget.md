# 0711 — One acknowledged-retry grant per exhausted budget

## Status

Accepted (2026-09-29)

Amends [ADR-0626](0626-recover-exhausted-acknowledged-authority-claims.md): a replacement claim
that ADR-0626 granted cannot earn another one.

## Context

ADR-0626 gives an exhausted external-boot job (`attempt == max_attempts`, lapsed lease) one more
claim when the database proves an exact, unconsumed `takeover-acknowledged` journal head: the
authority acknowledged the attempt and admitted no provider mutation. The claim raises
`max_attempts` by one and records the consumed proof (migration 0150). A replacement that also
stops before provider admission earns another claim from its own new head.

The rule assumed the stop was a crash. When the authority refuses every attempt the same way
before provider admission (`journal-conflict` on a reused System, #2951), each attempt leaves a
new acknowledged no-mutation head, so each lease lapse grants one more claim. The #2865 native
proof saw one job go `3/3`, `4/4`, `5/5`, `6/6` with no end. The job never becomes terminal, and
the reconciler cannot dead-letter it: ADR-0620 (#2889 amendment, migration 0162) skips a job
that still has an `allocating` or `current` authority, because that authority could still commit.

## Decision

1. **Bound.** An attempt that a grant claimed in the current budget cannot earn another grant.
   The proof predicate `has_acknowledged_external_boot_retry_proof(job)` is false when a
   consumption row has `claimed_attempt = job.attempt` and `consumed_at >= job.created_at`.
   Every recycle in `queue.enqueue` resets `created_at`, so the check reads only the current
   budget. Claim, queue depth (`count_claimable_worker_jobs`), and the consume function already
   call that predicate, so all three apply the same bound. The evidence part of the old predicate
   stays as the private function `has_acknowledged_external_boot_no_mutation_head(job)`.
2. **Terminal state.** The reconciler's `dead_letter_unowned_external_boot_jobs` also ends a
   `boot` job that is `running`, exhausted, lease-lapsed, and past the bound, when its latest
   authority still carries an exact acknowledged no-mutation head. Under the job-row lock it
   takes the System's journal-head lock (`hashtextextended('kdive:system:' || system_id, 2126)`,
   the lock every journal-head advance takes) and checks the head again. It then locks the job's
   live authority rows with `NOWAIT`, and skips the job until the next pass if a commit holds one.
   It retires a `current` authority, supersedes an `allocating` one, and fails the job and its open
   Run with `lease_expired`, as migrations 0162 and 0164 do.
3. **Scope of the bound.** The bound applies per budget, not per job lifetime. A recycle (the
   public teardown recycle of ADR-0620, or a forced step re-run) starts a new budget, and the last
   attempt of that budget is an ordinary claim that can earn one grant. A `teardown` job past the
   bound stays `running` for that public recycle, as before; 0162 does not dead-letter teardown.
4. **One limit for #2901.** An external-boot job allocates at most `max_attempts` authority
   generations per budget, plus at most one generation from this grant. The fix for deterministic
   `provider-conflict` churn (#2901) must stay inside that limit. It can make a job spend fewer of
   those attempts, for example by making a repeated identical `provider-conflict` terminal. It
   must not add a second retry counter or another grant.

## Consequences

- The #2865 trigger ends after at most one grant: when the granted attempt also stops at its own
  acknowledged head, the job becomes `failed` (`lease_expired`) at the next reconciler pass after
  the lease lapses. A job looping at deploy time, whose current attempt came from a grant, stops
  at its next lapse.
- A granted attempt that lapses before its own acknowledged head (no allocation, or a watermark
  only) gets no grant and stays `running`, like any exhausted job whose authority is still live
  (ADR-0620). It cannot loop.
- A worker crash after acknowledgement still gets its one replacement claim (ADR-0626).
- After the terminal update, no receipt can commit for the job: every commit needs an
  `allocating` or `current` authority and a `running` job. The authority service cannot admit a
  mutation either: `resolve_current_external_boot_authority` requires `current`, and a
  journal-head advance under a non-`current` authority returns `superseded`.
- A retired authority keeps the activation's dispatch route: the teardown, release, and quarantine
  resolvers (migrations 0147, 0140, and `db/external_boot_recovery_quarantine.py`) select the
  newest `current` or `retired` authority. When the latest authority was `allocating`, no route
  existed before this change either.
- The activation is not changed. A `preparing` activation that never recorded a preparation plan
  still has no release path until #2961.

## Considered & rejected

- **Bound grants per job lifetime (`count(*) < 1`).** judgment: a recycled budget could never
  recover from a crash at acknowledgement. The per-budget bound is also finite, because only an
  operator action adds a budget.
- **Key the bound on the attempt number alone.** verified: `queue.enqueue` recycles a `failed`
  job with `attempt = 0` and an unchanged `max_attempts` (`src/kdive/jobs/queue.py`, recycle
  `UPDATE`), so an old consumption row would match the new budget's last attempt and deny its
  grant.
- **Limit grants with a small fixed cap greater than one.** judgment: every extra grant against a
  deterministic refusal only adds another generation. No evidence shows two crashes in a row at
  acknowledgement are common enough to need a second grant.
- **Stop at the bound but leave the job `running`.** verified: migration 0162
  (`dead_letter_unowned_external_boot_jobs`) skips any job with an `allocating` or `current`
  authority, and the #2865 job's latest authority was `current`. The job would stay `running`
  forever, which fails the terminal-state criterion of #2960.
- **Supersede every live authority.** verified: `resolve_external_boot_system_teardown_dispatch_binding`
  (migration 0147) joins only `current` or `retired` authorities, so superseding the `current`
  one would remove the route `systems.teardown` needs. Migration 0164 retires for the same reason.
- **Dead-letter without the journal-head lock.** verified: `advance_external_boot_authority_journal_head`
  (migration 0123) takes that lock and checks the authority state before it writes the next
  record, and it does not lock the job row. Without the lock, the check of the head and the
  update could interleave with an advance to `admitted`.
- **Wait for the authority row lock.** verified: the worker commit functions lock the authority
  row before the job row (migrations 0135 and 0143), and the reconciler holds the job row first.
  A wait could deadlock and roll back every dead-letter in the reconciler's pass.
