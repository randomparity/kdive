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

1. **Bound.** An attempt that a grant claimed cannot earn another grant. The proof predicate
   `has_acknowledged_external_boot_retry_proof(job)` is false when a consumption row has
   `claimed_attempt = job.attempt`. Claim, queue depth (`count_claimable_worker_jobs`), and the
   consume function already call that predicate, so all three apply the same bound. The
   evidence part of the old predicate stays as the private function
   `has_acknowledged_external_boot_no_mutation_head(job)`.
2. **Terminal state.** The reconciler's `dead_letter_unowned_external_boot_jobs` also ends a
   `boot` job that is `running`, exhausted, lease-lapsed, and past the bound, when its latest
   authority still carries an exact acknowledged no-mutation head. Under the job-row lock it
   takes the System's journal-head lock (`hashtextextended('kdive:system:' || system_id, 2126)`,
   the lock every journal-head advance takes). It then checks the head again, sets the job's
   `allocating` or `current` authority to `superseded`, and fails the job and its open Run with
   `lease_expired`, as migration 0162 does.
3. **Scope of the bound.** The bound applies per budget, not per job lifetime. A public teardown
   recycle adds a new ordinary budget (ADR-0620), and the last attempt of that budget is an
   ordinary claim that can earn one grant. A `teardown` job past the bound stays `running` for
   that public recycle, as before; migration 0162 does not dead-letter teardown jobs.
4. **One limit for #2901.** An external-boot job allocates at most `max_attempts` authority
   generations per budget, plus at most one generation from this grant. The fix for deterministic
   `provider-conflict` churn (#2901) must stay inside that limit. It can make a job spend fewer of
   those attempts, for example by making a repeated identical `provider-conflict` terminal. It
   must not add a second retry counter or another grant.

## Consequences

- The #2865 trigger ends after at most one grant: the job becomes `failed` (`lease_expired`) at
  the next reconciler pass after its granted attempt lapses. A job looping at deploy time, whose
  current attempt came from a grant, stops at its next lapse.
- A worker crash after acknowledgement still gets its one replacement claim (ADR-0626). If that
  replacement also stops at a new acknowledged no-mutation head, the job now fails instead of
  claiming again.
- After supersession, no receipt can commit for the job: every commit needs an `allocating` or
  `current` authority and a `running` job. The authority service cannot admit a mutation either,
  because `resolve_current_external_boot_authority` requires `current` and a journal-head advance
  under a non-`current` authority returns `superseded`. The journal head stays at the acknowledged
  record. The next allocation for the System starts from that head, just as it would after an
  ADR-0626 successor.
- The activation is not changed. A `preparing` activation that never recorded a preparation plan
  still has no release path until #2961.

## Considered & rejected

- **Bound grants per job lifetime (`count(*) < 1`).** judgment: a recycled teardown budget could
  never recover from a crash at acknowledgement. The per-budget bound is also finite, because
  only an operator's public teardown adds a budget.
- **Limit grants with a small fixed cap greater than one.** judgment: every extra grant against a
  deterministic refusal only adds another generation. No evidence shows two crashes in a row at
  acknowledgement are common enough to need a second grant.
- **Stop at the bound but leave the job `running`.** verified: migration 0162
  (`dead_letter_unowned_external_boot_jobs`) skips any job with an `allocating` or `current`
  authority, and the #2865 job's latest authority was `current`. The job would stay `running`
  forever, which fails the terminal-state criterion of #2960.
- **Retire the authority instead of superseding it.** judgment: `retired` means the authority
  finished its operation (ADR-0620). `superseded` is the state an ADR-0626 successor allocation
  would leave, so readers of the chain see a shape they already handle.
- **Dead-letter without the journal-head lock.** verified: `advance_external_boot_authority_journal_head`
  (migration 0123) takes that lock and checks the authority state before it writes the next
  record, and it does not lock the job row. Without the lock, the check of the head and the
  supersession could interleave with an advance to `admitted`.
