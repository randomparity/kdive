# Teardown of a `failed` System reclaims and leaves it `failed` (#2908)

## Scope and authority

Campaign scope for issue #2908, token `q2908-c5a83c5d`. The operator approved these exclusions on
2026-09-29: widening the `failed` state machine (spec owner, checkpoint first; not chosen here);
the readiness-marker defect (#2907); the reconciler's orphan-teardown behaviour for failed Systems
(ADR-0441). The operator left the teardown contract to this design.

The decision is recorded as a dated amendment to
[ADR-0435](../../adr/0435-reclaim-failed-provision-artifacts.md), whose Context says a `failed`
System can never run teardown. The amendment also covers ADR-0441's repetition of that premise;
ADR-0441's overlay-absence gate does not depend on it.

## Problem

`teardown_handler` (`src/kdive/jobs/handlers/systems.py`) moves every System that is not
`tearing_down` or `torn_down` to `tearing_down` before provider cleanup. `failed` has no outbound
edge (`src/kdive/domain/capacity/state.py`), so a teardown job for a `failed` System raises
`IllegalTransition` on every attempt. The worker records `infrastructure_failure` and burns all
attempts in milliseconds. Nothing refuses the job earlier: `systems.teardown` short-circuits only
`torn_down`, and the orphaned-System and investigation force-close paths can enqueue a teardown
while provision is still running, which then fails and leaves the System `failed`.

The issue's unverified implication does not hold. A failed provider teardown never records
`failed`: `_record_system_failure` is called only from the provision, reprovision, and restore
paths, and `tearing_down` has the single successor `torn_down`. A provider fault during teardown
leaves the System `tearing_down`, and the stalled-teardown reconciler lane requeues it.

## Design

`teardown_handler` treats a terminal System (`TERMINAL_SYSTEM_STATES`: `torn_down`, `failed`) the
way it already treats a `torn_down` re-run:

1. Under the System lock, the external-boot fence is unchanged. The `tearing_down` move runs only
   when the System is neither `tearing_down` nor terminal.
2. Snapshot `delete_all`, `provisioner.teardown(domain_name)`, and core reclaim run as they do
   today. Provider teardown is idempotent over an absent domain, overlay, and baseline directory.
3. `_finalize_teardown` already returns without a write unless the System is `tearing_down`, so a
   `failed` System stays `failed`: no `torn_down` edge, no audit transition, no obligation
   discharge. Mutation obligations on a `failed` System stay with ADR-0652's reconciler lane.
4. The handler returns the System id, so the job ends `succeeded`.

A provider fault raises the provider's own `CategorizedError` and keeps the same category-based
retry classification it has for any other state; a retried attempt re-asks the provider, because
nothing in the path depends on the System leaving `failed`.

## Failure model

1. **Actors and deployments**
   - the job worker running `teardown_handler` for ordinary (non-authority) teardown jobs;
   - enqueuers: `systems.teardown`, allocation-orphan repair, investigation force-close,
     breakglass; local-libvirt, remote-libvirt, and fault-inject providers.
2. **Invariants and assets at stake**
   - a `failed` System never becomes `tearing_down` or `torn_down` (state machine unchanged);
   - the provider is asked to reclaim the failed System's domain, overlay, and baseline directory;
   - a `failed` System's state never makes `teardown_handler` consume retries or report
     `infrastructure_failure`.
3. **Accepted failure classes**
   - console and sysrq artifacts of a `failed` System are reclaimed by an explicit teardown, as for
     any torn-down System; the failure reason stays on `systems.failure_category` and the job row.
   - the job reports `succeeded` while the System reads `failed`; `systems.get` already renders the
     failed envelope with its failing job, so the state is not hidden.
   - a failed System's teardown that exhausts its attempts on provider faults is not re-run: the
     `{id}:teardown` job is not recycled and no reconciler lane selects `failed` Systems for
     teardown. Re-running it is the excluded ADR-0441 reconciler work; reported as a follow-up.
     Teardown jobs that already dead-lettered on `failed` Systems before this change are in the
     same position.
4. **Covered elsewhere**
   - mutation-obligation discharge for `failed` Systems: ADR-0652's reconciler lane;
   - authority-owned System teardown: `execute_authority_system_job`, not this handler;
   - a restricting external-boot activation: the existing terminal `CONFLICT` fence;
   - a teardown that arrives while the System is `reprovisioning` still raises `IllegalTransition`
     (no `reprovisioning -> tearing_down` edge); an adjacent gap outside this charter, reported as a
     follow-up with no owner yet.

## Considered and rejected

- **Verified no-op for `failed`.** Skips the provider, so a domain or host file a failed
  provision left behind (ADR-0435 residuals) would never be reclaimed by teardown.
- **Widen `failed -> tearing_down`.** Changes a terminal state and ADR-0435/ADR-0441 premises, and
  would let a failed System reach `torn_down` past ADR-0652's obligation lane.

## Success

- A teardown job for a `failed` System returns its id, calls the provider's snapshot `delete_all`
  (when the provider supports snapshots) and `teardown` once per attempt, and leaves the System `failed` with no new audit transition.
- A provider fault on that path raises the provider's error with the System still `failed`, and a
  later attempt of the same job succeeds and reclaims.

## Validation

- `focused-test`: `tests/adversarial/test_provider_state_races.py` — a teardown queued behind a
  provision that fails reclaims the leftover domain, calls a recording snapshotter's `delete_all`,
  and leaves the System `failed`; red on main.
- `focused-test`: same file — a failed System whose provider teardown faults once raises the
  provider error with a retryable category, stays `failed`, and the retry succeeds and reclaims;
  red on main.
- `task-test-not-applicable`: the ADR-0435 amendment is documentation; `just lint` covers it.
