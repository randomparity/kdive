# Authority recovery fault construction for live proof (#3185) — design

Design proposal for issue #3185 (parent #3152, epic #2803); source baseline
`b2d93172e`. Frozen scope: [WORK:SCOPE](https://github.com/randomparity/kdive/issues/3185#issuecomment-6280583516),
token `q3185-93a16704`. The operator approved the scope and exclusions on 2026-10-16. This design,
and in particular the proof-control expansion in [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md),
needs a separate operator decision before implementation.

## Problem

The installed-authority lanes can frame an authority-owned System (#3184, #3209) but cannot put
it into the states the recovery tools act on. Source on `b2d93172e` shows what is reachable:

- **Held protected effect (`ops.force_release`).** Reachable with the existing ADR-0622 barrier.
  A System with external-boot history tears down through the authority teardown operation, and
  `service.py` calls the `before-provider`/`after-provider` checkpoints around it. While that
  teardown is unfinished, `_require_system_teardown` (`services/allocation/release.py`) refuses
  release with `external_boot_system_teardown_required`.
- **Quarantined recovery orphan (`ops.resolve_recovery_orphan`).** Only one path writes an
  `external_boot_recovery_quarantine` row, and it is the authority's own: cleanup finalization
  fails *after* the terminal journal record, so `_publish_cleanup_quarantine` re-reads the private
  receipt and calls the head-fenced SQL function (`service.py`, migration 0144).
  - The provider effect of a restart at `after-provider` heals through `_finish_recovery`.
    Nothing is quarantined, so no existing barrier position reaches it.
  - **Local only.** The remote adapter implements no finalizer and no quarantine inventory, and
    no remote `external_boot_recovery_objects` port is composed. For a remote System the tool
    answers `recovery_provider_not_configured`.
- **Recovery conflict (`systems.resolve_external_boot_conflict`).** No production path enters
  `recovery_conflict`, local or remote.
  - `ExternalBootActivationRepository.record_conflict` has no caller in `src/`, and no SQL
    function writes the state.
  - An authority observation classified `mixed`, `unreadable` or `conflict` ends the job
    instead: the worker refuses with `configuration_error`.

The issue requires real construction or a narrower recorded blocker. It forbids synthetic
database writes, journal deletion and expanded proof-control commands without a reviewed design.

## Scope

In scope:

1. Fault helpers beside `tests/integration/live_stack/authority_lane.py`, for the frame's local
   and remote Systems. #3186 consumes them; it rebinds the 32 HTTP cells, which this change does
   not touch.
2. Frame support:
   - the lane config carries the fixed barrier socket;
   - cleanup accepts a System the body already tore down and an allocation it already released.
3. Proof-barrier expansion (conditional on operator approval, ADR-0787):
   - a `before-finalize` checkpoint;
   - an `abort` action, valid only at that checkpoint.
4. A gated live proof module, plus the runbook text.

Out of scope, with owners: everything in WORK:SCOPE exclusions; HTTP cell rebinding (#3186); the
product gaps named under Blockers. Neither gap is filed by this design; they are routed through
the campaign root. No production API, schema, migration, provider family or deploy-role change.

## Design

### Barrier client (`authority_faults.py`)

`Barrier` wraps the existing root-only request functions:

- local: `installed_local_authority_support.fault_barrier_request`;
- remote: `installed_remote_authority_support.fault_barrier_request`, over the operator's
  bounded SSH seam.

It exposes `arm(run_id, operation, checkpoint)`, `wait_reached(timeout_s)`, `release()` and
`abort()`.

- `arm` answering `busy` fails loud. The failure names the stale-arm recovery in the runbook and
  never restarts the authority by itself.
- `wait_reached` polls `status` with a finite deadline, as `wait_for_fault_barrier` does.
- `held(barrier, run_id, operation, checkpoint)` is an async context manager. It arms on entry.
  On every exit (success, assertion failure, `KeyboardInterrupt` or cancellation), it does this:
  - if `status` is `reached` or `armed`, it sends `release`. A reached wait proceeds normally. A
    never-reached arm is pre-released, so the body's own release or the frame's teardown passes
    it.
  - it then records the final status in the cell evidence. Releasing performs no fault.

A run killed with SIGKILL leaves the arm in place. The next `arm` answers `busy`. The runbook's
recovery is one operator-run authority restart, which counts against the unit's 5-starts/600 s
limit (#3272).

### Activation history

The body creates external-boot history through public tools, using the existing
`start_external_boot_activation`. Its `config` parameter becomes the explicit `project`,
`system_id` and `label` that it reads today, so a remote frame System can use it. The upload is
`build_and_upload_kernel` without `vmlinux` (#3130/#3131 stay excluded). It needs a pre-built
tree; there is no kernel build. If the frame's profile carries no crashkernel, the
`require_kdump` gate stays off.

### Arm A — held teardown effect for `ops.force_release` (local and remote; existing barrier)

1. Activate. Release the external boot and drain it. Require the existing root-release
   completion evidence (`assert_root_release_completion`).
2. Under `held(run_id, "teardown", "after-provider")`: call `systems.teardown` as the frame admin,
   then `wait_reached(120)`.
3. Evidence while held:
   - the teardown job is `running`;
   - the System is not `torn_down`;
   - the authority journal head for the System is `mutation-started` for the teardown
     operation identity;
   - the domain is absent on the authority daemon, because the effect ran but has not been
     journaled;
   - the allocation is `active`, and capacity in use is unchanged.
4. Call `ops.force_release` through the supplied caller. It must return status `error`, category
   `conflict`, reason `external_boot_system_teardown_required`, naming this System. The
   allocation stays `active`.
5. Leave `held` (release). Drain the teardown job to `succeeded` and wait for `torn_down`. The
   journal head must be `terminal` for the teardown operation.
6. Call `ops.force_release` again. It must answer released. The allocation is `released`, and
   capacity is back to the frame's baseline.

The frame cleanup then skips teardown and release and proves the authority inventory is
unchanged.

### Arm B — quarantined recovery orphan (local; requires ADR-0787)

1. Activate (no release yet). Under `held(run_id, "cleanup", "before-finalize")`, call
   `runs.release_external_boot`, then `wait_reached(300)`.
2. Pre-abort evidence:
   - the journal head is `terminal` for the root release's cleanup operation identity;
   - a fixed read-only observer, run as the authority account, reports `cleanup-quarantine.json`
     present under `<recovery_root>/<system_id>.<activation_id>/`, with its SHA-256;
   - no quarantine row exists for the System.
3. `abort()`. The finalize raises `ProofBarrierAbort`, `_provider_error` maps it to
   `provider_conflict`, and the authority publishes the quarantine through the real fenced
   function. The arm is consumed.
4. Wait, with a finite deadline, for exactly one row for the System:
   - `status` is `quarantined`, `object_kind` is `recovery-record`, `provider_kind` is
     `local-libvirt`, and `activation_id` matches;
   - its `operation_identity` and journal digest equal the receipt proof that was observed.
5. Drain the release job. ADR-0714 retries one `provider-conflict`. The second attempt's finalize
   passes the consumed arm and deletes the receipt.
   - Require the job `succeeded` at attempt 2.
   - Require the first attempt's audit failure context to carry
     `authority_reason: provider-conflict`.
   - Require the receipt to be absent and the row still `quarantined`.
6. Snapshot every quarantine row and the recovery-root listing. Call
   `ops.resolve_recovery_orphan(system_id, [object_identity], "delete")` through the supplied
   caller, and drain its job to `succeeded`. Then require:
   - the row is `deleted`, with `resolved_at` set and `disposition_job_id` equal to the job;
   - the request row's `object_ids` equals `[row.id]`, and `completed_at` is set;
   - every other quarantine row is byte-identical;
   - the recovery-root listing is unchanged apart from this System's absent directory.

The provider object is removed by the release retry, not by the resolver. The resolver's proof
is two things: the authority re-observes the object as absent, and it commits a disposition for
exactly that set (`orphan.py` skips the mutation when `delete` finds the object absent). A
variant that keeps the receipt until the resolver runs must end the release job terminally
(ADR-0714). Recovering such a release depends on the excluded #3143 recovery path, so this
design does not take it.

### Blockers (narrower, recorded, obligations kept)

`authority_faults.py` exports the two reason constants; #3186 uses them:

- `REMOTE_ORPHAN_BLOCKED`: "the remote-libvirt authority publishes no cleanup quarantine: its
  adapter has no finalizer or quarantine inventory and no remote recovery-object port is
  composed".
- `CONFLICT_BLOCKED`: "no production path enters recovery_conflict:
  ExternalBootActivationRepository.record_conflict has no caller and an authority conflict
  observation fails the job".

Each blocked arm runs the frame's revision and lane gates, then stops `blocked` before any
mutation. If Arm B is not approved, the local orphan arm stops with `LOCAL_ORPHAN_BLOCKED`
instead ("no proof-control position fails cleanup finalization after the terminal record").

### ADR-0787 barrier change

`ProofCheckpoint` gains `before-finalize`. `_finalize_adapter` awaits it inside its existing
`try`, immediately before `adapter.finalize` and after the teardown and non-finalizer early
returns. So it fires only where the authority would publish a quarantine on failure.

`abort` is accepted only when the single arm is at `before-finalize` and `reached`, and answers
`aborted`. In every other state it answers `{"error": "invalid-request"}`. It fails that one wait
with `ProofBarrierAbort` and clears the arm. `checkpoint()` clears the arm in a `finally`, so an
aborted or shut-down wait never leaves a stale arm. Everything else in ADR-0622 is unchanged:
root-only, one arm, closed JSON, and no provider, path or command selection.

## Failure model

1. **Actors and deployments**
   - The operator runs the gated live module on the installed x86_64 local and remote authority
     lanes (ADR-0715 identity).
   - CI runs the ordinary unit tests.
   - Root on the lane host is the only barrier client.
2. **Invariants and assets at stake**
   - Unrelated Systems, domains, private storage and quarantine rows stay unchanged.
   - Capacity returns to baseline.
   - The authority's 5-starts/600 s budget is not spent by the plan.
   - A disabled-by-default barrier never fires in production.
   - Evidence never carries host paths or names (ADR-0715).
3. **Accepted failure classes**
   - SIGKILL of the test leaves one arm. It is bounded: the next arm fails loud and a documented
     single restart recovers it.
   - Live behaviour that differs from this design (for example, the release retry not succeeding
     at attempt 2) fails the arm with its observed state, followed by cleanup. That is an honest
     result, not a workaround.
4. **Covered elsewhere**
   - Remote quarantine publication and conflict entry: product gaps routed by the campaign root.
   - HTTP cells: #3186.
   - Exhausted-job recovery: #3143.
   - vmlinux: #3130, #3131, #3135.
   - POWER: #2818.

### Threat model

- **Boundary widened:** the root-only proof socket. It gains one checkpoint and one action that
  can fail one cleanup finalization the operator selected by exact System, Run and operation.
- **Actor:** root on a disposable proof host. Workers and tenants cannot reach the socket (mode
  `0600`, `SO_PEERCRED` uid 0, absent unless `live_vm_host_authority_fault_proof_enabled`).
- **Controls:**
  - closed-key JSON within 1024 bytes;
  - `abort` only at a reached `before-finalize`;
  - the arm is consumed by abort;
  - publication still passes the authority's head, lease and fence checks;
  - no new path, provider or credential input.
- **Out of scope:** a malicious root, who already controls the host.

## Success

- Arm A passes on both lanes.
- Arm B passes on the local lane, if approved.
- The remote orphan arm and both conflict arms stop `blocked` with the named reasons.
- Every arm's cleanup leaves the authority inventory equal to its baseline.
- No authority restart is performed.

## Validation

- `test_proof_barrier.py` covers `abort` semantics: only at a reached `before-finalize`; it fails
  the wait; it consumes the arm; an aborted wait leaves `status` idle; it is refused at other
  checkpoints.
- `test_service.py` covers the barrier position: an abort at `before-finalize` publishes the
  quarantine and raises `provider_conflict`.
- Helper tests use a fake barrier and fake callers:
  - `held` releases on success, on failure and on `KeyboardInterrupt`;
  - `busy` fails loud;
  - each arm's assertions fail on a wrong reason, an extra quarantine row, or a changed unrelated
    row.
- Frame tests cover cleanup of an already torn-down and released System.
- Live: the plan below; `just lint`, `just type` and focused pytest.
