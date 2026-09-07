# Worker-granted System mutation-obligation discharge

Issue: [#2302](https://github.com/randomparity/kdive/issues/2302).
Decision, with the caller inventory and the rejected alternatives:
[ADR-0629](../../adr/0629-worker-fenced-system-mutation-obligation-discharge.md).
Plan: [2026-09-07-worker-mutation-obligation-discharge-grant.md](../plans/2026-09-07-worker-mutation-obligation-discharge-grant.md).

## Problem

`systems.teardown` fails with `permission denied for table
remote_module_attempt_obligations`: the teardown reclaim path
(`src/kdive/jobs/handlers/system_reclaim.py:111`) runs a direct bulk `UPDATE` under
`kdive_worker` or `kdive_reconciler`, and `0126:230-232` grants both `SELECT` only.

The reported first-attempt-fails / immediate-retry-succeeds asymmetry is not a second
mechanism in the write. `src/kdive/jobs/handlers/systems.py:700-706` commits the terminal
`torn_down` System state in its own transaction, before the provider teardown and before the
failing discharge. The retry then reaches the terminal short-circuit at
`src/kdive/mcp/tools/lifecycle/systems/admin.py:451-462`, which returns `status: torn_down`
without enqueueing a job at all — so the retry never runs the statement that failed, and the
System's mutation obligations stay open with no later job that would discharge them. Privilege
is checked before row matching exactly as the issue reads it.

## Scope

One migration, two repository-layer edits, one caller switch, one test module. The plan holds
the file map and the exact code.

Out of scope, and unchanged: mapping `psycopg.errors.InsufficientPrivilege` to a platform error
in the shared worker exception path (#2302 item 2, deferred by operator decision — owner: a
separate follow-up); auditing other job handlers for the same pattern; the write-once trigger
semantics and the discharge idempotency contract; the two `kdive_server` activation-edge call
sites, which keep their direct write; and repairing obligations already leaked by this bug on
production Systems.

## Success

1. The teardown reclaim path completes under a connection holding the real `kdive_worker`
   grants, and the System's open mutation obligations carry reason `terminal_escape`.
2. The same path completes under the real `kdive_reconciler` grants, with the
   `reclaim_snapshot_ledger=True` its live call site passes.
3. Neither role can `UPDATE` the table directly: a direct `UPDATE` still raises
   `InsufficientPrivilege`, so the fix is proven to be the function and not a widened grant.
4. A principal that is a member of neither role, and can execute the function, is refused with
   SQLSTATE `42501` — the in-body gate, proven separately from the `EXECUTE` grant.
5. The discharge stays first-write-wins: a second reclaim leaves the stored
   `mutation_discharged_at` and reason unchanged.
6. The shared `discharge_system_mutation_obligations` still works under `kdive_server`, so the
   two activation-edge call sites are unaffected.
7. `just lint`, `just type`, `just test`, `just migration-order-check`, `just schema-guard`,
   and `just adr-status-check` are green.

## Threat model

**Boundaries added.** One: a `SECURITY DEFINER` function executable by `kdive_worker` and
`kdive_reconciler`, running as its owner. **Boundaries widened.** None — no table, column, or
sequence privilege changes, and no existing role's reach changes.

**Actors.** The trusted parties are the worker and reconciler processes, which already hold
their own database roles and are the only holders of the new `EXECUTE` grant. The untrusted
input crossing the boundary is the single `uuid` argument, which reaches the function from a
System id the caller already read from the database. An agent over MCP never reaches this
function: its only caller is the teardown reclaim path in
`src/kdive/jobs/handlers/system_reclaim.py`.

**Control per boundary.** Authorization is the in-body
`pg_has_role(session_user, …, 'member')` gate, which holds even if the `EXECUTE` grant is later
widened by accident; it is the reason the gate is not left to the grant alone. Injection is
bounded by the argument being a typed `uuid` and by `SET search_path = ''` with every
referenced name schema-qualified, so no object resolves through a caller-controlled path. The
write is bounded by the fixed statement: one table, two columns, one fixed literal reason,
`WHERE system_id = p_system_id AND mutation_discharged_at IS NULL`, with the write-once trigger
still enforcing first-write-wins beneath it. The function raises no message carrying caller
data. Serialization stays the caller's `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)`,
which is the same fence ADR-0605 verification takes
(`src/kdive/services/remote_module_attempt_preparation.py:80`).

**Out of scope.** A compromised worker or reconciler role can discharge obligations for any
System; bounding that further needs a per-System capability the callers do not carry, and both
roles can already read every row. The raw-privilege-string leak into `failure_message` is
addressed only incidentally, by the call succeeding; the error-mapping fix is deferred.

## Validation

The plan's per-task Verification inventories name each test case, its expected red, and its
green command. Every arm runs against disposable Postgres via testcontainers and needs a
reachable Docker daemon; `KDIVE_REQUIRE_DOCKER=1` turns the skip into a hard failure and is how
these are proven to have run rather than skipped. Success criteria 1-6 each map to one case in
`tests/db/test_worker_system_mutation_discharge.py`; criterion 7 is the guardrail suite, whose
migration-ordering and schema-immutability contracts are carried by
`just migration-order-check` and `just schema-guard` rather than by a task-local test.
