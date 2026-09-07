# Worker-granted System mutation-obligation discharge

Issue: [#2302](https://github.com/randomparity/kdive/issues/2302).
Decision record: [ADR-0629](../../adr/0629-worker-fenced-system-mutation-obligation-discharge.md).

## Problem

`systems.teardown` fails with `permission denied for table
remote_module_attempt_obligations`. The teardown job handler reaches
`RemoteModuleAttemptObligationRepository.discharge_system_mutation_obligations`
(`src/kdive/db/remote_module_attempt_obligations.py:305-322`) through
`src/kdive/jobs/handlers/system_reclaim.py:110-113`, and that method issues a direct `UPDATE`
on a table where `kdive_worker` holds `SELECT` only
(`src/kdive/db/schema/0126_remote_module_attempt_obligations.sql:230-232`). The reconciler's
authority-System teardown repair (`src/kdive/reconciler/repairs/jobs.py:98-103`) calls the same
method under `kdive_reconciler`, which `0126:231-232` restricts identically.

The reported first-attempt-fails / immediate-retry-succeeds asymmetry is not a second
mechanism in the write. `src/kdive/jobs/handlers/systems.py:700-706` commits the terminal
`torn_down` System state in its own transaction, before the provider teardown and before the
failing discharge. The retry then reaches the terminal short-circuit at
`src/kdive/mcp/tools/lifecycle/systems/admin.py:451-462`, which returns `status: torn_down`
without enqueueing a job at all — so the retry never runs the statement that failed, and the
System's mutation obligations stay open with no later job that would discharge them. Privilege
is checked before row matching exactly as the issue reads it.

## Scope

One migration, one repository method, one integration test module.

- **`src/kdive/db/schema/0152_worker_system_mutation_discharge.sql` (new).** Defines
  `public.discharge_system_mutation_obligations(p_system_id uuid) RETURNS integer`,
  `SECURITY DEFINER`, `SET search_path = ''`. Body: reject a caller that is a member of
  neither `kdive_worker` nor `kdive_reconciler` with SQLSTATE `42501`; reject a null system id
  with SQLSTATE `22023`; then the single `UPDATE` setting `mutation_discharged_at =
  pg_catalog.now()` and `mutation_discharge_reason = 'terminal_escape'` for rows of that
  System where `mutation_discharged_at IS NULL`; return the row count. `REVOKE ALL … FROM
  PUBLIC`, then `GRANT EXECUTE … TO kdive_worker, kdive_reconciler`.
- **`src/kdive/db/remote_module_attempt_obligations.py`.**
  `discharge_system_mutation_obligations` keeps its signature, its return contract, and its
  surrounding `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)`, and replaces the inline
  `UPDATE` with a call to the new function.
- **`tests/db/test_worker_system_mutation_discharge.py` (new).** The role-grant proofs below.

Out of scope, and unchanged: mapping `psycopg.errors.InsufficientPrivilege` to a platform error
in the shared worker exception path (#2302 item 2, deferred by operator decision — owner: a
separate follow-up); auditing other job handlers for the same pattern; the write-once trigger
semantics and the discharge idempotency contract.

## Success

1. A connection holding the real `kdive_worker` grants runs
   `reclaim_system_core_after_provider_teardown(..., discharge_mutation_obligations=True)` to
   completion and the System's open mutation obligations are discharged with reason
   `terminal_escape`.
2. The same call under the real `kdive_reconciler` grants also completes.
3. Neither role can `UPDATE` the table directly: a direct `UPDATE` still raises
   `InsufficientPrivilege`, so the fix is proven to be the function and not a widened grant.
4. A principal that is a member of neither role, and can execute the function, is refused with
   SQLSTATE `42501` — the in-body gate, proven separately from the `EXECUTE` grant.
5. The discharge stays first-write-wins: a second call for the same System returns `0` and
   leaves the stored reason and timestamp unchanged.
6. `just lint`, `just type`, `just test`, `just migration-order-check`, `just schema-guard`,
   and `just adr-status-check` are green.

## Threat model

**Boundaries added.** One: a `SECURITY DEFINER` function executable by `kdive_worker` and
`kdive_reconciler`, running as its owner. **Boundaries widened.** None — no table, column, or
sequence privilege changes.

**Actors.** The trusted parties are the worker and reconciler processes, which already hold
their own database roles and are the only holders of the new `EXECUTE` grant. The untrusted
input crossing the boundary is the single `uuid` argument, which reaches the function from a
System id the caller already read from the database. An agent over MCP never reaches this
function: it is called only from job-handler and reconciler code paths.

**Control per boundary.** Authorization is the in-body
`pg_has_role(session_user, …, 'member')` gate, which holds even if the `EXECUTE` grant is
later widened by accident; it is the reason the gate is not left to the grant alone.
Injection is bounded by the argument being a typed `uuid` and by
`SET search_path = ''` with every referenced name schema-qualified, so no object resolves
through a caller-controlled path. The write is bounded by the fixed statement: one table, two
columns, one fixed literal reason, `WHERE system_id = p_system_id AND mutation_discharged_at
IS NULL`, with the write-once trigger still enforcing first-write-wins beneath it. The
function raises no message carrying caller data. Serialization stays the caller's
`advisory_xact_lock(conn, LockScope.SYSTEM, system_id)`, which is the same fence ADR-0605
verification takes (`src/kdive/services/remote_module_attempt_preparation.py:80`).

**Out of scope.** A compromised worker or reconciler role can discharge obligations for any
System; bounding that further needs a per-System capability the callers do not carry, and both
roles can already read every row. The raw-privilege-string leak into `failure_message` is
addressed only incidentally, by the call succeeding; the error-mapping fix is deferred.

## Validation

Every arm below runs against disposable Postgres via testcontainers and needs a reachable
Docker daemon; `KDIVE_REQUIRE_DOCKER=1` turns the skip into a hard failure and is how these
are proven to have run rather than skipped.

| Contract | Mode | Evidence |
|---|---|---|
| Worker-role reclaim completes and discharges | `focused-test` | `test_worker_role_teardown_reclaim_discharges` |
| Reconciler-role reclaim completes | `focused-test` | `test_reconciler_role_teardown_reclaim_discharges` |
| No table-level `UPDATE` was granted | `focused-test` | `test_worker_role_direct_update_still_denied` |
| In-body role gate refuses a non-member | `focused-test` | `test_non_member_execute_is_refused` |
| First-write-wins is preserved | `focused-test` | `test_second_discharge_is_a_noop` |
| Migration ordering and schema immutability | `task-test-not-applicable` | `just migration-order-check` and `just schema-guard` are the executable consumers of this contract; a task-local test would restate their git-diff comparison against a base ref the test cannot fix. |
