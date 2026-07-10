# Spec — audit successful `jobs.cancel` transitions (#1083)

- **Status:** Draft
- **Date:** 2026-07-10
- **Issue:** #1083 (follow-up to #1080 / PR#1082)
- **ADR:** none — aligns `jobs.cancel` with the existing `audit.record`
  convention (ADR-0028 ordering, ADR-0006/0020 append-only audit); no change to
  the audit model, so no new ADR.
- **Branch:** `feat/audit-jobs-cancel-1083` (base `main`)

## Problem

`jobs.cancel` writes **no audit row on a successful cancel**.
`cancel_job` (`src/kdive/mcp/tools/jobs.py:205-240`) transitions a job
`queued`/`running` → `canceled` and returns without an `audit.record` call —
`src/kdive/mcp/tools/jobs.py` has no `audit` import at all. Only *denials* are
audited: `DenialAuditMiddleware`
(`src/kdive/mcp/middleware/denial_audit.py`) records a `RoleDenied` at the
dispatch boundary. So the audit log shows who was *refused* a cancel but not who
*performed* one.

The other mutating lifecycle handlers record their transitions inside the same
transaction as the mutation:

- `systems.reprovision` — `_admit_reprovision`
  (`src/kdive/mcp/tools/lifecycle/systems/admin.py:253`): `update_state` then
  `audit.record`, both on `conn` under one `conn.transaction()`.
- `systems.teardown` — `_teardown_locked`
  (`src/kdive/mcp/tools/lifecycle/systems/admin.py:327`) audits the denial path;
  the success path enqueues under the same outer transaction.
- `systems.power` / destructive gate — `control.py:147` audits the denial.

Since PR#1082 lowered `jobs.cancel` from operator to a per-kind gate, two actor
sets can cancel jobs: a **contributor** cancelling its own leaseholder-lifecycle
job (`CONTRIBUTOR_CANCELABLE_JOB_KINDS`), and an **operator** cancelling a
provision-lane or destructive job (provision/reprovision/teardown/force_crash).
An operator cooperatively aborting another principal's in-flight
destructive/provision job is exactly the cross-principal action that warrants an
attribution trail, and today it leaves no record.

## Goals

1. A successful `jobs.cancel` (an actual `queued`/`running` → `canceled`
   transition) writes exactly **one** `audit_log` row via `audit.record`,
   composed into the **same transaction** as the state mutation, following the
   `admin.py`/`control.py` pattern (ADR-0028: the audit is inside the mutation's
   transaction and cannot itself raise past the mutation).
2. The row carries: `tool="jobs.cancel"`, `object_kind="jobs"`,
   `object_id=<job id>`, `project=<job's owning project>`, a **readable**
   `transition="<kind>:<prior_state>->canceled"` that names the job kind in a
   plaintext column, and `args={"job_id": <id>, "kind": <job kind>}` for
   `args_digest` correlation. The kind must be in the transition string, not only
   in `args`: `args` is stored one-way as `args_digest` (SHA-256), so a kind that
   lived only there would not be readable back from the `audit_log` row — and
   "which job kind was cancelled" is an explicit issue goal (see D4).
3. Denial auditing (`DenialAuditMiddleware`) is unchanged.

## Non-goals

- **No audit row for a no-op cancel** of an already-terminal job (see Decision
  D3). Denials remain the middleware's job; a terminal-state `IllegalTransition`
  is neither a denial nor a transition.
- No change to `audit.record`, the `audit_log` schema, `DenialAuditMiddleware`,
  or `JOBS.update_state`. No DB migration.
- Scope is `jobs.cancel` only. `jobs.get`/`list`/`wait` are non-mutating reads,
  out of scope.

## Decisions

### D1 — Compose audit into the cancel transaction

`JOBS.update_state` opens its own `conn.transaction()` internally
(`db/repositories.py:172`), which nests as a savepoint under an outer
`conn.transaction()`. Wrap the mutation and the audit write in one outer
transaction on a single pooled connection:

```
prior_state = existing.state.value  # from the pre-authz read (see D2)
async with pool.connection() as conn, conn.transaction():
    job = await JOBS.update_state(conn, uid, JobState.CANCELED)
    await audit.record(conn, ctx, audit.AuditEvent(
        tool="jobs.cancel",
        object_kind="jobs",
        object_id=uid,
        transition=f"{job.kind.value}:{prior_state}->canceled",
        args={"job_id": job_id, "kind": job.kind.value},
        project=_project(job),
    ))
```

`update_state` raising `IllegalTransition`/`ObjectNotFound` aborts before
`audit.record`, so no orphan audit row is written for a failed cancel. If
`audit.record` were to raise (e.g. the misattribution guard), the outer
transaction rolls back the cancel too — audit and mutation commit together or
neither does (ADR-0028).

`audit.record`'s guard requires `event.project in ctx.projects`. `cancel_job`
already established `_in_scope(existing, ctx)` (the owning project is granted)
before mutating, and a job's owning project never changes, so the guard cannot
fire on the success path.

### D2 — `prior_state` source

The `transition` string uses `existing.state.value` — the job state observed by
the pre-mutation authz read (`existing = await JOBS.get(conn, uid)`). Rationale:
`update_state`'s `can_transition` guard permits `queued`→`canceled` and
`running`→`canceled` only, so if the mutation succeeds the recorded prior state
was a legal cancel source. The two legal sources (`queued`/`running`) are the
only values `existing.state` can hold on the success path unless a concurrent
transition intervened between the read and `update_state`'s `FOR UPDATE` — in
which case `update_state` raises `IllegalTransition` and no row is written. The
prior state is an audit annotation, not a control decision, so an
already-in-hand value is preferred over a second `SELECT ... FOR UPDATE`.

### D3 — No-op cancel records nothing

Cancelling an already-terminal job (`succeeded`/`failed`/`canceled`) raises
`IllegalTransition`; the handler returns a `CONFIGURATION_ERROR` envelope
carrying `current_status` and writes **no** audit row. An audit event records a
*transition*; a no-op performs none. This keeps `jobs.cancel` symmetric with
`_admit_reprovision`/`_teardown_locked`, which audit only after the state
actually changes. (Authz denials are separately covered by
`DenialAuditMiddleware` and are out of this handler.)

### D4 — Job kind lives in the readable `transition` column, not only `args`

`audit.record` stores `args` one-way as `args_digest = SHA-256(args)`
(`security/audit.py:34-35,119`) — tamper-evidence/correlation, not a readable
field. So a kind that lived only in `args` would not be recoverable from an
`audit_log` row, yet "which job kind was cancelled" is an explicit issue goal.
The readable `audit_log` columns are `tool`, `object_kind`, `object_id`,
`transition`, `project`; `object_kind="jobs"` names the table, not the
build/teardown/force_crash kind. The job kind is therefore encoded in the
`transition` string as `"<kind>:<prior_state>->canceled"`, following the
destructive-op gate precedent that puts the op kind in `transition`
(`f"{op_kind.value}:denied"`, `control.py:154`, `admin.py:216`). `kind` is
*also* kept in `args` so `args_digest` correlates the row to the tool call.

## Acceptance criteria

- [ ] A contributor cancelling a running leaseholder-kind job
      (e.g. `authorize_ssh_key`) writes exactly **one** `audit_log` row with
      `principal`=caller, `tool="jobs.cancel"`, `object_kind="jobs"`,
      `object_id`=job id, `project`=job's owning project, and
      `transition="authorize_ssh_key:running->canceled"` (the readable kind is
      recoverable from the row without reversing `args_digest`).
- [ ] An operator cancelling a queued destructive-kind job (e.g. `teardown`)
      writes exactly **one** `audit_log` row with
      `transition="teardown:queued->canceled"` and `args_digest` covering
      `{"job_id", "kind"}`.
- [ ] A no-op cancel of an already-terminal job writes **zero** `audit_log`
      rows and still returns the `current_status`-bearing error envelope.
- [ ] A cancel denied by role (`RoleDenied`) writes **zero** rows from
      `cancel_job` itself (the middleware's denial row is unchanged and out of
      scope for this handler's test).
- [ ] Structural: the `update_state` and `audit.record` calls sit inside one
      outer `conn.transaction()` on the same connection (code-review check), so
      the mutation and its audit row commit or roll back together. A
      fault-injection test (forcing `audit.record` to raise and asserting the
      job stays non-terminal) is optional given the D1 argument that the guard
      cannot fire on the success path.
- [ ] `just ci` is green.

## Failure modes and edge cases

- **Concurrent transition (read→update race).** `existing.state` reads
  `running`, a worker completes the job to `succeeded` before our
  `update_state`; `update_state`'s `FOR UPDATE` sees `succeeded`, raises
  `IllegalTransition`, we return the error envelope and write no row. No
  mis-attributed transition is recorded.
- **`ObjectNotFound` at update.** Job deleted between read and update: return
  `_not_found`, no audit row.
- **`audit.record` raising.** The outer `conn.transaction()` rolls back the
  cancel; the job is not left `canceled` with no trail. (In practice the
  project-grant guard cannot fire here — see D1.)
- **Idempotent re-cancel.** A second `jobs.cancel` on an already-`canceled` job
  is the D3 no-op: `current_status="canceled"` error, zero rows. The first
  cancel's single row is the only attribution.

## Rollback

Additive: one `audit` import, one `audit.record` call, and wrapping the existing
`update_state` call in an outer transaction. No schema/migration, no change to
any other tool or to `audit.record`. Rollback is reverting the branch; a reverted
build simply stops writing the new row and behaves exactly as today.
