# Remote module restore crash window (#3427)

Decision record: [ADR-0620](../../adr/0620-authority-owned-system-teardown.md), amended by this
change. No new ADR, no schema or wire change.

## Problem

A remote module restore (`execute_lifecycle`, action `restore`) runs the host call and only then
publishes its evidence. The host call rewrites the attempt's scratch volume to the appliance's
RESTORED result; the evidence is two files the module store writes afterwards, the lifecycle
completion and the activation's `.restored` record. A process crash between those points strands
the System:

1. **Crash after the host call, before any write.** The scratch volume holds the RESTORED result
   and the store holds neither file. A System teardown reads no restored evidence, so `_reap`
   expects the INSTALLED result, finds the RESTORED one and raises
   `remote module restored result differs before reap`. The teardown can never complete.
2. **Crash between the two writes.** `publish_lifecycle_completion` writes the completion first
   and the `.restored` record second. A crash between them leaves a completion without restored
   evidence. A restore retry is answered from the completion by `observe_lifecycle` and never
   writes `.restored`, so the release CLEANUP reap is refused
   (`ordinary remote module cleanup requires restored evidence`) and a teardown fails as in 1.

## Facts verified in source (main 37e2db046)

- `DurableRemoteModuleVolumePreparationHost.execute_lifecycle`
  (`remote_libvirt/external_boot_authority.py`): stages the request, answers from an existing
  completion, answers a restore from existing `.restored` evidence (publishing its completion),
  otherwise runs the host and publishes the completion after it returns. `_wait_for_owned_child`
  waits for the host task through cancellation, so only a process death opens the window.
- `RemoteModuleVolumePreparationStore.publish_lifecycle_completion`: `_publish` of
  `<key>.lifecycle-completion`, then `_publish` of `<terminal key>.restored`. Each `_publish` is an
  atomic, fsynced, no-replace link that accepts identical bytes on a repeat.
- `ConcreteRemoteModuleAuthorityHost._reap` (`remote_module_authority_host.py`): with no restored
  evidence the expected result is the INSTALLED one. With reap state `absent` and a volume present
  it reads the scratch result and refuses any other value.
- A restore retry with nothing published re-runs `_restore`: the existing scratch volume is left
  alone (`_ensure_scratch_formatted` keeps a formatted volume) and the appliance's `restored`
  resume action rewrites `restored` (`deploy/remote_module_appliance/README.md`, phase table), so
  the re-run reaches the same RESTORED success. Case 1 strands only the teardown, not the retry.
- `_restore` accepts the appliance result only when `validate_for(restore)` holds, `status` is
  `success` and `phase` is `restored`. The restore operation is derived deterministically from the
  stored terminal preparation (`_restore_operation`), so `_reap` derives the same one.
- The System teardown discards the host's reap response (`execute_system_teardown` in the adapter
  awaits it for its side effects); only the release lifecycle path compares a reap response.

## Options

### Option A: recognise a finished restore in the scratch volume (chosen)

- **Teardown.** When `_reap` has no restored evidence and the scratch result is not the INSTALLED
  result, it accepts a scratch result that is the finished restore of this attempt: identity
  complete, `validate_for(restore)`, `status == "success"`, `phase == "restored"` — the same
  predicate `_restore` applies before it returns a restore. The reap then proceeds against that
  result and reports the restore operation. Anything else is still refused with the existing
  message.
- **Write order.** `publish_lifecycle_completion` writes `.restored` before the completion. A crash
  between them leaves restored evidence without a completion, which the existing branch in
  `execute_lifecycle` already turns into a completion on the next restore; the completion is then
  never present without `.restored`.
- **Restore retry.** Unchanged: it re-runs the idempotent host restore and publishes. A test pins
  this so a later change cannot make the retry depend on published evidence.

### Option B: write the evidence atomically with the scratch rewrite (rejected)

The scratch volume lives in the libvirt pool and the evidence in the authority's local store; no
single atomic write spans both. The host could write a store-side intent before the appliance and
reconcile it afterwards, but that is new persisted state and a new recovery path for every
lifecycle action, which the approved exclusions rule out ("evidence schema changes beyond what
recovery needs"). The scratch volume already carries self-describing, attempt-bound evidence that
`_restore` trusts; Option A reads it rather than duplicating it.

### Option C: have the restore retry short-circuit on a RESTORED scratch volume (rejected)

The appliance writes `restored` and then removes the installed backup directory, so a RESTORED
scratch result does not prove the appliance finished. Re-running the appliance finishes that step;
short-circuiting would skip it. The retry already works, so C adds risk for no gain.

## Design

1. `RemoteModuleVolumePreparationStore.publish_lifecycle_completion`: for a restore response,
   publish `<terminal key>.restored` first, then the lifecycle completion. Other responses are
   unchanged.
2. `ConcreteRemoteModuleAuthorityHost._reap`: in the `state == "absent"` scratch check, when
   `restored is None` and the scratch result differs from the INSTALLED result, accept it only when
   it is a finished restore of this attempt (predicate above) and continue with
   `operation = restore`, `result = <scratch result>`. When `restored` is present the check is
   unchanged (exact equality with the stored restored result).
3. ADR-0620 amendment recording both rules.

The ordinary CLEANUP reap still requires stored restored evidence at the durable layer; this change
does not relax that. A release that crashed in the window retries its RECOVER restore first, which
publishes the evidence (case 1 re-runs the host; case 2 publishes from `.restored`).

## Failure model

- **Crash after the host restore, before either write.** Teardown: reaps against the scratch
  volume's RESTORED result. Restore retry: re-runs the host restore, which the appliance resumes
  to the same RESTORED success, and publishes both files.
- **Crash after `.restored`, before the completion.** Restore retry: answered from `.restored`
  without calling the host, and publishes the completion. Teardown: reaps against `.restored`.
  Observation reports the modules restored, which matches the scratch volume.
- **Crash after both writes.** Unchanged.
- **Crash during the reap itself.** Unchanged: the `reaping`/`reaped` markers make the reap
  resumable without re-reading the scratch result.
- **Scratch result is a failed or partial restore** (`restore-ready`, failure status) **or belongs
  to another attempt.** Still refused with `remote module restored result differs before reap`;
  recovering a failed restore is another crash window and out of scope.
- **Existing stores.** A store already holding a completion without `.restored` (written by the
  old order) is not repaired by the new order; a teardown of that System is covered by the reap
  rule, and its CLEANUP still needs a fresh restore. Pre-release surface; no migration.

## Tests

In `tests/providers/remote_libvirt/test_remote_module_authority_host.py`, over the `_Quiesce`
stack (real durable store and host, fakes at the libvirt edge):

1. Crash after the host restore: stage the restore request, run the host restore directly
   (nothing published), then a System teardown reaps with `result.phase == "restored"`.
2. Same crash, then a restore retry returns the RESTORED response and publishes `.restored`, and a
   release CLEANUP reap succeeds.
3. Crash between the two writes: fail the second evidence write once, then a restore retry and a
   release CLEANUP reap succeed. This test is order-agnostic, so it is red under the old order.
4. A scratch result that is a failed restore or a `restore-ready` phase is still refused by the
   teardown.

Controlled faults: revert the `_reap` rule (test 1 red) and revert the write order (test 3 red),
then restore. Test 2 passes before the change too; it pins the retry behaviour the design relies
on.
