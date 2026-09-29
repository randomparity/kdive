# Take over an interrupted System teardown (#2884)

## Scope and authority

Campaign scope for issue #2884, token `q2884-2ea70add`. The operator approved a three-part scope
on 2026-09-28: the takeover exemption and the allocation fence (migration 0161), and folding the
teardown dedup recycle into this issue. Exclusions: the journal file/database divergence (#2793,
merged); activate-versus-teardown policy beyond the fence (ADR follow-up); manual database or
journal edits and host reset (not authorized). This change amends
[ADR-0620](../../adr/0620-authority-owned-system-teardown.md), including how ADR-0584's
same-operation rule applies to takeover records; it adds no ADR.

## Problem

Four defects sit on one path: settling an interrupted authority teardown with supported recovery.

1. **Takeover refused.** The takeover `watermark-installed` record carries the new authority id
   as its attempt id (`service.py` `_record`; `AuthorityTakeoverRequestV1` has no attempt id).
   A teardown's operation identity is deterministic per System and activation
   (`admin.py` `_enqueue_authority_teardown`), so a reclaimed or new teardown job takes over with
   the same identity as the interrupted one. The same-operation integrity clause in
   `advance_external_boot_authority_journal_head` (0123) refuses any record with that identity
   and a different attempt id while the head is `admitted`, `mutation-started`,
   `provider-returned` or `observed`, whatever the record's phase. The takeover fails with
   `journal_conflict`, the suspended operation is never recorded, and recovery never runs.
2. **Activate supersedes teardown.** A non-teardown allocation is refused only while a teardown
   authority is `current` (0147). Every allocation supersedes the prior `allocating` or `current`
   row (0122). An activate job can therefore supersede a teardown takeover in flight, or allocate
   over a head whose unresolved operation is a teardown, and run that teardown's recovery under an
   activate authority, outside the teardown job and with no teardown commit or credit.
3. **A failed teardown job is never replaced.** `_enqueue_authority_teardown` replays any job
   under `<system>:teardown` (recycle `NEVER`), and `jobs.dedup_key` is unique. Once an authority
   teardown job exhausts its attempts, every public `systems.teardown` returns that failed job.
4. **Proof context binds the wrong record.** A teardown's attempt id is `uuid5` of its
   operation identity, so it is constant across generations. `_recovery_observation` and
   `_execute_teardown` both select the first `mutation-started` record with that identity and
   attempt id. After a takeover recovers generation N's teardown and generation M runs a fresh
   one, `_execute_teardown` at M builds its proof facts from N's `mutation-started` context. The
   local provider refuses that anchor against its retained generation-M intent
   (`matches_anchor`), so the committed teardown can never be proven. `_recovery_observation` has
   the same defect whenever an older generation of the same teardown also reached
   `mutation-started`.

## Design

### Migration 0161

One migration, two guarded patches in the 0148/0157/0160 style. Each reads
`pg_get_functiondef`, requires its target text to occur exactly once and its replacement to be
absent, replaces it, checks the result, and executes it.

- **`advance_external_boot_authority_journal_head`.** The same-operation clause gains
  `AND v_phase NOT IN ('watermark-installed', 'takeover-superseded', 'takeover-acknowledged')`.
  Those three phases do not continue the operation; each already has its own fence (allocating
  and newest authority, head phase, pending takeover, watermark sequence and digest). The clause
  still protects every operation phase record (`admitted` through `terminal`), which is what
  ADR-0584's replay protection needs.
- **`allocate_external_boot_authority`.** The 0147 non-teardown clause becomes: refuse a
  non-teardown purpose while a teardown authority for the System is `allocating` or `current`,
  or while the System's journal head is an unresolved teardown operation (phase `admitted`,
  `mutation-started`, `provider-returned` or `observed` with `purpose = 'teardown'`) or carries
  a suspended teardown operation. The refusal is the existing `superseded` status, so an activate
  job sees `stale_handle` and retries or exhausts without writing an authority row. A teardown
  allocation is unaffected and still supersedes whatever is `allocating` or `current`.

### Service (`_recovery_observation`, `_execute_teardown`, `_acknowledge_takeover_bound`)

Both proof paths select the `mutation-started` record that belongs to the operation they prove,
adding `authority_id` and `generation` to the existing identity and attempt-id match:
`_recovery_observation` matches the recovered record's own authority and generation, and
`_execute_teardown` matches the request's.

`_acknowledge_takeover_bound` builds its identity-keyed phase maps (whether to recover, which
record is unresolved, and the post-recovery check) from operation-phase records only. A teardown
takeover's records share the suspended teardown's identity, so a takeover interrupted before
acknowledging otherwise hides the unresolved operation from its successor, which skips recovery
and is refused `journal_conflict` at acknowledgement (a discovered in-path defect, authorized by
the orchestrator on 2026-09-28). `_anchor`, the host, the journal and `execute_mutation` do not
change.

### Dedup recycle (`_enqueue_authority_teardown`)

When the prior `<system>:teardown` job is `failed`, the handler builds the marker it would
enqueue and compares it with the failed job's marker. If they are identical, it calls
`queue.enqueue` with `JobRecyclePolicy.TERMINAL`, which resets that row to a fresh queued attempt
(attempt 0, same id and `max_attempts`). The check and the recycle run in the transaction that
holds the System advisory lock, and only a `failed` row reaches the recycle, so `succeeded` is
never reset. Every other prior state (live, `succeeded`, `canceled`) and any marker mismatch keeps
today's replay or conflict. No new recycle policy or queue change is needed.

Recycling is safe against the authority tables:

- `external_boot_authorities (job_id, job_attempt)` is a non-unique index (0122). A recycled
  attempt 1 inserts a new row with a fresh id and generation beside the old, now-superseded
  attempt-1 row.
- Allocation (0122) checks the live job row (`attempt`, `worker_id`, lease). Acknowledge,
  commit, finalize and every provider-authority resolver look the authority up by id and
  generation, require a live state (`allocating`/`current`), and require the authority's worker
  incarnation to own the running job with a live lease. Until the recycled job allocates, the
  failed attempt's last authority may still be `current` with the same `(job_id, job_attempt)`;
  the incarnation and lease binding refuses it once another incarnation claims the job, and the
  new allocation then supersedes it.
- Credit is exactly once. `finalize_external_boot_authority_teardown` credits only through a
  `current` teardown authority whose terminal head it owns, keys the receipt by authority id,
  moves the reservation to `external_boot_reservation_releases`, and refuses once the System is
  `torn_down`. Whichever authority finalizes first credits; every later finalize for the
  activation is refused or replays the same receipt.

### Settle path (retained fixture)

Journal records 1–23, DB head 23 (generation-3 teardown `mutation-started`), file record 24
retracted at startup by #2793. The new job allocates generation G; its watermark (24) anchors and
suspends record 23; recovery observes with generation 3's own context (matching the retained
generation-3 host intent) and anchors `provider-returned`, `observed` and `terminal` for
generation 3; `takeover-acknowledged` follows. The fresh generation-G teardown anchors `admitted`
and `mutation-started`, the provider adopts the retained intent as an authenticated successor and
destroys the domain, and `_execute_teardown` proves the result with generation G's own context.
The finalizer credits the ready reservation once.

The fixture also holds an `allocating` activate authority (generation 5) from before 0161. Its
job attempt's worker is gone, a reclaimed attempt allocates again and is fenced, and the teardown
allocation supersedes it. The settle's read-only snapshot confirms no activate job is live
before the public teardown is submitted.

## Success

- A takeover over a teardown head at `admitted`, `mutation-started`, `provider-returned` or
  `observed` with the same operation identity anchors, records the suspension, recovers it to
  terminal, and acknowledges; a following fresh teardown commits and credits once.
- Operation phase records with the same identity and a different attempt, source, target,
  operation or recovery objects are still refused `conflict`.
- A non-teardown allocation is refused while a teardown authority is `allocating` or `current`,
  or while the head is an unresolved or suspended teardown; a teardown allocation is admitted.
- A public teardown recycles a failed authority teardown job with an identical marker, and never
  a live, succeeded or canceled one or a marker mismatch.
- `_recovery_observation` and `_execute_teardown` bind their own generation's
  `mutation-started` context when older generations of the same teardown share its identity.
- A takeover that follows a teardown takeover interrupted after its watermark, or after an
  inherited `provider-returned`, recovers the teardown and acknowledges.
- The retained fixture settles live under criterion 6.

## Failure model

1. **Actors and deployments** — the worker (allocation, takeover, commit), the provider authority
   (journal and host teardown), the server (public `systems.teardown`), and Postgres.
2. **Invariants and assets at stake** — journal continuity and replay protection for operation
   phases; one teardown owner per System at a time; host destruction only inside a teardown
   authority lane; the reservation credited exactly once.
3. **Accepted failure classes** — an activate job refused by the fence retries until it exhausts
   (`stale_handle`), writing no authority rows. A takeover-superseded record with the same
   identity as an unresolved head now anchors; its own pending-takeover fence still binds it. A
   recycled job reuses attempt numbers already present on superseded authority rows; nothing
   resolves authority by `(job_id, job_attempt)` without also requiring a live state. A retained
   intent from an older generation that never reached the recovered operation's `begin` still
   refuses observation (`provider_conflict`); it is not on this settle path and is reported as a
   follow-up.
4. **Covered elsewhere** — journal file/database divergence and the startup retraction (#2793);
   broader activate-versus-teardown policy (ADR follow-up).

## Threat model

- **Boundaries:** the public `systems.teardown` path gains the ability to reset a failed job to
  queued. It stays behind the existing `admin` role check and System advisory lock, and it only
  re-runs the identical authority marker the caller could already enqueue on a fresh System.
- **Actors:** a project admin (teardown), the worker incarnation (allocation), the provider
  authority. None gains a new capability; the fence only narrows allocation.
- **Controls:** guarded SQL text replacement; marker equality; state-bound authority lookups.
- **Out of scope:** a compromised worker or authority identity.

## Validation

- `tests/db/test_migration_0161_teardown_takeover.py` (real Postgres): the takeover watermark
  over each unresolved teardown head phase anchors and records the suspension; an operation-phase
  record with a changed attempt id is still `conflict`; the allocation fence refuses activate over
  an `allocating` teardown authority, over an unresolved teardown head and over a suspended
  teardown, and admits a teardown; finalize after a recycle credits once and a superseded earlier
  attempt of the same job is refused.
- `tests/db/test_migration_0161_teardown_takeover.py`: a DB-backed service test with
  the real SQL repository and head function and a fake teardown adapter. Three generations share
  one teardown identity; each takeover recovers its predecessor's `mutation-started`, and the
  recorded contexts show recovery and the final proof each bind their own generation.
  A second test interrupts a takeover after its watermark and after an inherited
  `provider-returned`; the next takeover recovers and the teardown completes.
- `tests/providers/local_libvirt/test_external_boot.py`: observation is read-only and repeatable
  over each retained generation-N intent phase.
- `tests/mcp/lifecycle/`: recycle of a failed authority teardown with an identical marker, and
  no recycle for live, succeeded, canceled or mismatched jobs.
- Live settle on the retained lab fixture, after review: criterion 6 and #2793's installed-host
  startup retraction.
