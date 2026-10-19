# On-demand reconciliation failure reporting

## Problem

Issue #3239 reports nine failing repairs while `ops.reconcile_now` returns success.
The handler invokes the periodic repair plan with server database authority. An isolated
actual-server-LOGIN reproduction produced nine `InsufficientPrivilege` failures; the same
empty-state pass under actual reconciler authority had none. This is permission evidence,
not proof of installed cleanup effects.

## Scope and authority

The operator approved reporting design B in campaign design packet section 3239 on
2026-10-16 (SHA256 d94de308ce0f426d1f3db3158c6f60cb3b207c1b3bfbb31c526f737debe09418).
Issue #3239 permits either successful repair or an accurate explanation of inability to run.
The frozen charter is WORK:SCOPE q3239-40b8250a on that issue. Its exclusions remain general
reconciler redesign (existing subsystem owner) and remote object-sweep admission (existing
remote cleanup contract owner). [ADR-0785](../../adr/0785-on-demand-reconcile-failure-reporting.md)
records the reporting choice. No ownership migration is needed.

Preserve the repair plan, locking, server grants, periodic reconciler authority and execution.
Extend `ReconcileReport` in `src/kdive/reconciler/loop.py` with safe failure metadata and render
it in `src/kdive/mcp/tools/ops/reconcile/reconcile.py`. Tests and generated tool reference are
the remaining implementation surface. No SQL migration, credential injection, asynchronous
delegation or installed host operation is authorized.

## Behavior

Each caught repair exception retains its existing repair-kind name in `failures` and adds an
immutable `ReconcileFailure` containing `repair_kind`, `reason_code`, and `recovery`.
`psycopg.errors.InsufficientPrivilege` maps to `database_permission_denied` and
`periodic_reconciler`; other caught exceptions map to `repair_failed` and
`operator_inspection`. No exception message, SQL, credential, login, resource path or provider
output enters these fields. Existing internal logging is unchanged.

The on-demand response retains scalar counts, complete `repair_counts` and comma-joined
`failures`, and adds `repair_failures` as an ordered list of these three-field objects.
A nonempty `failures` produces status `error`, category `infrastructure_failure`, partial
counts and no suggested retry. Success remains status `ok` with existing next action and
an empty `repair_failures` list. The failure envelope uses static safe detail.
For legacy manually constructed reports without metadata, render each named failure with the
unknown-failure mapping rather than omitting its explanation.

A permission failure means the repair did not complete with this invocation's authority;
the periodic reconciler owns the required database authority. The recovery code directs the
operator to that existing role; it does not promise its next pass will succeed. Counts may
undercount committed work when a repair commits before raising. The wrapper must say so and
must instruct the caller to inspect failures rather than blindly repeat destructive cleanup.
Authorization denial still occurs before repair execution. Whole-pass errors and audit errors
retain their existing handling; this change describes isolated per-repair exceptions only.

## Failure model

- Prevent: false success on any isolated per-repair exception; raw exception data in the new
  response metadata; dropping any of the nine reported kinds.
- Contain: repairs retain existing isolation, so one exception does not starve later kinds;
  partial counts remain best effort and do not prove absence of side effects.
- Accept: whole-pass/audit failure handling, existing internal logging, provider-internal
  partial-success contracts and periodic execution scheduling remain unchanged.
- Deployment: disposable Postgres actual-role tests prove the server/reconciler split without
  installed host mutation. Installed terminal cleanup effects are outside this reporting fix.

## Success and validation

1. All nine issue kinds appear with permission reason/recovery in an actual-server-LOGIN
   on-demand call against isolated Postgres; actual reconciler LOGIN gives a clean control.
2. Injected unknown external failure yields only fixed codes, preserves successful sibling
   counts, and cannot leak its exception text through the response.
3. Existing clean-state, successful repair, denial, shared-lock and audit tests keep passing.
4. The FastMCP wrapper and regenerated `docs/guide/reference/ops.md` explain failure fields,
   authority ownership, partial counts and recovery without inviting blind retries.

Run focused MCP/reconciler tests, `just lint`, whole-tree `just type`, reference/doc guards,
then the mandatory managed pre-push `just ci` once per final candidate and hosted CI.
No installed `ops.reconcile_now` invocation or new live effect claim is required or authorized.
