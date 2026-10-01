# Repeated provider-conflict ends the external-boot job — design

Issue: #2901 (PR-B of the #2898 split). Decision record: [ADR-0714](../../adr/0714-repeated-provider-conflict-ends-the-external-boot-job.md).

## Problem

An authority-driven external-boot job (boot, teardown, preparation) that hits a deterministic
provider fault retries until it exhausts its budget, and every attempt allocates a new authority
generation. In the #2889 live settle, five attempts each failed at `phase=commit` with
`authority: provider-conflict` about one second apart, moved the journal head five generations
ahead, and used up the recycled teardown budget in about 30 s.

The path today:

- `AuthorityService._provider_error` (`src/kdive/providers/external_boot_authority/service.py`)
  turns every non-`AuthorityServiceError` exception at the provider boundary into
  `provider_conflict`; the transport sends it as the peer reason `provider-conflict`.
- `_failure` in `src/kdive/jobs/authority_sender.py` maps that reason to a non-terminal
  `CategorizedError` with category `infrastructure_failure`, which is retryable.
- `_bound_failure` in `src/kdive/jobs/handlers/external_boot/runner.py` wraps it in a `fail`
  result with `terminal: false` and `failure_context: {"phase": ...}`.
- The `fail` branch of `commit_external_boot_authority_result` (migration 0122, patched by later
  migrations) requeues while `attempt < max_attempts`. A requeue clears the job's
  `error_category` and `failure_context` (the envelope rule that migration 0163 restored), and the
  `external_boot_authority_audit` row records only `outcome = 'result_requeued'`. No record shows
  that the previous attempt failed the same way.

## Scope

In scope:

1. **Mark the reason.** `_failure("provider-conflict")` sets
   `details={"authority_reason": "provider-conflict"}`. Category, message, and `terminal` stay as
   they are. The wire protocol does not change.
2. **Carry it into the result.** `ExternalBootAuthorityFailureContext` (`src/kdive/jobs/models.py`)
   gains `authority_reason: Literal["provider-conflict"] | None = None`. `_bound_failure` copies
   it from a `CategorizedError` whose `details["authority_reason"] == "provider-conflict"` and
   whose category is `infrastructure_failure`. A model validator requires that category whenever
   the field is set.
3. **Migration `0170_repeated_provider_conflict_terminal.sql`:**
   - adds the nullable column `external_boot_authority_audit.failure_context jsonb` and the index
     `external_boot_authority_audit_job_attempt_idx (job_id, job_attempt)`;
   - patches the commit function in place, using the 0160 pattern (`pg_get_functiondef`, each old
     text must occur exactly once, `EXECUTE`), at four sites:
     a. adds `'authority_reason'` to the allowed `failure_context` keys;
     b. refuses (SQLSTATE `22023`) an `authority_reason` that is not the string
        `provider-conflict`, or that comes with an `error_category` other than
        `infrastructure_failure`;
     c. the audit insert stores `v_failure_context` for a `fail` operation and `NULL` for every
        other operation;
     d. `v_terminal` gains a third clause. A `fail` whose `failure_context` contains
        `authority_reason` is terminal when an audit row exists for the same job with
        `job_attempt = v_job.attempt - 1`, `created_at >= v_job.created_at` (the current budget,
        ADR-0711), `outcome = 'result_requeued'`, and a `failure_context` equal to this one.
4. ADR-0714, this spec, and the plan.

Out of scope (the approved exclusions in `WORK:SCOPE`): the provider defects (#2898, #2867), the
readiness race (#2899), backoff for other job kinds, and the size of the ADR-0711 grant. Not
changed: `worker._is_terminal` (marked external-boot jobs never reach it), the authority wire
categories, `COMMITTABLE_ERROR_CATEGORIES`, and the commit function's fences, binding checks, and
reservation-credit paths. The only effect on those is the existing terminal `fail` behaviour,
which runs earlier.

### Failure model

1. **Actors and deployments**
   - the worker role committing an external-boot result (local-libvirt and remote-libvirt);
   - the authority service, which is unchanged;
   - an operator who reads `jobs.get` and the audit table.
2. **Invariants and assets at stake**
   - exactly-once reservation credit and the authority fences (ADR-0584, ADR-0620);
   - ADR-0711's limit on generations per budget (no new grant, no `max_attempts` increase);
   - a requeued job carries no `error_category` and an empty `failure_context` (migration 0163,
     ADR-0019 envelope);
   - the shape of the commit function's other branches.
3. **Accepted failure classes**
   - Two consecutive transient provider faults that both surface as `provider-conflict` at the
     same phase end the job one attempt early. Accepted because the authority cannot tell a
     transient provider exception from a deterministic one (the provider text never crosses the
     boundary, ADR-0584). The cost is bounded: an operator recycles the job, the same as after
     exhaustion.
   - A `provider-conflict` raised outside `_bound_failure`, for example by takeover
     acknowledgement before an acknowledgement exists, is not covered. That path writes no `fail`
     result today.
   - Audit rows written before 0170 have `failure_context = NULL` and never match. The first repeat
     after deploy needs one more attempt.
4. **Covered elsewhere**
   - provider defects: #2898 / #2867;
   - readiness race: #2899;
   - wedged marked jobs with no binding result: ADR-0593.

## Success

- S1 — On the 0170 schema, a second consecutive `fail` with the same `failure_context` carrying
  `authority_reason: provider-conflict` on one external-boot job returns `job_state = 'failed'`
  at attempt 2 of 3. Today the same commit returns `queued`.
- S2 — These still requeue below `max_attempts`: the first such `fail`; a `fail` without
  `authority_reason`; a repeat whose `phase` differs; and a repeat whose previous audit row is
  older than the job's current `created_at`.
- S3 — On teardown, the terminal repeat leaves reservation credit and releases exactly as a
  terminal teardown failure leaves them today (the 0160 credit test shape).
- S4 — An `authority_reason` other than `provider-conflict`, or one paired with a category other
  than `infrastructure_failure`, is refused with SQLSTATE `22023`.
- S5 — A sender `provider-conflict` error yields a bound failure result whose context carries
  `authority_reason`. Other peer reasons and other exceptions do not.

## Validation

- `focused-test`, S1–S4: a new `tests/db/test_migration_0170_repeated_provider_conflict.py`
  against real Postgres, plus a patch-target test asserting that each old text occurs exactly
  once in the 0169 function definition. Red before the migration exists:
  `uv run python -m pytest tests/db/test_migration_0170_repeated_provider_conflict.py -q`.
- `focused-test`, S5: `tests/jobs/test_external_boot_authority_client.py` (sender details) and
  `tests/jobs/handlers/external_boot/` (bound-failure context). The model validator goes in
  `tests/jobs/test_external_boot_authority_models.py`.
- `focused-test`, migration registry: `tests/db/test_migrate.py` lists gain `0170`.
