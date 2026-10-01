# Repeated provider-conflict ends the external-boot job — design

Issue: #2901 (PR-B of the #2898 split). Decision record: [ADR-0714](../../adr/0714-repeated-provider-conflict-ends-the-external-boot-job.md).

## Problem

A deterministic authority provider fault on an external-boot job (boot or teardown) is retried
until the job exhausts its budget, and each attempt allocates a new authority generation. In the
#2889 settle, five attempts failed at `phase=commit` with `authority: provider-conflict` in about
30 s. The fault is reported as a retryable `infrastructure_failure`, and a requeue keeps no trace
of the failure. [ADR-0714](../../adr/0714-repeated-provider-conflict-ends-the-external-boot-job.md)
Context gives the path in full.

## Scope

In scope:

1. **Mark the reason.** `_failure("provider-conflict")` in `src/kdive/jobs/authority_sender.py`
   sets `details={"authority_reason": "provider-conflict"}`. The category, message, `terminal`,
   and the wire protocol are unchanged. A generic, non-external-boot job failure caused by this
   error therefore also shows `failure_detail_authority_reason` (from `worker._failure_context`).
   That is intended diagnostic output.
2. **Carry it into the result.** `ExternalBootAuthorityFailureContext` (`src/kdive/jobs/models.py`)
   gains `authority_reason: Literal["provider-conflict"] | None = None`, and `_FailureResult`
   requires `infrastructure_failure` whenever it is set. `_bound_failure`
   (`src/kdive/jobs/handlers/external_boot/runner.py`) copies the mark from a `CategorizedError`
   with that detail whose committed category is `infrastructure_failure`.
3. **Migration `0170_repeated_provider_conflict_terminal.sql`** adds a nullable
   `external_boot_authority_audit.failure_context jsonb` and an index on
   `(job_id, job_attempt)`. It patches `commit_external_boot_authority_result` in place (the 0160
   pattern: `pg_get_functiondef`, each old text exactly once, `EXECUTE`) at five texts:
   a. allow the key `authority_reason`;
   b. refuse (SQLSTATE `22023`) any `authority_reason` other than the string
      `provider-conflict`, or one with an `error_category` other than `infrastructure_failure`;
   c.–d. the final audit insert stores the context only for a `fail` that carries
      `authority_reason`, and `NULL` otherwise (two texts: the column list and the values);
   e. `v_terminal` gains a clause. The `fail` is terminal when its context carries
      `authority_reason` and an audit row exists for the same job with
      `job_attempt = v_job.attempt - 1`, `created_at >= v_job.created_at` (the current budget,
      ADR-0711), `outcome = 'result_requeued'`, and an equal `failure_context`.
4. ADR-0714, this spec, and the plan.

Out of scope:

- the approved exclusions in `WORK:SCOPE`;
- a provider-conflict raised during preparation (`_materialize_preparing`, before any
  `_bound_failure` block; see the failure model).

Unchanged: `worker._is_terminal`, the wire categories, `COMMITTABLE_ERROR_CATEGORIES`, the commit
function's fences, binding checks, and reservation-credit paths.

## Failure model

1. **Actors and deployments**
   - the worker role committing an external-boot result (local-libvirt and remote-libvirt);
   - the unchanged authority service;
   - an operator reading `jobs.get` and the audit table;
   - a deploy that applies `python -m kdive migrate` before it starts the roles (the case where
     it does not is accepted below).
2. **Invariants and assets at stake**
   - exactly-once reservation credit and the authority fences (ADR-0584, ADR-0620);
   - ADR-0711's per-budget generation limit;
   - a requeued job carries no category or context (migration 0163);
   - the immutable audit table keeps only the bounded context it needs.
3. **Accepted failure classes**
   - **Two transient provider faults in a row.** Two consecutive transient provider faults at
     the same phase end the job one attempt early. Accepted because the provider text never
     crosses the boundary (ADR-0584), so they cannot be told apart. The cost is one operator
     recycle.
   - **Preparation-phase provider-conflict.** It is raised outside `_bound_failure`, so no `fail`
     result exists. The worker leaves the job `running` for reclaim (ADR-0593), and the
     activate-purpose `fail` precondition refuses a `preparing` activation. That path keeps
     churning at lease-lapse pace up to `max_attempts` plus the ADR-0711 grant. Covering it needs
     a fence change, and the operator approved its exclusion (see `WORK:SCOPE`). Takeover
     acknowledgement failures are the same class.
   - **Rows from before the migration.** Audit rows written before 0170 have no context and never
     match, so the first repeat after deploy costs one more attempt.
   - **Worker running before the migration.** A worker running the new code before 0170 has a
     `fail` refused with `22023`. The job stays `running` until reclaim, which is no worse than
     the churn today and is bounded by `max_attempts`.
4. **Covered elsewhere**
   - provider defects: #2898 / #2867;
   - readiness race: #2899;
   - wedged marked jobs: ADR-0593.

## Success

- S1 — At attempt 2 of 3, a second consecutive `fail` with the same context carrying
  `authority_reason: provider-conflict` returns `job_state = 'failed'`. Today it returns `queued`.
- S2 — These still requeue below `max_attempts`: the first such `fail`; a `fail` without
  `authority_reason`; a repeat at a different `phase`; and a repeat whose prior audit row predates
  the job's `created_at`.
- S3 — On teardown, the terminal repeat leaves the seeded ready reservation (`ready`, 4096) and
  zero releases unchanged.
- S4 — An unknown `authority_reason`, or one paired with a category other than
  `infrastructure_failure`, is refused with `22023`.
- S5 — A sender `provider-conflict` error yields a bound result whose context carries
  `authority_reason`. Other peer reasons and other exceptions do not.

## Validation

- `focused-test`, S1–S4: `tests/db/test_migration_0170_repeated_provider_conflict.py` (real
  Postgres) and a patch-target exact-once test. Red before the migration exists.
- `focused-test`, S5: `tests/jobs/test_external_boot_authority_client.py` (sender details),
  `tests/jobs/handlers/external_boot/test_runner.py` (bound context), and
  `tests/jobs/test_external_boot_authority_models.py` (validator).
- `focused-test`, migration registry: the `tests/db/` migration-list and `[-K:]` window tests
  include `0170`.
