# Repair mutation obligations already leaked on torn-down Systems

Issue: [#2326](https://github.com/randomparity/kdive/issues/2326).
Decision, with the path inventory and the rejected alternatives:
[ADR-0632](../../adr/0632-leaked-mutation-obligations-repaired-by-reconciler.md).
Plan: [2026-09-08-repair-leaked-mutation-obligations.md](../plans/2026-09-08-repair-leaked-mutation-obligations.md).

## Problem

A System can reach `torn_down` with a mutation obligation still open. The row keeps
`mutation_discharged_at IS NULL`, so `retained_owners`
(`src/kdive/db/remote_module_attempt_obligations.py:531-561`) keeps returning its attempt and the
module-volume sweep never reclaims that attempt's `source.ext4` and `scratch.ext4`. Nothing raises,
nothing logs at a level an operator sees, and there is no operator command that repairs it.

No later job discharges it either: `systems.teardown` returns `torn_down` from its terminal
short-circuit (`src/kdive/mcp/tools/lifecycle/systems/admin.py:451-462`) without enqueueing a job.
ADR-0629 stopped new leaks on the teardown reclaim path and repaired none, recording the remediation
as separate work.

## Scope

One new reconciler repair function in `src/kdive/reconciler/repairs/systems.py`, one catalog entry
in `src/kdive/reconciler/loop.py`, one ADR, and one test module. The plan holds the file map and the
exact code. **Migration 0153 is reserved for this change and deliberately left unused** — the ADR
records why the reconciler lane is the shape and a data migration is not.

Out of scope, and unchanged: the teardown ordering defect that creates new leaks — committing
`TORN_DOWN` after a successful discharge rather than before it (owner: a separate follow-up, not yet
filed); the write-once trigger semantics and the discharge idempotency contract (owner: ADR-0588 /
ADR-0629); any widening of `kdive_worker` or `kdive_reconciler` privileges beyond ADR-0629's
function, which ADR-0629 rejects explicitly; and a System in `failed` carrying an open obligation,
which #2326's predicate does not select (owner: unfiled follow-up candidate, reported by this run to
the campaign orchestrator).

The affected-row count #2326 asks for before choosing a shape cannot be taken from a development
checkout. The lane is the shape whose correctness does not depend on that number: it returns 0
against a database with no leaked rows and drains whatever backlog exists on its first pass.

## Success

1. A System in `torn_down` carrying an obligation with `mutation_discharged_at IS NULL` and no
   active teardown job is discharged, with `mutation_discharge_reason = 'terminal_escape'`, and the
   repair returns a count of 1 for it.
2. The same System is left untouched, and the count is 0, while a job at dedup key
   `'<system_id>:teardown'` is `queued` or `running` — the in-flight exclusion #2326 requires.
3. A System that is not `torn_down` is left untouched however its obligations stand.
4. An obligation already discharged is left byte-identical: a second pass changes neither
   `mutation_discharged_at` nor `mutation_discharge_reason`, so first-write-wins is preserved.
5. The discharge succeeds over a connection holding the real `kdive_reconciler` grants and no
   others, proving it runs through ADR-0629's `SECURITY DEFINER` function rather than a widened
   table privilege.
6. The repair is registered in the reconciler catalog after `abandoned_jobs`, and `ALL_REPAIR_KINDS`
   still matches a fully populated `_repair_plan`.
7. Only the selected System's obligations are discharged: a second torn-down System with its own
   open obligation, excluded by an active teardown job, is unaffected in the same pass.
8. `just ci` is green.

## Threat model

**Boundaries added.** None. The lane calls ADR-0629's existing `SECURITY DEFINER` function through
the existing repository method; no function, grant, role, or table privilege changes.
**Boundaries widened.** None. The reconciler already holds `EXECUTE` on that function
(`src/kdive/db/schema/0152_worker_system_mutation_discharge.sql:39-40`) and already calls it on the
authority-teardown repair path (`src/kdive/reconciler/repairs/jobs.py:98-105`).

**Actors.** The trusted party is the reconciler process, which holds `kdive_reconciler`. No
untrusted input crosses into this code: every value in the statement is a System id the lane read
from `systems` in the same pass, and an agent over MCP reaches no part of it.

**Control per boundary.** Authorization is ADR-0629's in-body `pg_has_role(session_user, …)` gate
plus the `EXECUTE` grant, unchanged. The write is bounded by that function's fixed statement — one
table, two columns, the fixed literal reason, `WHERE system_id = … AND mutation_discharged_at IS
NULL` — so this lane cannot write anything the teardown path could not. What this change adds is a
new *caller*, and the control on it is the candidate predicate: `state = 'torn_down'` plus the
absence of an active teardown job, re-read inside the per-System locked transaction.

**Out of scope.** The predicate is not a fence. The teardown handler releases the System lock before
its provider call (`src/kdive/jobs/handlers/systems.py:700-706`), so nothing serializes this lane
against that window; the re-read narrows it to the width every other reconciler repair carries. The
residual ADR-0629 records — any process holding a reconciler login can discharge any System's open
obligations — is unchanged and not widened by adding a caller inside the reconciler.

## Validation

The plan's per-task Verification inventories name each test case, its expected red, and its green
command. Every arm runs against disposable Postgres via testcontainers and needs a reachable Docker
daemon; `KDIVE_REQUIRE_DOCKER=1` turns the skip into a hard failure and is how these are proven to
have run rather than skipped. Success criteria 1-4 and 7 map to cases in
`tests/reconciler/test_leaked_mutation_obligation_repair.py`; criterion 5 to the
`kdive_reconciler` LOGIN arm in that module, built on the existing `authority_role_dsns` fixture;
criterion 6 to `...::test_repair_is_registered_after_abandoned_jobs` in the same module, because the
existing `test_all_repair_kinds_matches_a_fully_populated_plan` derives both sides of its assertion
from `_REPAIR_CATALOG` and so stays green on a missing entry; criterion 8 is the guardrail suite.
