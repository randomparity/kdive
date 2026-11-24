# Shared lifecycle service ownership

## Problem

Shared System teardown and reconciler repairs import worker-handler helpers. Payload import
also loads the external-boot registrar. The current dependency direction hides ownership.

## Scope and architecture

Implement [ADR0851](../../adr/0851-shared-lifecycle-services-own-admission-and-reclaim.md).
Move worker `external_boot/admission.py` to `services/external_boot/payloads.py`; move worker
`system_reclaim.py` to `services/systems/reclaim.py`. Redirect the enumerated four/five
production imports and six direct test imports. Keep the reverse-admission service untouched.
Remove old modules; no forwarders/package exports. Update the recovery import-closure guard,
artifact-discard module reference and worker-write evidence paths/verified lines.

## Success

1. Existing teardown/reconciler consumer imports expose service-owned helpers in fresh Python
   processes without loading `kdive.jobs.handlers` or its children. Direct service cold imports
   also avoid that tree. The new two-case permanent test fails before migration and passes after.
2. Moved executable AST is unchanged, apart from module documentation. Callers change only
   import ownership. Keep activation-derived identity/PREP/plan/timing and transaction policy;
   keep post-provider-absence flags, System lock/transactions, fenced discharge, bootstrap/pcap
   cleanup and bounded artifact retirement with retained rows on incomplete/failed batches.
3. The exact import/evidence readers resolve new ownership. Public MCP/port/store contracts,
   role grants, package initializers and all198SQL stay unchanged; actual wheel matches source
   and ships the new modules without the retired internal modules.

## Failure model

- Actors/deployments: HTTP/server admission, workers and reconciler share the Python codebase;
  configured provider ports, existing Postgres roles and artifact store supply their boundaries.
- Assets/invariants: payload authority provenance, admission transaction ownership, physical-
  absence prerequisite, fenced cleanup and retryable rows; correct import direction/packaging.
- Accepted classes: existing provider/store failures and bounded cleanup continuation retain
  their current behavior; this relocation adds no new recovery guarantee. Source users of the
  retired internal module paths must follow the move; no public compatibility is promised there.
- Covered elsewhere: existing admission matrix and authority protocols; worker/reconciler DB
  fencing/role tests; final campaign CI/reviews and separate real remote/POWER validation.

## Threat model

- Boundaries: unchanged provider/DB/store operations, moved to shared service ownership. No
  network, permission, SQL or executable entrypoint is added or widened.
- Actors: tenants invoke existing MCP services; operator configures roles/provider endpoints.
- Controls: keep the same activation-derived fields/validation and caller flags, physical-
  absence checks, System locks/transactions and role-fenced discharge; compare source AST and
  caller bodies, run real-PG service/worker/role tests. No new logger diagnostic text/level.
- Outside scope: changing those policies, host trust or artifact retirement guarantees belongs
  to their existing owners; this unit changes dependency direction and module identity only.

## Validation

Focused-test inventory: two fresh-process existing-consumer imports fail before the move;
real-PG regressions and AST/SQL proof preserve runtime contracts. Controlled faults temporarily
restore each old helper/consumer import separately, require the intended import regression and
restore/remove only owned temporary files. Validate direct cold imports, import closure and
worker-write evidence. Run lint/whole types, hooks, documentation/record/status checks and actual
wheel consumer proof outside checkout. One local source commit follows these gates and fresh
scoped code/security reviews precede health resolution. Final campaign gates remain required.
