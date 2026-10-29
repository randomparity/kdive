# Remote resolve-conflict terminal state and re-entered reap (#3414)

Decision records: [ADR-0805](../../adr/0805-authority-observed-conflict-is-recorded-from-its-journal-head.md)
and [ADR-0620](../../adr/0620-authority-owned-system-teardown.md), each amended by the chosen
option. Status: DESIGN ONLY. Implementation waits for the operator's choice in
[Decision needed](#decision-needed).

## Problem

Two defects on one path, both reached by `resolve-conflict` on the remote authority after #3412.

1. **Terminal observation mismatch.** The service sends `resolve-conflict` through
   `commit_cleanup` (`_CLEANUP_EVIDENCE_COMMITS`, `external_boot_authority/service.py`). The
   remote `commit_cleanup` runs `coordinator.recover(point, authority, acknowledged)` and then
   `coordinator.cleanup(point)`, which deletes the boot kernel/initrd volumes. The adapter's
   `_categorize` then reads `definition == "source"` with `recovery_objects_absent` and answers
   `absent`. `resolve_conflict_handler` passes `expected_observation="source"` and
   `_require_category` refuses `absent`, so the terminal commit is dropped. Before #3412 the
   resolve failed earlier, at the module-volume check, so the mismatch was masked.
2. **Re-entered reap after the sweep.** #3412 made the worker run `restore` then `reap` for
   `RESOLVE_CONFLICT`. The first reap discharges the reap obligation. The module-volume sweep
   (`jobs/handlers/module_volume_reaping.py`) keeps volumes only for attempts in `retained_owners`: the
   `source.ext4`/`scratch.ext4` pair while the mutation obligation is undischarged and the
   `reaping`/`reaped` journal volumes while the reap obligation is open and undischarged. So it
   deletes the markers of a reap-discharged attempt, and would also delete the restore volumes of
   an attempt whose mutation obligation is discharged. A re-claimed job repeats the
   reap: `_reap_state` reads `absent`, `_reap` re-reads the already-deleted scratch result and
   raises `remote module restored result differs before reap`.

## Facts verified in source (main df9a14506)

- `_categorize` (`remote_libvirt/external_boot_authority.py`): `source` + objects absent -> `absent`.
- `resolve_conflict_handler` `expected_observation="source"`; `_require_category` refuses others.
- The handler's `build` emits `terminal_evidence(..., "recovered")`: a resolved conflict leaves the
  activation `recovered`, exactly as an ordinary `recover` does.
- Ordinary remote `RECOVER` goes through `commit` -> `coordinator.recover` only (restore the
  definition); it never deletes recovery objects and observes `source`. Its objects are removed
  later by the release's `cleanup` phase.
- The release handler admits `RECOVERED`; with the activation already `recovered`,
  `_run_active_release` skips its recover phase and runs only `cleanup`, which expects `absent`
  and reaps the module volumes through the retained restored evidence.
- Local authority (`local_libvirt/external_boot_authority.py`): `RESOLVE_CONFLICT` runs
  `ports.recover(..., acknowledged=...)` only. It is not in `_DELETING_OPERATIONS`, so objects are
  retained and the observation is `source`. Local resolve is terminal `source`, not `absent`.
- The only reason the remote resolve rides `commit_cleanup` is that it is the one remote path that
  forwards the acknowledged third definition (`_acknowledged_definition`). The cleanup that follows
  is a side effect of that routing, not a decision: ADR-0805 amendment (#3330) item 3 says the
  remote resolve "restores ... then ends with the same cleanup that `recover` runs" and says
  nothing about deleting recovery objects. ADR-0620's #3412 amendment records the reap but
  states the cleanup refusal as a given.
- `retry_after_discharge` (worker) already tolerates a reap whose obligation discharged, provided
  the host answers `volumes_absent`. The host cannot answer once the sweep took its markers (defect 2).
- `commit_worker_remote_module_evidence('discharge-reap')` is idempotent; the worker needs no change
  for a repeated discharge.

## Options for the terminal state

### Option A: `source` (recommended)

Resolve-conflict stops being a cleanup-evidence commit. It is a recovery commit like `recover`:
restore the module half, then recover the definition from the acknowledged identity, leave the
recovery objects, observe `source`.

Consequences:

- **Release/teardown.** The activation is `recovered`. Release skips its recover phase and runs
  `cleanup`, which deletes boot artifacts, reaps module volumes once through the restored evidence
  and records the cleanup receipt: identical to the path after an ordinary `recover`. Teardown is
  admitted by the reap obligation as today (an open obligation carries its receipt; ADR-0620's
  #3406 amendment covers the discharged case). No new requirement on later operations.
- **Reap obligations.** The resolve no longer reaps, so the obligation stays open until the release
  or teardown. There is exactly one reap per attempt, so no second reap follows a discharge on this
  path. The mutation obligation stays undischarged through the resolve window (it is discharged by
  system reclaim, the reconciler's job repair and the terminal-escape path, none of which run on a
  `recovered` activation); the sweep therefore keeps the restore volumes. If a repair did discharge
  it first, the release's first reap would meet no scratch result, which the host rule below also
  covers, so the host rule is a hardening under A, not a dependency.
- **Restored evidence.** The release's reap compares against the restore evidence the resolve
  recorded (`worker_record_restored_evidence` refuses a differing re-record). No earlier restore
  exists for this attempt, because the conflicting release ended before its own recover restored.
- **#3412.** Its worker `reap` step for `RESOLVE_CONFLICT`, the validator entry, the host
  `active_ok` entry, `"resolve-conflict"` in `_RESTORED_REAP_OPERATIONS` with its comment, and the
  `lifecycle.py` step-table comment become dead and are removed, as is the now-false comment on
  `_CLEANUP_EVIDENCE_COMMITS`. The restore step stays. The SQL admits for the
  `('resolve-conflict','resolve-conflict')` cleanup-evidence pair (migrations 0145 and 0182) become
  unreachable and are left in place: they are permissive, and editing a shipped migration is out of
  scope (a follow-up may drop them).
- **ADR text.** ADR-0805 amendment (#3330) item 3 is clarified: remote resolve restores then recovers
  from the acknowledged definition and retains recovery objects, as local does. ADR-0620's #3412
  amendment is superseded for the reap step (an amendment note, not a rewrite, per the project's
  amend-in-place convention). No new ADR: this is the interpretation ADR-0583/0805 already state
  ("the same ordinary recovery"), and it removes behavior #3412 added the day it merged.
- **Local-vs-remote parity.** Restored: both return `source` and retain objects.
- **#3388 test.** The expectation `terminal`/`resolve-conflict`/`source`/`source` is correct and
  stays unchanged. The test needs a follow-up assertion that the System can then be released
  (activation `recovered` -> release `cleanup` -> `absent`).

### Option B: `absent`

Keep the cleanup-evidence commit and change the handler to expect `absent`.

Consequences:

- The resolve deletes boot artifacts and reaps and discharges module volumes. `recovered` with no
  recovery objects. A later release finds nothing to delete and its `cleanup` re-enters the
  discharged-retry reap (`retry_after_discharge`) a second time, now almost always after the sweep:
  defect 2 would hit the release instead. B therefore *requires* the idempotent host fix and makes
  every resolved conflict depend on it.
- The terminal evidence says `recovered` but the activation has no recovery point for the audit trail
  that `recovered` implies; ADR-0805's "resolution is ordinary recovery" reads false.
- Local and remote diverge (local retains, remote deletes).
- ADR-0805 item 3 and ADR-0620 need real rewrites; the #3388 test expectation changes to
  `absent`/`absent` (and to remote-only, since local is `source`), so the single parametrized
  `[local]`/`[remote]` assertion splits.
- The `recover` handler (also `expected_observation="source"`) and the resolve handler would then
  expect different categories for the same recovery work.

## Decision needed

Operator picks A or B. Recommendation: **A**. It removes a cleanup that was never decided, restores
local/remote parity, makes defect 1 disappear instead of being re-labelled, and means a resolve
reaps nothing so it cannot hit defect 2. B keeps more of #3412 but turns every resolved conflict
into a double reap.

## Re-entered reap becomes idempotent (both options, mandatory under B)

Cause fix in the host, not a worker special case. In `RemoteModuleAuthorityHost._reap`, when
`_reap_state` is `absent` (no markers) *and* both attempt volumes are absent, the reap's
post-condition already holds. The host returns the `volumes_absent=True` response built from the
request's retained restored `result` and writes no marker. The scratch-result re-read exists to prove
the restore result before volumes are deleted; with no volume present there is nothing to delete or
to prove. The same holds for TEARDOWN with no restored evidence (an installed-only baseline never
created the restore volumes): a previously raising case becomes an idempotent success, an ADR-0620
teardown behavior the amendment must state. `reaping` + absent volumes already completes through the
existing path. Unchanged: `reaped` + present volumes still raises; `reaping`/`absent` + present volumes
still verifies the scratch result; present volumes with a mismatching result still raises.

The worker keeps its guards: `retry_after_discharge` requires `volumes_absent`, and the response is
compared with the retained restored evidence before the idempotent discharge.

Under A this fix is not needed for the resolve itself. It is included because the same sweep
window breaks any CLEANUP or TEARDOWN retry after discharge (same code, same cause), and the issue's
Expected section requires an idempotent re-entry after the sweep. If the operator would rather not
widen the change, drop it from A and file it; B cannot drop it.

Rejected: telling the host the obligation was discharged (adds a wire field for what the host can
read from storage); trusting only the worker's discharge flag (leaves the host raising); recreating
markers on re-entry (the sweep removes them again and leaks journal volumes).

## Migration, interface, ADR

- Migration: none under either option (no schema, no SQL function change).
- Wire: none. `RemoteModuleLifecycleRequestV1` only loses its `RESOLVE_CONFLICT` reap entry under A.
  Deploy the authority first or together with the worker. An old authority still routes the resolve
  through `commit_cleanup`, which refuses while the restored volumes stand, so a new worker against
  an old authority fails closed; an old worker against a new authority sends a reap the narrowed
  validator refuses, also failing closed. Both are retryable and mutate nothing.
- Adapter interface: under A, `RemoteLibvirtAuthorityAdapter.commit` admits `RESOLVE_CONFLICT`
  (forwarding `_acknowledged_definition` to `coordinator.recover`), and the service drops the
  `resolve-conflict` row from `_CLEANUP_EVIDENCE_COMMITS`. `AuthorityCleanupAdapter` is unchanged.
- ADR: amendments only (Option A: ADR-0805 item 3 clarification, ADR-0620 #3412 supersession note).
  No new ADR.

## Failure model (Option A)

- Restore fails: the resolve job fails before any authority mutation; the conflict stands.
- Authority commit fails after the restore: retry repeats the host's retained restore harmlessly.
- Authority commit succeeded but the worker's terminal commit did not: ADR-0805 item 6 governs.
  After the definition write the composite differs, a retried commit answers `superseded`, the next
  observation classifies a part-restored state as a conflict and the activation parks again for a
  fresh resolve. This is the documented limit of the local lane and now applies to remote too; no
  durable per-component evidence is added here. The implementation must check the remote
  `superseded` path ends the job rather than leaving it running (the #3388 symptom `lease_expired`).
- Observation after commit is not `source` (for example `target`/`mixed`/`conflict`): conflicts are
  recorded by the existing path, anything else is refused as today.
- Release after resolve: unchanged `recovered` -> `cleanup` path.

## Test plan (tests first)

Red tests, each verified red against main:

1. Adapter: remote `commit` for a `resolve-conflict` request with an acknowledged third definition
   recovers from that definition, leaves recovery objects, and `observe` returns `source`.
   (Today the request is refused by `commit` as not admitted.)
2. Service: a `resolve-conflict` request does not take the cleanup-evidence path (no
   `resolve_cleanup_evidence`, no `commit_cleanup`); the adapter's `commit` is called.
3. Handler: `resolve_conflict_handler` commits terminal `recovered` on `source` and still refuses
   `absent`. The module lifecycle for `RESOLVE_CONFLICT` is `("restore",)`.
4. Wire: `RemoteModuleLifecycleRequestV1` refuses a `reap` under `RESOLVE_CONFLICT`.
5. Host: `_reap` with no markers and both volumes absent returns `volumes_absent` without reading the
   scratch result and without writing a marker; with a volume present and a mismatching result it
   still raises; `reaped` + present volume still raises. (Applies to CLEANUP and TEARDOWN too.)
6. Worker evidence: a CLEANUP retry after discharge whose host answers `volumes_absent` discharges
   idempotently (already covered; keep).
7. Host: restore volumes absent, no markers, reap obligation open (the swept shape) returns
   `volumes_absent`. Service: no `resolve-conflict` request reaches `commit_cleanup`.
8. Update the #3412 tests that asserted restore-then-reap for resolve and the accepted validator.
9. Live (#3388): keep `terminal`/`resolve-conflict`/`source`/`source`; add a release after the
   resolve ending `absent` on `[remote]`; add `[local]` only after confirming local release after a
   resolve reaches `absent` (unverified here). No lab work in this change.

Controlled-fault check: restore the cleanup-evidence routing for `resolve-conflict` and confirm test
2 and test 1 go red; restore the scratch re-read for the swept case and confirm test 5 goes red.

## Residual risks

- No remote resolve ever committed on main, so no activation is left `recovered` with deleted
  recovery objects.
- Deploy order for A: authority first (see Wire).
