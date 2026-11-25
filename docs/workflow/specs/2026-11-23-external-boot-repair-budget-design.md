# Bounded external-boot repair admission

## Problem

An exhausted automatic job is selected as the source of its replacement. Its new UUID produces
a new repair key even though the activation has not progressed. The real-PG root admits
[1,1,1] instead of [1,0,0] under the actual reconciler role after simulated pre-commit exhaustion.

## Scope

Implement [ADR-0855](../../adr/0855-bounded-external-boot-repair-admission.md) in the existing
repair owner. Add three typed phase facts to its private Candidate and migrate its four direct
unit-test constructions. Keep source/live query ownership, payload assembly and queue admission
in their existing functions. Add no table, policy registry, worker protocol or compatibility path.
Public producers, idempotency, schema, state-machine edges and ErrorCategory remain unchanged.

## Failure model

- Actors and deployments: configured reconcilers inspect Postgres and enqueue existing worker
  operations for local/remote-libvirt external-boot activations. Concurrent sweeps remain possible.
- Invariants and assets: bounded automatic admission for unchanged evidence; current-authority live
  ownership, original authorization/project matching, typed payloads and durable queue uniqueness;
  real lifecycle progress must not be mistaken for unchanged retry bookkeeping.
- Accepted failure classes: exhausted automatic slots do not guarantee autonomous convergence;
  a retained validated non-automatic source is required. Idempotent replay/recycling does not
  promise an extra automatic slot. Legacy keys may permit one new finite slot after upgrade.
  Candidate snapshots can age; existing transaction and worker/authority fences remain decisive,
  without a new linearizable snapshot protocol or wall-clock recovery guarantee.
- Covered elsewhere: queue-recycle-policy retains direct-request admission/replay policy;
  ADR0839 retains scheduling progress. Provider execution and authority result commits remain
  their existing owners. No new operator/host configuration or hardware proof is claimed.

## Success

1. Source SQL excludes the automatic dedup namespace before row_number/rank100. Live SQL keeps
   automatic jobs and its exact authority/operation/lease predicates. Preserve kind, activation,
   System, Run, plan and project filters and typed source validation, including malformed rows.
2. Candidate state/current_attempt_id/readiness_deadline come from the selected durable row.
   Activation selects its activation deadline, recovery the current attempt deadline, other lanes
   NULL. Preserve each query's FROM/JOIN/WHERE and outer UUID keyset/100-candidate semantics.
3. The admission identity keeps activation/operation/origin UUID plus a SHA-256 fingerprint of
   JSON [activation_state, current_attempt_id-or-null, UTC-deadline-or-null, authority_instance].
   Keep existing opaque repair prefixes and marker limit. For unchanged retained source rows,
   automatic jobs cannot change the selected source UUID. Never use mutable bookkeeping.
4. Repeated terminal/exhausted repairs reuse the same NEVER-recycled slot. Explicit new source,
   phase state/attempt/deadline or authority route changes may use a new slot; a live matching
   automatic successor still suppresses it. Existing direct-request recycling keeps its own
   fresh explicit attempt budget without an additional automatic-slot guarantee.
5. Preserve per-candidate transactions/cancellation, role restrictions, source authorizing data,
   payload/routing checks, queue defaults, cursor advancement/wrap and safe failure diagnostics.
   Concurrent same-tuple sweeps cannot admit more than one slot through durable key uniqueness.

## Validation

Use real Postgres, migrated role DSNs and existing seed_case/resolver helpers, replacing no repair
policy with mocks. Repeat repair/exhaust/sweep for each actual operation across all four lanes.
Assert one admission and unchanged durable phase; keep all transaction/role/live/timeout regressions.
Build a historical chain larger than100 and prove source filtering cannot be displaced by it.
Keep automatic live suppression when a later explicit source exists; reject source-less chains.
Prove a new explicit source, actual persisted activation phase and authority-route change permit
new finite slots, while updated_at/lease/generation bookkeeping cannot renew one. Check equivalent
timestamps in different DB session time zones. Concurrent sweeps retain one admitted slot.
Use controlled missing-filter, missing-phase/route and recycled-slot faults where executable.

Run focused budget tests red before source changes; add real-PG role and connected worker/public
recovery replay regressions, then relevant reconciler tests, whole lint/types/hooks/docs/records.
Verify four eligibility FROM/JOIN/WHERE bodies, outer query/cursor transactions, live predicates,
source validation and immutable SQL remain intact apart from authorized projections/filter/key.
Run budget cases through an actual built/installed wheel with verified module origin and bytes.
Fresh code/security review precedes local commit and exact official health resolution/attribution.
