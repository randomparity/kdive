# Atomic authority boot readiness

## Problem

Authority activation and job success commit before Worker opens a second transaction
for the boot step. Debug attach needs that step, so worker loss leaves boot_first
for a succeeded activation. The current real-role reproduction fails on that state.
The existing recorder tests mock the fenced commit and cannot prove publication.

## Scope and design

Follow ADR-0835. Add0204_authority_activation_boot_step.sql without editing any
applied migration. Read the installed public.commit_external_boot_authority_result
regprocedure with its existing18-argument signature. Require exactly one occurrence
of the activation UPDATE followed by the recover/resolve-conflict ELSIF, as verified
on actual migrated0203 PostgreSQL. Use replace once and EXECUTE that complete current
function definition, preserving later amendments and its existing execution metadata.

After activation's active-state UPDATE and before the next branch, insert into
public.run_steps(run_id,step,state,result) the verified p_run_id, 'boot', 'succeeded'
and jsonb_build_object('system_id',p_system_id::text,'boot_outcome','ready').
Use ON CONFLICT(run_id,step) DO NOTHING, matching current evidence-conflict semantics.
Do not add a new lock or nested transaction. Existing Allocation/System/Run and
worker-incarnation locks and all authentication/binding/evidence checks precede the
branch. Job success, authority retirement and result audit remain in the same function.
An insert error must propagate and roll back all writes in that result invocation.

Remove the worker's activation follow-up call, _record_authority_boot_step,
_write_boot_step, _is_committed_activation, _activation_is_active and their retry
constants/unused imports. Keep deadline continuation, losing-result classification,
lease/reclaim behavior and exact carrier dispatch unchanged. No readiness repair loop.

Replace mocked recorder tests with real worker-role commits that drive the existing
handler/provider fixture and then debug attach preconditions. Database tests directly
exercise the SQL commit and observe from another connection. Test errors at the real
run_steps INSERT boundary, not by mocking a transaction helper or application model.

Preserve all existing reboot policy. ACTIVE authority RUN_BOOT is denied before the
force deletion branch; source and a real PG matrix probe establish this. Verify a
public force request preserves that denial and existing step, and run the existing
ordinary force regression. Separately delete a settled step under System/Run locks
and replay the retired authority result: it must not recreate the row. A distinct
fresh valid activation can publish its own readiness. Do not alter activation retry
identity or expand admission merely to construct an unreachable test scenario.

Global constraints: Python3.14/uv; x86_64 and ppc64le targets; public contracts
preserved; local commits only. Native x86_64 database/service proof is available.

## Failure model

- Actors/deployments: current workers and direct fenced SQL callers, concurrent
  result/teardown/reboot requests, PostgreSQL transaction failure, process loss.
- Protected assets: exact authority/Run/System binding; activation/job/authority/audit
  consistency; boot readiness; existing conflict and admission policy; SQL privileges.
- Covered classes: absent row insertion, rollback on insert error, stale/replayed
  carriers, non-activate operations, existing boot rows, unchanged migration metadata
  and amendment preservation, worker exit after the committed result.
- Accepted classes: existing conflicting boot rows remain byte-identical and may
  still fail attach. Preexisting missing readiness is not backfilled; applying this
  migration is not evidence that an old provider activation is still ready.
- Existing controls retained: incarnation and authority authentication, lifecycle
  fences and locks, bounded evidence validation, later result amendments and reclaim.

## Threat model

This moves a write into an existing SECURITY DEFINER function; it adds no callable
entrypoint or grant. Use only verified function parameters and fixed JSON keys,
fully qualify public.run_steps, retain empty search_path, owner, ACL and signature.
No caller-controlled SQL identifier or executable string enters the migration.
The guarded replacement aborts on missing/duplicate seams rather than replacing an
unexpected function. Runtime SQL errors propagate; no broad exception swallowing.

## Success and validation

1. Fresh real-role activation success and ready step are visible together with
   authority retirement and audit; debug attach observes the existing ready outcome.
2. A real readiness INSERT failure and a later audit INSERT failure each leave
   activation, job, authority, audit and readiness at their pre-call values from an
   independent connection. The latter proves an inserted ready row rolls back too.
3. Existing boot rows are unchanged; replay/stale and non-activate calls do not add
   readiness. Existing admitted force and authority denial semantics remain.
4. The migration preserves signature/security/ACL and later patches, rejects zero
   or duplicate seams, and the worker has one result-publication owner.

Use the two current database/worker files plus focused debug and ordinary force
cases, lint/whole-tree types, schema/migration guards and hooks. New ready assertions
must fail before0204. Controlled omitted insertion, overwritten conflict row and
swallowed insertion error must fail their corresponding tests, then be restored.
Enclosing final whole-branch code/security review and full CI remain required.
