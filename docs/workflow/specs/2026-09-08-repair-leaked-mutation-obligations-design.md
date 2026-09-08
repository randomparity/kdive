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
exact code. This change writes no migration; ADR-0632 records why the reconciler lane is the shape
and a data migration is not, and releases the reserved number 0153.

Out of scope, and unchanged: the teardown ordering defect that creates new leaks — committing
`TORN_DOWN` after a successful discharge rather than before it (owner: a separate follow-up, not yet
filed); the write-once trigger semantics and the discharge idempotency contract (owner: ADR-0588 /
ADR-0629); any widening of `kdive_worker` or `kdive_reconciler` privileges beyond ADR-0629's
function, which ADR-0629 rejects explicitly; and a System in `failed` carrying an open obligation,
which #2326's predicate does not select. That last one is a permanent leak with a known cause, not
an open question — ADR-0632 Consequences records the evidence — and this run reports the sized
follow-up to the campaign that dispatched it.

The affected-row count #2326 asks for before choosing a shape cannot be taken from a development
checkout. The lane is the shape whose correctness does not depend on that number: it returns 0
against a database with no leaked rows and drains whatever backlog exists on its first pass.

## Success

1. A System in `torn_down` carrying an obligation with `mutation_discharged_at IS NULL` and no
   teardown job that is active or recently terminal is discharged, with
   `mutation_discharge_reason = 'terminal_escape'`.
2. The same System is left untouched, and the count is 0, while its teardown job at dedup key
   `'<system_id>:teardown'` is `queued`, `running`, **or terminal (`canceled`/`failed`/`succeeded`)
   within the settle window** — the in-flight exclusion #2326 requires. The terminal-and-recent arm
   is what covers an operator `jobs.cancel` of a teardown whose handler keeps running (ADR-0632
   Context).
3. A System that is not `torn_down` is left untouched however its obligations stand.
4. An obligation already discharged is left byte-identical: a second pass changes neither
   `mutation_discharged_at` nor `mutation_discharge_reason`, so first-write-wins is preserved.
5. The discharge succeeds over a connection holding the real `kdive_reconciler` grants and no
   others, proving it runs through ADR-0629's `SECURITY DEFINER` function rather than a widened
   table privilege.
6. The repair is registered in the reconciler catalog after `abandoned_jobs`.
7. Only the selected System's obligations are discharged: a second torn-down System with its own
   open obligation, deferred by an active teardown job, is unaffected in the same pass.
8. The returned count is **obligation rows**, matching the repair-kind name: one System carrying two
   open obligations reports 2.
9. One candidate that raises does not starve the rest: the remaining candidates are still
   discharged and the count reflects them.
10. `just ci` is green.

## Threat model

**Boundaries added or widened.** None. The lane calls ADR-0629's existing `SECURITY DEFINER`
function through the existing repository method, and the reconciler already holds `EXECUTE` on it
(`src/kdive/db/schema/0152_worker_system_mutation_discharge.sql:39-40`) and already calls it on the
authority-teardown repair path (`src/kdive/reconciler/repairs/jobs.py:98-105`). No function, grant,
role, or table privilege changes.

**Actors.** The only actor is the reconciler process, holding `kdive_reconciler`. No untrusted value
enters the statement: every parameter is a System id the lane read from `systems` in the same pass,
and an agent over MCP reaches no part of it.

**Control per boundary.** Authorization and the write's bound are ADR-0629's, unchanged — the
in-body `pg_has_role` gate, the `EXECUTE` grant, and one fixed statement over two columns with a
literal reason. What this change adds is a new *caller*, and the control on it is the candidate
predicate: `state = 'torn_down'` plus no active-or-recently-terminal teardown job, re-read inside the
per-System locked transaction.

**Out of scope.** ADR-0632 Consequences states the settle window's limit and the residual ADR-0629
already accepted; neither is narrowed or widened here.

## Validation

The plan's per-task Verification inventory names each test case, its expected red, and its green
command. Every arm runs against disposable Postgres via testcontainers and needs a reachable Docker
daemon; `KDIVE_REQUIRE_DOCKER=1` turns the skip into a hard failure and is how these are proven to
have run rather than skipped. Criteria 1-4 and 7-9 map to cases in
`tests/reconciler/test_leaked_mutation_obligation_repair.py`; criterion 5 to the `kdive_reconciler`
LOGIN arm in that module, built on the existing `authority_role_dsns` fixture; criterion 6 to
`...::test_repair_runs_after_abandoned_jobs` in the same module, because the existing
`test_all_repair_kinds_matches_a_fully_populated_plan` derives both sides of its assertion from
`_REPAIR_CATALOG` and so stays green on a missing entry; criterion 10 is the guardrail suite.
