# 0593 — External-boot operations ride marked boot and teardown jobs

## Status

Accepted (2026-09-03)

## Context

Issue #2118 names seven external-boot lifecycle operations — materialize, prepare, activate,
release, recover, resolve-conflict, and cleanup — plus the teardown that ADR-0583 uses as the
escape hatch out of a stuck activation. Every durable and provider piece exists. The provider
port `ExternalBootPorts` (`src/kdive/providers/ports/external_boot.py:308`) declares six
operations; `ProviderRuntime.external_boot` (`src/kdive/providers/core/runtime.py:180`) is the
injection slot; `ExternalBootAuthorityMarkerV1` and `ExternalBootAuthorityResultV1`
(`src/kdive/jobs/models.py:188`, `:221`) are the envelopes; and the worker already decodes a
marker, fences a malformed one out of generic finalization, and commits an authority-bound
result (`src/kdive/jobs/worker.py:534-573`, `src/kdive/jobs/queue.py:267`). What is missing is
the job surface: no payload carries a marker, and no handler runs an operation.

Three facts in the installed schema constrain any answer.

`allocate_external_boot_authority` refuses unless
`v_job.kind = (CASE WHEN p_purpose = 'teardown' THEN 'teardown' ELSE 'boot' END)`
(`src/kdive/db/schema/0122_external_boot_authority.sql:465`). A new `JobKind` member is therefore
inadmissible, not merely undesirable. That one guard carries it alone: the CHECK constraints at
`:111-112` and `:210-211` constrain the authority row's *purpose*, not the job's *kind*, and would
not by themselves exclude a new kind.

The worker's only authorized write path is the `SECURITY DEFINER` function.
`0121_external_boot_activations.sql:280-285` revokes every privilege on the four external-boot
tables from `kdive_worker` and re-grants `SELECT` alone, and
`commit_external_boot_authority_result` (`0122_external_boot_authority.sql:731`) both updates
the activation and finalizes the `jobs` row itself (`:1661`, `:1681`).

`HandlerRegistry` binds exactly one handler per `JobKind`
(`src/kdive/jobs/models.py:386-395`), and both `boot` and `teardown` already have one
(`src/kdive/jobs/handlers/runs/registrar.py:34`,
`src/kdive/jobs/handlers/systems.py:836`). Those two handlers boot a Run and tear a System
down. Reaching either with an authority-marked job would perform the wrong operation on a
System an activation restricts.

A fourth fact is a gap rather than a constraint, and it is the one this record exists to
settle. `commit_external_boot_authority_result` returns `superseded` unless an
`external_boot_authority_acknowledgements` row exists for the allocated authority
(`:904`), and `acknowledge_external_boot_authority` is granted to `kdive_provider_authority`
alone (`:1741-1744`) — a role no worker session holds, by ADR-0584's design. Nothing in the
tree is a worker-side client of the authority host's request socket; `transport.py` implements
the server half only.

## Decision

**The seven operations ride the existing `boot` and `teardown` kinds, selected by the marker's
`purpose`, and are dispatched to per-operation handlers by the marker's `operation`.**

1. `BootPayload` extends `RunPayload` and `TeardownPayload` extends `SystemPayload`, each with
   one optional field named exactly `external_boot_authority_v1`. The field name is load-bearing:
   the claim and finalizer fences and the two `SECURITY DEFINER` functions all test
   `payload ? 'external_boot_authority_v1'` against the top-level JSONB key. `dump_payload`
   already serializes with `exclude_none=True`, so an unmarked payload is byte-identical to
   today's.

2. A marked payload is cross-checked at validation, not at the database. The marker's
   `activation_id`, `run_id`, `system_id`, and `plan_identity` must agree with each other and
   with the payload's own `run_id`/`system_id`; the enqueueing caller supplies `provider_kind`
   and `authority_instance` explicitly, because neither `ExternalBootActivation` nor
   `ExternalBootReservation` carries them.

3. Routing is structural and cannot be skipped. `register_all_handlers` builds one
   `ExternalBootOperations` registry — marker `operation` to handler, exactly one each — and
   passes it as a **required** keyword to both `runs.register_handlers` and
   `systems.register_handlers`. Each wraps its ordinary handler in one shared router
   (`route_marked`), so a marked job never reaches `boot_handler` or `teardown_handler` and an
   unmarked job never reaches an operation handler. There is one router, not a guard per
   caller.

4. **`materialize` and `prepare` are dispositioned `prepared-before-admission`: preconditions
   the `activate` handler verifies and consumes, never operations it performs.** The schema
   forecloses every other reading. `allocate_external_boot_authority` admits `purpose =
   'activate'` only from activation state `prepared` or `activating`
   (`0122…sql:482-485`), and the table CHECK `external_boot_activation_state_evidence`
   (`0121…sql:38-52`) admits either state only when `materialization` **and** `recovery_point`
   are already non-null. An activate job therefore cannot allocate authority until both have
   been recorded, so it cannot be the thing that records them. Both are also `kdive_server`
   writes (`0121…sql:275`, `:280-285`), which no worker session can perform. The activate
   handler reads both off the activation row, requires ownership agreement, and fails closed
   with `configuration_error` when either is absent or disagrees. Producing them is the
   server-side preparation path, excluded here and owned by #2204.

   The registry covers the six enqueueable operations — activate, recover, resolve-conflict,
   release, cleanup, teardown. The marker's three remaining `operation` values are not
   enqueueable admissions: `fail` is a result carrier, and `deadline` and `recovery-attempt`
   are mid-operation commits that leave the `jobs` row `running` on purpose
   (`0122…sql:1685-1687`), which is the re-entry #2202 owns. A marker carrying one is refused
   with a categorized error rather than silently unhandled.

5. **The acknowledgement is a one-method, provider-neutral seam over models that already
   exist.** `ExternalBootAuthorityAcknowledger` is
   `async def acknowledge(self, request: AuthorityTakeoverRequestV1) -> AuthorityAcknowledgementV1`,
   both models taken unchanged from `src/kdive/providers/external_boot_authority/protocol.py`
   (`:131`, `:209`). `AuthorityTakeoverRequestV1` already carries every fact the handler holds
   after allocation, and `AuthorityAcknowledgementV1` already carries exactly the three the
   commit needs — `journal_sequence`, `journal_digest`, `positive_quiescence_digest`. The
   remaining `acknowledge_external_boot_authority` arguments (`allocation_id`, `job_id`,
   `job_attempt`, `worker_incarnation`) are the host's to resolve from
   `external_boot_authorities`, on which it holds `SELECT` (`0122…sql:1716-1719`). No new
   model is defined for this.

   The seam is injected through `ExternalBootHandlerPorts` and absent by default; absent, the
   handler fails closed with `ErrorCategory.CONFIGURATION_ERROR` before touching the provider.
   Wiring it to the authority host's transport is provider-adapter work (#2199, #2200);
   defining it here is what lets an operation be executed and tested at all.

6. **The handler returns its result; the worker commits it.** `_finalize_handler` already
   requires `_authority_binding_matches` before crossing the SQL boundary
   (`src/kdive/jobs/worker.py:534-542`), which is the check that keeps a mismatched result out
   of the database. A handler that committed for itself would bypass that check and duplicate
   the commit path. A provider failure is raised as `ExternalBootAuthorityFailure`, which the
   same function commits through the `fail` branch.

## Consequences

- No new `JobKind`, no schema change, and no migration. The whole change is Python.
- `boot_handler` and `teardown_handler` change one line each — the payload model they load —
  because `_ACTIVE_PAYLOAD_MODELS` now names the extended models and `load_payload` requires an
  exact model match (`src/kdive/jobs/payloads.py:467`).
- `register_handlers` in the runs and systems registrars gains a required keyword. Every call
  site must pass the operations registry; that is the point, since a call site that omitted it
  would silently reopen the wrong-operation path.
- The production handler registry registers all six operations, and every one of them fails
  closed on the absent acknowledger until an adapter issue supplies it. **That failure is not
  visible in the `jobs` table, and `terminal=True` is inert on this path.** When a marked job's
  handler raises anything that is not an `ExternalBootAuthorityFailure` whose binding matches the
  marker, `src/kdive/jobs/worker.py:505-517` logs `marked external boot job %s failed without
  authority result` and returns without reaching `_fail_job_and_run`. No `jobs` row is written and
  no `error_category` is set. The job's lease then lapses, `claim_worker_job` re-claims it and
  increments `attempt` — the lane #2201's `0127` migration reopened — and the same failure repeats
  once per lease lapse until `attempt >= max_attempts`, at which point the row is permanently
  `running`, because `repair_abandoned_jobs` is fenced against marked payloads
  (`src/kdive/reconciler/repairs/jobs.py:42-49`) and both generic finalizers are fenced
  (`0122…sql:304-315`). This is the shipped default configuration until #2199/#2200 wire an
  acknowledger, and it is reached by every pre-allocation refusal — absent acknowledger, absent
  port, provider-kind mismatch, activation identity mismatch, wrong activation state. The only
  observable is the log line.
- **The window in which a marked job is left `running` is a re-execution window, not an
  availability window.** A lapsed-lease `running` job with `attempt < max_attempts` is re-claimed
  and the handler restarts at step 1, which includes the provider call at step 5. So a worker that
  dies between the port call and the commit, a commit that returns `superseded`, or a result the
  worker's `_authority_binding_matches` rejects, all leave a System the first attempt already
  mutated and an activation whose state never moved — so the allocate preconditions still hold and
  the second attempt re-runs the same mutation. For `cleanup` and `teardown` that is a re-run
  deletion. Nothing here makes the port calls idempotent or gates re-entry on an observation:
  idempotency under a later authority generation is the adapter's obligation under ADR-0584, and
  observe-driven re-entry is #2202's. This decision does not close that window; it names it
  correctly. #2203 owns the reaping half.
- **A third route reaches the same wedge, through the commit rather than the handler.**
  `_finalize_handler` calls `_commit_external_result` outside its `try/except`
  (`src/kdive/jobs/worker.py:539` against `:505-533`), and `_dispatch` has no `except`
  (`:439-444`), so an exception from `commit_external_boot_authority_result` propagates to
  `_claim_loop`'s generic handler (`:417-427`). The observable is a lane-level warning with no job
  attribution — weaker than the marked-job log line the bullet above describes — and the commit
  raises SQLSTATE `22023` on several evidence-content paths a handler composes. Keeping every
  evidence `objects` entry inside the commit's `known_refs` set is what keeps handlers off it.
- **The adapter's admission watermark fences the concurrent case, not the sequential one.**
  `_require_admissible_generation`
  (`src/kdive/providers/local_libvirt/external_boot_authority.py:149-158`) rejects a *lower*
  generation and does not constrain a higher one, so it does nothing for the re-execution window
  above, where the re-claim allocates a higher generation. It does fence a still-live worker whose
  lease lapsed while it was mid-provider-call and whose replacement has already allocated a later
  generation — a concurrency case, not a sequential one.
- The `release` operation credits the recovery-store reservation back while the objects it covers
  still exist, because ADR-0584's adapter makes deletion belong to `cleanup` under a later
  generation. That departs from ADR-0583's stated ordering and under-charges
  `recovery_max_bytes` between the two commits — permanently if the `cleanup` job never commits,
  which the bullet above makes the expected case. Recorded as
  [deferral record 0010](../debt/0010-external-boot-release-credits-capacity-before-cleanup.md); this
  decision neither introduces nor resolves it.
- `ExternalBootPorts.materialize` and `.prepare` stay uncalled by any worker handler. They are
  the preparation path's, and that path is #2204's. This change does not make the six-method
  port fully exercised in production; it makes the four operations a worker owns executable.
- The terminal evidence's `composite_state` is the acknowledgement's
  `positive_quiescence_digest`, not a digest the handler computes for itself. That is what
  `0122…sql:1514-1515` already stores as `acknowledged_composite_state` on a resolved conflict,
  so the evidence and the acknowledgement agree by construction instead of by convention.

## Considered & rejected

- **Add `JobKind` members for the lifecycle operations.** verified: `allocate_external_boot_authority`
  returns `superseded` unless `v_job.kind` is exactly `boot` or `teardown`
  (`src/kdive/db/schema/0122_external_boot_authority.sql:465` at commit `54f346f5`). A new kind is
  unclaimable authority, not a design preference.
- **Register the operations directly on `JobKind.BOOT` and `JobKind.TEARDOWN`.** verified:
  `HandlerRegistry.register` raises `DuplicateHandler` when a kind already has a handler
  (`src/kdive/jobs/models.py:393-394`), and both kinds are already bound in
  `register_all_handlers` (`src/kdive/jobs/assembly.py:112-126`).
- **Branch on the marker inside `boot_handler` and `teardown_handler`.** judgment: two
  independent guards on the same invariant, where forgetting either runs the wrong operation
  against a live System. One shared router is the same behavior with one place to get wrong.
- **Give materialize and prepare their own marked jobs.** verified: neither name appears in
  `p_purpose`'s five admitted values (`0122…sql:367`), in the marker's `operation` literal
  (`src/kdive/jobs/models.py:200-210`), or in `ExternalBootResultPayload`'s eight variants
  (`:175-185`). Such a job could allocate no authority and commit no result.
- **Run materialize and prepare as phases inside the activate job.** verified: the activate
  job must allocate authority before it may touch the provider, and
  `allocate_external_boot_authority` refuses `purpose = 'activate'` unless the activation is
  already `prepared` or `activating` (`0122…sql:482-485`) — states the CHECK at
  `0121…sql:38-52` admits only once `materialization` and `recovery_point` are both recorded.
  The phases would have to run before the authority they need. Recording them is also a
  `kdive_server` write the worker's SELECT-only grant (`0121…sql:280-285`) forbids.
- **Give materialize and prepare their own unmarked jobs.** verified: a distinct unmarked job
  needs a distinct `JobKind`, and `JobKind` is a Postgres enum whose values
  `src/kdive/db/schema/` can only extend by migration — which criterion 1 and this change's
  no-migration constraint both exclude. Reusing `boot` or `teardown` unmarked is not an option
  either: an unmarked job of those kinds is dispatched to `boot_handler`/`teardown_handler` by
  `route_marked`, which boot a Run and tear a System down. (`0122…sql:465` does **not** carry
  this: it constrains an *authority-marked* job's kind, and an unmarked job never calls
  `allocate_external_boot_authority` at all.) The disposition above needs no new kind.
- **Have the handler call `commit_external_boot_authority_result` itself.** judgment: it
  bypasses `_authority_binding_matches`, the one check standing between a mismatched result and
  the authority tables, and duplicates a commit path `queue.py:267` already owns.
- **Have the worker acknowledge its own authority.** verified: `acknowledge_external_boot_authority`
  raises `provider authority is required` (SQLSTATE `42501`) unless the session holds
  `kdive_provider_authority` (`0122…sql:594-596`), and is granted to that role alone
  (`:1741-1744`). ADR-0584 makes the separation the fence, so a worker that could acknowledge
  would defeat it.
- **Build the worker-side authority-host client in this change.** judgment: an mTLS
  unix-socket client with journal-head reconciliation is the provider-adapter work #2199 and
  #2200 already own, and it would triple this change without making one more operation
  executable than the seam does.
- **Do nothing and leave the contracts inert.** verified:
  `docs/debt/0003-external-boot-contracts-await-their-executor.md` records three registered MCP
  tools returning `recovery_executor_unavailable` with no home for the obligation in #2118's
  body; the null option keeps them inert indefinitely.

## References

- Spec: [2026-09-03-external-boot-job-handlers-design.md](../workflow/specs/2026-09-03-external-boot-job-handlers-design.md)
- [ADR-0583](0583-external-run-boot-uses-prepared-recovery-points.md),
  [ADR-0584](0584-provider-host-authority-fences-external-boot-mutations.md)
- Issue #2205; parent #2118; blocker #2201 (merged).
