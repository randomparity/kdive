# Authority recovery fault construction for live proof (#3185) — design

Design proposal for issue #3185 (parent #3152, epic #2803), on source baseline `b2d93172e`.

- Frozen scope: [WORK:SCOPE](https://github.com/randomparity/kdive/issues/3185#issuecomment-6280583516),
  token `q3185-93a16704`. The operator approved the scope and exclusions on 2026-10-16.
- The operator approved this design on 2026-10-19, including the proof-control change in
  [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md). The approval covers Arm B's
  scope as stated below, the blockers below as narrower blockers, and a live plan to be decided
  after PR CI is green.
- The implementation plan is transient and not committed.

## Problem

The installed-authority lanes can frame an authority-owned System (#3184, #3209) but cannot put
it into the states the recovery tools act on. Source at `b2d93172e` shows three cases.

### Held protected effect (`ops.force_release`)

Reachable with the existing ADR-0622 barrier:

- a System with external-boot history tears down through the authority teardown operation;
- `service.py` calls `before-provider` and `after-provider` around that operation;
- until the System is `torn_down`, `_require_system_teardown` (`services/allocation/release.py`)
  refuses release with `external_boot_system_teardown_required`.

### Quarantined recovery orphan (`ops.resolve_recovery_orphan`)

- The only writer of `external_boot_recovery_quarantine` is the authority. It writes a row when
  cleanup finalization fails *after* the terminal journal record (`_publish_cleanup_quarantine`,
  migration 0144).
- A restart at `after-provider` heals through `_finish_recovery` and publishes nothing.
- **Local only.** The remote adapter has no finalizer and no quarantine inventory, and no remote
  `external_boot_recovery_objects` port is composed. For a remote System the tool answers
  `recovery_provider_not_configured`.

### Recovery conflict (`systems.resolve_external_boot_conflict`)

No production path enters `recovery_conflict`:

- `ExternalBootActivationRepository.record_conflict` has no caller in `src/`;
- no SQL function writes the state;
- an authority observation classed as `mixed`, `unreadable` or `conflict` instead fails the job
  with `configuration_error`.

### What the issue requires

The issue requires real construction or a narrower recorded blocker. It forbids synthetic
database writes, journal deletion, and expanded proof-control without a reviewed design.

## Scope

In scope:

1. Fault helpers in `tests/integration/live_stack/authority_faults.py`, for the frame's local and
   remote Systems. #3186 consumes them; this change does not rebind the 32 HTTP cells.
2. Frame support:
   - the lane config carries the fixed barrier socket;
   - cleanup accepts a System that the body already tore down and an allocation it already
     released.
3. ADR-0787 barrier change, conditional on approval.
4. A gated live proof module and runbook text.

Out of scope, with owners:

- the WORK:SCOPE exclusions;
- HTTP cell rebinding (#3186);
- the two product gaps under Blockers. They are routed through the campaign root and not filed
  here.

There is no production API, schema, migration, provider-family or deploy-role change.

## Design

### Barrier client and `held`

`Barrier` wraps the existing root-only request functions:

- local: `installed_local_authority_support.fault_barrier_request`;
- remote: `installed_remote_authority_support.fault_barrier_request`.

It offers `arm`, `status`, `wait_reached(timeout_s)`, `release` and `abort`.

- `arm` answering `busy` fails loud and names the runbook's stale-arm recovery.
- `held(barrier, run_id, operation, checkpoint)` is an async context manager. It arms on entry.
  On every exit (success, assertion failure, `KeyboardInterrupt`, cancellation) it releases
  when `status` is `reached` or `armed`. It then requires `status == "idle"` and records that
  status in the evidence.
- Under ADR-0787, a release of a never-reached arm clears it, so no ordinary failure leaves a
  stale arm. Only a SIGKILLed run does. Recovery is then one operator-run authority restart,
  counted in the unit's 5-starts/600 s budget (#3272).

### Hold budget

Every held request belongs to one worker attempt. That attempt's authority client has one
absolute deadline, `recovery_readiness_timeout` (5 min, `jobs/handlers/external_boot/ports.py`),
and publication needs the job's live lease. So the helpers enforce three things:

- `wait_reached(120)` in both arms;
- before `abort` or `force_release`, the job reads `running` at the attempt observed at reach,
  with `lease_expires_at` in the future. Otherwise the arm fails as `hold budget exceeded`
  instead of reporting a constructed state;
- the evidence records seconds from admission to release or abort.

### Activation history

The body creates external-boot history through public tools, using the existing
`start_external_boot_activation`. Its `config` parameter is replaced by explicit `project`,
`system_id`, `label` and `require_kdump`:

- the local frame passes `True`, because its fixture profile carries a crashkernel;
- the remote frame passes `False`.

The upload is `build_and_upload_kernel` without `vmlinux`, so #3130/#3131 stay excluded. It uses
the lane's pre-built tree (`KDIVE_KERNEL_SRC`); nothing is built.

A remote activation failure stops the arm `blocked`, naming the job's category and reason. It is
reported as a finding. Remote external-boot activation on an authority System has no prior live
proof.

### Arm A — held teardown effect for `ops.force_release` (local and remote; existing barrier)

1. Activate. Release the external boot and drain it. Require `assert_root_release_completion`.
2. Under `held(run_id, "teardown", "after-provider")`, call `systems.teardown` and wait for the
   barrier to report reached.

   The teardown request's `run_id` is the activation's (`lifecycle.py` `teardown_handler`).
3. Evidence while held:
   - the teardown job is `running`;
   - the System is not `torn_down`;
   - the journal head is `mutation-started` for the teardown operation;
   - the domain is absent on the authority daemon;
   - the allocation is `active` (`allocations.wait`, `timeout_s=0`);
   - capacity in use is unchanged.
4. Check the budget, then call `ops.force_release` through the supplied caller. It must return
   `error`/`conflict`/`external_boot_system_teardown_required` naming this System. The allocation
   stays `active`.
5. Leave `held`. Drain the teardown to `succeeded`, wait for `torn_down`, and require the head to
   be `terminal` for teardown.
6. Call `ops.force_release` again. It must release. This observes the obligation's terminal
   release, not only its refusal.

The frame cleanup then skips teardown and release, and proves the authority inventory and
capacity unchanged.

### Arm B — quarantined recovery orphan (local; requires ADR-0787)

1. Activate. Under `held(run_id, "cleanup", "before-finalize")`, call
   `runs.release_external_boot` and wait for the barrier to report reached.
2. Pre-abort evidence:
   - the head is `terminal` for the release's cleanup operation;
   - a fixed, read-only observer reports (run as root through the same `sudo -n` seam the proof barrier already requires; the ADR-0800 inventory collector returns fixed facts only, and extending it is outside this change)
     `<recovery_root>/<system_id>.<activation_id>/cleanup-quarantine.json` present, with its
     SHA-256. It takes UUID arguments only;
   - no quarantine row exists for the System.
3. Check the budget, then `abort()`. `_provider_error` maps the abort to `provider_conflict`, and
   the authority publishes through the real fenced function.
4. Wait, with a 60 s deadline, for exactly one new row:
   - this System and activation, `recovery-record`, `quarantined`;
   - an operation identity and journal digest that match the observed receipt;
   - every other row equal to the pre-snapshot.
5. Drain the release. ADR-0714 retries one `provider-conflict`. Require:
   - the job `succeeded` at attempt 2;
   - attempt 1's audit `failure_context.authority_reason` is `provider-conflict`;
   - the receipt is absent;
   - the row is still `quarantined`.
6. Call `ops.resolve_recovery_orphan(system_id, [object_identity], "delete")` through the
   supplied caller, and drain it to `succeeded`. Require:
   - the row is `deleted`, with `resolved_at` set and `disposition_job_id` equal to the job;
   - the request's `object_ids` is `[row.id]`, with `completed_at` set;
   - every other row is unchanged.

**Failure cleanup.** If anything fails after step 3, the arm calls `ops.resolve_recovery_orphan`
(`delete`) for every row it published, before the frame tears down. This happens while the
allocation is still active, which resolver admission requires. A row that cannot be resolved is
recorded in the evidence as owned residue, and the arm fails.

**What Arm B proves.** It proves authority publication of a real quarantine, and an exact-set
disposition committed after the authority re-observes the object as absent. In that disposition
`orphan.py` skips the provider delete, because the release retry already removed the receipt.
The delete of a *present* receipt stays unproven, and is recorded as `LOCAL_PRESENT_ORPHAN_BLOCKED`.

### Blockers

These are narrower, recorded reasons; the obligations are kept. `authority_faults.py` exports
each reason, and #3186 reuses them.

- `REMOTE_ORPHAN_BLOCKED`: "the remote-libvirt authority publishes no cleanup quarantine: no
  finalizer, quarantine inventory or remote recovery-object port".
- `CONFLICT_BLOCKED`: "no production path enters recovery_conflict: record_conflict has no
  caller and an authority conflict observation fails the job".
- `LOCAL_PRESENT_ORPHAN_BLOCKED`: "a quarantined receipt survives to resolution only if the
  release job ends terminally (ADR-0714), whose recovery is #3143".

Each blocked arm runs the frame's revision and lane gates, then stops before mutation.

## Failure model

1. **Actors and deployments**
   - The operator runs the gated live module on the installed x86_64 local and remote authority
     lanes, under ADR-0715 identity.
   - CI runs the unit tests.
   - Root on the lane host is the only barrier client.
2. **Invariants and assets**
   - Unrelated Systems, domains, private storage and quarantine rows stay unchanged.
   - Capacity returns to baseline.
   - The plan spends none of the authority's start budget.
   - The barrier is disabled by default.
   - Evidence carries no host paths or names (ADR-0715).
3. **Accepted failure classes**
   - SIGKILL leaves one arm. That costs one documented restart. Under ADR-0787, the shutdown does
     not publish a row.
   - If release attempt 2 does not succeed, ADR-0714 can end the job `failed`. The arm then
     records its observed state, and failure cleanup tries the exact resolve. Any later recovery
     of that release job belongs to #3143, and this design promises none.
   - A remote activation failure is recorded as a blocker and a finding.
4. **Covered elsewhere**
   - Remote quarantine publication and conflict entry: product gaps routed by the campaign root.
   - HTTP cells: #3186.
   - Exhausted-job recovery: #3143.
   - vmlinux: #3130, #3131, #3135.
   - POWER: #2818.

**Threat model.** ADR-0787 widens the root-only proof socket by one checkpoint and one action.
The only actor is root on a disposable proof host. Workers and tenants cannot reach the socket
(ADR-0622 controls). The new controls are:

- `before-finalize` is accepted only for operation `cleanup`;
- `abort` is accepted only at a reached `before-finalize`, and it consumes the arm;
- publication still passes the authority's head, lease and fence checks.

The receipt observer also crosses into the authority's private store, as root through the lane's
existing `sudo -n` seam. It takes UUID arguments only, opens with `O_NOFOLLOW`, bounds its read and
output, and returns only presence, a digest and the two proof identifiers.

A malicious root is out of scope.

## Success

Each item below is bounded to the arms named in it:

- Arm A passes on both lanes, or the remote arm stops `blocked` with an observed activation
  reason.
- Arm B passes on the local lane if ADR-0787 is approved.
- The remote orphan arm and both conflict arms stop `blocked` with their named reasons.
- Each arm's cleanup leaves its lane's authority inventory and capacity at baseline.
- No authority restart is performed by these arms.

## Validation

Unit tests:

- **Barrier.** `abort` succeeds only at a reached `cleanup` `before-finalize`. Releasing an
  unreached arm clears it. A shutdown wait at `before-finalize` publishes nothing.
- **Service.** An abort at `before-finalize` publishes once and raises `provider_conflict`. A
  replay of the same operation finalizes once and does not publish again.
- **Helpers (fakes).** `held` releases on success, failure and interrupt. `busy` fails loud. The
  budget check fails late holds. A wrong refusal reason fails. An extra or changed unrelated
  quarantine row fails. Failure cleanup resolves exactly the published rows.
- **Frame.** A body-completed teardown and release is cleaned up correctly.

Live: the operator-approved run described in the design-gate hand-off. Guardrails: `just lint`,
`just type`, and the focused pytest above.
