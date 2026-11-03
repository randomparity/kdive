# 0787 — Abortable before-finalize proof checkpoint

## Status

Accepted (2026-10-19)

## Context

`ops.resolve_recovery_orphan` acts on `external_boot_recovery_quarantine` rows. Only the
authority writes one: when cleanup finalization fails after the terminal journal record,
`_publish_cleanup_quarantine` re-reads the private receipt and publishes it through the
head-fenced SQL function ([ADR-0615](0615-authority-owned-orphan-disposition.md)). On a healthy
host that failure does not happen. The [ADR-0622](0622-native-authority-fault-proof-barrier.md)
barrier pauses only around the provider commit, and a restart there recovers through
`_finish_recovery` and quarantines nothing. Issue #3185 needs a real quarantined orphan for live
proof. It forbids synthetic database writes and unreviewed proof-control expansion.

## Decision

We will extend the ADR-0622 barrier in three ways.

- **`before-finalize` checkpoint.** An arm may name it only with operation `cleanup`.
  `_finalize_adapter` awaits it just before `adapter.finalize`, outside the `try` that maps
  provider errors.
  - `ProofBarrierAbort` is re-raised through `_provider_error` as `provider_conflict`, so the
    caller publishes the quarantine through its unchanged fences.
  - `ProofBarrierShutdown` propagates unmapped, so a service shutdown never publishes one.
- **`abort` action.** It is valid only while the single arm is `reached` at `before-finalize`.
  It fails that one wait with `ProofBarrierAbort` and clears the arm.
- **`release` of a never-reached arm clears it.** A later matching checkpoint finds no arm and
  passes, which is the same effect as the pre-release had. `checkpoint()` clears its arm in a
  `finally`.

These ADR-0622 properties are unchanged:

- the socket is root-only by `SO_PEERCRED`;
- only one arm exists at a time;
- requests are closed JSON of at most 1024 bytes;
- no provider, path or command is selected;
- shutdown never releases a wait.

## Consequences

- The local installed authority can produce one real quarantine row on demand, bound to an exact
  System, Run and cleanup operation.
- The release job's next attempt finalizes normally and removes the receipt. The orphan the
  resolver then sees is already absent at the provider, so the resolver's delete of a present
  receipt stays unproven.
- The remote authority has no finalizer, so this checkpoint never fires there.
- The pause happens inside the System's authority lane lock. While it is held, other requests for
  that System wait, the recovery-orphan serializer included. The proof bounds the pause to 120 s.
- An abort costs no service restart.
- An arm that was armed and then released, but never reached, no longer strands the barrier as
  `busy`.

## Considered & rejected

- **Record the local orphan as blocked too.** judgment: this leaves authority publication
  unprovable on every lane, when a bounded seam reaches the real publisher.
- **Pause at `before-finalize` and restart the authority.** verified: both authority unit
  templates set `StartLimitBurst=5` and `StartLimitIntervalSec=600`
  (`deploy/ansible/roles/*/templates/*authority*.service.j2` at `b2d93172e`). Under this ADR the
  shutdown path also deliberately publishes nothing.
- **Perturb the private tombstone directory so `finalize_tombstone` raises.** judgment: this
  couples the proof to private layout, and the same entry would also break the resolver's
  delete.
- **Seed quarantine rows with SQL.** verified: migration 0144 lets only
  `kdive_provider_authority` execute `publish_external_boot_recovery_quarantine_authority`. A
  seeded row also proves no authority publication.
- **Keep the arm armed across retries, so the receipt survives to resolution.** judgment: under
  ADR-0714 the second identical `provider-conflict` ends the release job, and recovering it is
  #3143's path, which #3185 excludes.
- **Saturate the provider offload pool.** verified: `_OFFLOAD_CAPACITY = 4` is a non-blocking
  admission (`src/kdive/providers/local_libvirt/external_boot_authority.py`, at `b2d93172e`).
  Filling it depends on timing, which ADR-0622 rejected.

### Amendment (2026-10-27): the remote authority finalizes and quarantines cleanup (#3326)

This amendment replaces the Consequences bullet "The remote authority has no finalizer, so this
checkpoint never fires there". The remote-libvirt authority now fires the checkpoint and
publishes a quarantine row like the local one.

- After release cleanup, `commit_cleanup` records a private `cleanup-quarantine` receipt in the
  module store, named by the recovery record's identity. It binds the operation identity, the
  attempt and the anchored `mutation-started` journal sequence and digest.
- `finalize` runs for `cleanup` only. It requires the receipt to equal the anchored proof, writes
  a `cleanup-finalized` marker, then removes the recovery record, the indexes that name it and,
  last, the receipt. It is repeatable until only the marker remains, and a missing receipt is a
  no-op. A cleanup for another operation is refused before any provider call while a receipt
  stands.
- `cleanup_quarantine_inventory` reports the one unmanaged `recovery-record` observation whose
  operation identity and attempt equal the request's. Any other receipt is a conflict.
- The remote adapter serves `observe`, `delete` and `adopt` for `recovery-record` under digest
  fences. `delete` finalizes the record. Other object kinds are refused, not routed elsewhere.
- The remote branch of the authority host builds the orphan service over the remote adapter, not
  the local one.
- The marker keeps a finalized cleanup distinguishable from a record that never existed, so a
  later cleanup `observe` classifies `absent`, as it did while the record remained. It also
  locates the receipt once the recovery index is gone. The object port and inventory report the
  receipt actually on disk, whatever the marker says, so an interrupted finalize never hides
  residue. System teardown already treats an absent record as `recovery_absent`.

### Amendment (2026-10-27): remote finalize also removes the activation's other files (#3333)

After the marker, remote `finalize` removes the activation's remaining per-activation files (terminal, restored, prior-power, preparation, materialization, lifecycle and request records) before the recovery record, keeping the System `teardown` record. The marker is canonical JSON carrying the module volumes, the boot artifact refs and the PREPARE nonce, so System teardown proves the same set absent after finalize; a malformed marker, or a marker from before it carried refs once the terminal record is gone, makes teardown refuse rather than prove nothing, and nothing rewrites such a marker (it exists only from the unreleased #3367 build). A later read, stage or prepare for a finalized activation raises `RemoteCleanupFinalizedError`.

### Amendment (2026-10-29): an adopted cleanup finalizes under the proof it ran with (#3416)

A retried release generation adopts the earlier generation's terminal cleanup, so `commit_cleanup`
does not run again and the receipt keeps that cleanup's operation identity, attempt and anchored
journal position. The service therefore builds the `finalize` context from the adopted operation's
`mutation-started` record (the oldest, when several retries were interrupted), not the retry's, and the remote adapter's exact comparison with the
receipt holds. The receipt is not rewritten, so the published quarantine row and the object port's
digest fences keep naming what the first attempt recorded.

### Amendment (2026-10-30): release finalize leaves an adopted receipt in place (#3435)

`ops.resolve_recovery_orphan` with the `adopt` disposition marks the cleanup quarantine receipt
`managed`, and the quarantine row becomes `adopted`. A later attempt of the same release job still
reaches `finalize`. Before this amendment, both providers then removed the adopted receipt and the
records it proves. The remote adapter never read `managed`, and the local finalizer unlinked the
receipt with the tombstone.

- Release `finalize` makes no change while the standing cleanup receipt is `managed`. Remote
  `finalize` returns before its proof comparison. Local `finalize_cleanup_tombstone` reads the
  receipt under its pinned session and returns. An unmanaged or absent receipt finalizes as
  before.
- The two providers agree at `finalize`. Inventory, observation, recovery-object `delete` and now
  release `finalize` all leave a managed receipt alone, so release does not make the `adopted`
  row stale.
- System teardown's discard path (ADR-0583) is unchanged: destruction still owns its cleanup.

Considered & rejected:

- **Keep removal, and have the resolver's adopt warn that a retry may still finalize.** judgment:
  this leaves the database recording `adopted` for an object the provider later deletes, and it
  makes an audited administrator decision depend on whether a retry happens to follow.
- **Raise a conflict on a managed receipt.** verified: `rg -n '"managed": (False|True)' src/kdive`
  at `37e2db046` finds only the two adopt writes setting `True`, plus the remote marker's
  `False` copy, which never rewrites the receipt. No resolver path un-adopts a receipt, so every
  later retry would fail the same way, and under ADR-0714 the second identical
  `provider-conflict` ends the release job.

### Amendment (2026-10-30): an adopted cleanup publishes its quarantine under the proof it ran with (#3434)

The #3416 amendment applied the adopted proof to `finalize` only. A retried release generation
that is aborted again before `finalize` still published under its own CLEANUP identity. The
receipt names the adopted cleanup's identity, so the remote inventory refused it, and the
repository and publish function refused it too. A shutdown at the first `before-finalize` publishes
nothing. When an aborted retry followed it, no row could be published at all.

- The service derives one proof context, the adopted operation's `mutation-started` record (the
  oldest, or the request's own when nothing was adopted), and passes it to both `finalize` and
  `cleanup_quarantine_inventory(request, context)`. The remote inventory compares the receipt's
  operation identity and attempt with that context instead of the request. The local
  adapter takes the parameter and keeps its request comparison. That gap is a follow-up.
- Each published object carries its own operation identity. Migration 0186 stores that identity on
  the row and compares an existing row against it. It is admitted when it equals the current
  authority's identity, or, for a `release` authority only, when it equals the 0143 `cleanup`
  derivation of an earlier acknowledged `release` authority row of the same system, activation,
  run, plan, provider kind and authority instance. Every other identity is refused. An object with
  no identity falls back to the current authority's, so a binary older than 0186 publishes as before. The row
  therefore names what the receipt names, and the resolver's binding equals the object port's.

Considered & rejected:

- **Return no observation from a retry whose receipt belongs to an adopted operation, relying on
  the earlier generation's row.** judgment: this leaves a shutdown followed by an aborted retry
  with no row, and the issue requires a row however many times the release was interrupted.
- **Do nothing.** judgment: an aborted retry stays unresolvable, and the issue requires a row.
- **Store the retry's identity on the row and have the resolver translate it.** verified: the
  resolver refuses unless the provider's observed binding equals the one it rebuilds from the
  row (`src/kdive/providers/external_boot_authority/orphan.py`, `orphan_binding_changed`). The
  remote observation names the receipt's identity, so a row stored under the retry's identity
  could not be resolved without changing the disposition path.
- **Accept any caller-supplied identity in the publish function.** judgment: a
  `SECURITY DEFINER` boundary would then trust the authority worker for a value the database
  can derive itself.

### Amendment (2026-10-30): remote finalize removes only what it can prove and names the remedy (#3419)

The #3326 and #3333 reviews left six low findings on remote `finalize_cleanup_quarantine`. Five
are fixed and one, the unbounded scan, is accepted. The #3435 and #3434 amendments are unchanged: a `managed` receipt still returns before
any of this runs, and the receipt is still compared with the derived proof context.

- The recovery index is removed only while its content names the record being finalized. The
  check no longer depends on the record still being present, so a retry after the record went
  first also removes the index. An index naming another record stays in place.
- Accepted limitation: the finalize directory scans stay unbounded. Nothing removes a store's
  files except finalize. A finalized activation keeps its marker, and an adopted or never-finalized
  activation keeps its `.request` and other files, so the store grows for the life of the host
  until something collects a torn-down activation's files. Under a bound, finalize would refuse
  once the host passed it. Releasing or tearing down another activation runs into the same
  bound, so the refusal would offer no remedy short of deleting files by hand. An unbounded scan
  costs time proportional to the store but never refuses.
- System teardown's existing 4096-name bound in `teardown_preparation` is kept. It now counts
  only `.request` names, because counting every name let accumulated markers stop teardown too.
- The `.request` conflict check compares the whole module operation, as `teardown_preparation`
  does, rather than only the nonce. Finalize no longer records a nonce that teardown would refuse.
- A `.request` file that does not parse, and a marker without teardown refs once the terminal
  record is gone, now say what to do next. For the request: the file may belong to any activation
  on the host, so keep a copy and find the System and run it names, or take every System on the
  host if the copy names none. Confirm no module volume of those Systems remains unaccounted for
  in the pool, remove the file, then retry finalize. Removing it without that check could let a
  finalize record no nonce, and that activation's unfinished-preparation proof at teardown would
  pass without looking at the pool. For the marker: confirm
  the activation's module volumes are gone from the pool, remove the marker, then retry teardown.
  Without the marker, teardown reads the live records it did before finalize.
- Crash-window tests cover the index removed with the record present (the retry finds the receipt
  through the marker) and the record removed with the index present (the retry finds it through
  the index). Each test fails if the index-then-record order or that retry path changes.
- The reviews described `refuse_finalized` followed by a write as relying on a single executor.
  That description is wrong: the module executor runs up to four calls at once. The
  check-then-act is ordered by the authority service's one active mutation per System lane
  (ADR-0584), and the `refuse_finalized` docstring now says so. A second authority instance
  writing the same store directory would break it.

Considered & rejected:

- **Scan `.object-index` files by content when the record is gone.** judgment: object indexes are
  removed before the index and the record, so a retry that finds the record gone has already
  removed them. The scan would add work and find nothing in that state.
- **Bound the finalize scan and refuse past it.** judgment: the refusal has no working remedy
  while store files accumulate (above); the operator chose to accept the unbounded scan and track
  store-file collection separately.
- **Add a store lock around `refuse_finalized` and the write.** judgment: the System lane already
  serializes those calls within one service. A lock would only help a second process writing the
  same store directory, which this change does not introduce.

### Amendment (2026-11-01): System teardown keeps an adopted receipt (#3467)

This amendment replaces the #3435 bullet "System teardown's discard path (ADR-0583) is
unchanged: destruction still owns its cleanup". Local teardown unlinked an adopted receipt with
its tombstone, so the `adopted` row named a receipt the provider had deleted. Remote teardown
removes no store file, so the receipt survived there.

- System teardown leaves a `managed` receipt in place on both providers and still completes.
  ADR-0583 limits teardown to objects whose activation ownership is provable; an adopted object
  is the operator's.
- Local: when the standing receipt is `managed`, the teardown discard path removes only intent
  and preparation residue a crash inside `publish_tombstone` left, so the recovery directory
  keeps the tombstone and receipt that release `finalize` also leaves (#3435). System teardown's recovery-absence check counts a directory holding
  exactly that tombstone and a `managed` receipt carrying the same tombstone as absent. Release
  readers keep the stricter check and see the state an adopted release already leaves.
- Remote: unchanged. Teardown already leaves the receipt.
- The receipt is observable on both hosts' stores. The remote recovery-object port still reads
  it after teardown; the local port opens a session that needs the System's domain, so after
  local teardown the receipt is visible on disk only.
- Nothing in kdive removes an adopted receipt after teardown; the operator who adopted it does.
  On local, remove the whole recovery directory, not only the receipt, or teardown absence reads
  false again. On remote the receipt is `<recovery identity>.cleanup-quarantine`, found by the
  row's object reference without the index or the `cleanup-finalized` marker. A marker written
  before adoption carries an unmanaged copy and does not own the receipt.
- Remote store collection at teardown (#3472) may remove the activation's other files,
  including that marker, and keeps a `managed` receipt.
- Local adoption and finalize decide under one lock. Nothing serializes an adoption against
  System teardown of the same System: the authority's System lane releases its lock for the
  provider call (`execute_mutation` in
  `src/kdive/providers/external_boot_authority/service.py`), the recovery-orphan serializer
  takes that lock without checking the active operation, `ops.resolve_recovery_orphan` checks
  no System state, and both run on the local adapter's shared thread pool. So
  `RecoveryMetadataStore.finalize_tombstone` and `adopt_cleanup_quarantine` each hold an
  exclusive `flock` on the activation's recovery directory across their read and write.
  Finalize re-reads the receipt under it and keeps a `managed` one with its tombstone, as the
  adopted discard branch does; an adoption that finds the receipt gone fails as changed. This
  corrects the earlier "System lane already serializes" judgment for this pair; it does not
  cover `record_cleanup_quarantine`.
  #3492 extends the lock there: `record_cleanup_quarantine` takes it, re-reads the receipt under
  it and returns on a `managed` one, so an adoption landing between its check and its replace is
  never overwritten with an unmanaged receipt.

Considered & rejected:

- **Remove the receipt on both providers and settle the row.** judgment: it undoes an audited
  administrator decision whenever the System is later destroyed, the objection #3435 raised
  for release. verified: the status check in
  `src/kdive/db/schema/0138_external_boot_recovery_quarantine.sql` allows only `quarantined`,
  `adopted` and `deleted`, and no function moves `adopted` onward, so settling the row needs a
  migration; the remote half needs the store collection #3472 owns.
- **Keep the receipt alone and remove the tombstone at teardown.** judgment: a new store
  method and a crash window between unlinks, for no reader that needs the tombstone gone.
- **Keep the directory without changing the absence check.** verified: `exact_recovery_absence`
  returns false while the recovery directory exists, and `_prepare_system_teardown_recovery`
  then returns before teardown destroys the domain, so an adoption would block destruction,
  which ADR-0583 forbids.
- **Do nothing.** judgment: the providers keep disagreeing and the local row stays stale.

### Amendment (2026-11-02): remote System teardown collects the activation's store files (#3472)

This amendment narrows the #3419 sentence "Nothing removes a store's files except finalize". The
#3419 accepted limitation on unbounded scans stands until #3475 replaces it.

- When a remote System teardown call (execute or observe) finds its record in phase `complete`,
  the store removes the torn-down activation's files under an exclusive `flock` on the store
  directory: the activation files finalize removes, then per recovery identity the object
  indexes, the recovery record and the cleanup receipt, then the recovery index and the
  `cleanup-finalized` marker. Each anchor goes after what it names, so a retry after a crash
  finds every remaining file; a never-finalized activation has no marker to find its record
  through. Observe collects too because the service recovers an interrupted teardown by
  observing it. A collection failure is logged and the call still reports the completed
  teardown. The service's observation right after the execute collects again; if that fails
  too, the files stay once the teardown is journaled terminal.
- A `managed` receipt, the recovery record it quarantines and the recovery index stay, as the
  2026-11-01 amendment requires, so the binding lookup and the recovery-object port still find
  it. The operator who removes an adopted receipt after teardown removes all three:
  `<recovery identity>.cleanup-quarantine`, `<recovery identity>.recovery`, and the
  `.recovery-index` naming that identity.
- Remote `adopt_cleanup_quarantine` and `finalize_cleanup_quarantine` take the same store lock.
  Collection either sees the receipt adopted or removes it before the adoption reads it, and
  that adoption fails as changed. Finalize writes nothing for a torn-down activation, so an
  orphan delete that read the receipt before collection cannot publish a marker after it.
  Finalize still does not re-check `managed` under the lock.
- The System's `.teardown` record is kept. `cleanup_finalized`, and through it
  `refuse_finalized`, is also true when that record is `complete` and names the activation, so
  a late stage stays refused after the marker is collected, and a cleanup observation of the
  activation still reads `absent`. Once that record is `complete`, `system_teardown_state`
  reads none of the activation's records, so a half-collected store cannot refuse a retry or an
  observation; the recovery volumes were read absent before `complete` was written.
- Other activations of the same System, and stores torn down before this change, keep their
  files. Each System's `.teardown` record stays for the life of the host, as before, and is now
  the late-stage refusal.

Considered & rejected:

- **Keep the `cleanup-finalized` marker as the late-stage refusal.** judgment: one file per
  activation for the life of the host, and a never-finalized activation has no marker, so a
  late stage for it was never refused.
- **Write a new per-activation teardown tombstone.** judgment: the per-System `.teardown` record
  already carries the binding, plan and `complete` phase, and is written before collection.
- **Collect in a separate garbage-collection step.** judgment: a new entry point and scheduler
  for work the teardown calls already reach at the one moment the activation is known dead.
- **Collect only in `execute_system_teardown`.** verified: `AuthorityService._recover` resolves
  a `mutation-started` teardown through `_recovery_observation`, which calls
  `observe_system_teardown` and never executes again
  (`src/kdive/providers/external_boot_authority/service.py`, commit 3b417bd78), so a crash
  during collection would leave its files for good.
- **Let a collection failure fail the teardown call.** judgment: it reports a provider conflict
  for a System already destroyed, on every retry, while the refusal it would protect already
  stands.
- **Re-check `managed` just before unlinking, without a lock.** verified: remote
  `adopt_cleanup_quarantine` is a read-compare-`os.replace` in `_replace`
  (`src/kdive/providers/remote_libvirt/external_boot_authority.py`), so an adoption can land
  between any re-check and the unlink.
- **Do nothing.** judgment: the store grows without bound and #3475 has no remedy to restore.

### Amendment (2026-11-02): System teardown collects every activation it can prove (#3486)

This amendment replaces the #3472 bullet "Other activations of the same System, and stores torn
down before this change, keep their files." The rest of the #3472 amendment stands.

- Collection on a `complete` teardown record covers every activation whose file content names the
  System's `system_id`, found in `.lifecycle-request`, `.request`, `.preparation`, `.terminal`,
  `.recovery`, `.cleanup-quarantine` and `.cleanup-finalized` files. The activation the record
  names is collected as before. Any other activation is collected only when its
  `cleanup-finalized` marker exists, or when every pool volume its records name (module source and
  scratch volumes, the volumes its operation nonce renders, and its kernel and initrd) reads
  absent. Otherwise its files stay and a warning names it: those records are the only pointer to
  volumes that may remain.
- The proof is read outside the store lock. Under the lock the activation's claims are read again,
  and a change keeps the files. A kept activation's plan keeps its materialization record, which
  every activation of the same Run shares.
- Every non-anchor file of an activation goes before its anchors (the suffixes listed above), so
  an interrupted collection leaves each remaining activation findable by the next. The recovery
  index now goes before the recovery record, replacing the #3472 order: collection finds the
  record's identity from the activation's own `.recovery` and `.cleanup-quarantine` content as
  well as from the index and marker, and an index left last would be stranded once the records
  it names are gone.
- Consequences: a kept activation's files stay for the life of the host, with a warning on every
  teardown call that re-proves it; an adopted activation's trio is re-proven the same way. Each
  proof costs one libvirt connection. Only the runbook remedy clears a kept activation.
- `cleanup_finalized`, `refuse_finalized`, finalize's no-write check and the
  `system_teardown_state` short-circuit hold for every activation of a System whose `.teardown`
  record is `complete`. A `system_id` is never re-activated: `systems.id` is generated per row
  (`src/kdive/db/schema/0001_init.sql`) and `torn_down` has no outgoing System or activation
  edge (`src/kdive/domain/capacity/state.py`).
- Stores torn down before #3472 have a manual remedy, not a sweep:
  [remote-libvirt host setup](../operating/runbooks/remote-libvirt-host-setup.md#clear-store-files-of-torn-down-systems).
  The operator who removes an adopted receipt after teardown still removes the three files the
  #3472 amendment names.

Considered & rejected:

- **Do nothing.** judgment: every re-activated or adopted activation of a torn-down System keeps
  its `.request`, `.lifecycle-request` and `.preparation` files, which #3475's bounded scans
  count, so that bound's refusal would tell the operator to tear down Systems that are already
  torn down.
- **Collect only the named and the finalized activations.** judgment: needs no pool proof, but
  leaves every never-finalized sibling, the case #3486 reports, in place.
- **Collect every activation of the System unconditionally.** judgment: deletes the only record
  of a sibling's volumes when teardown proved only the named activation's volumes absent
  (`_protected_teardown_volumes` in
  `src/kdive/providers/remote_libvirt/external_boot_operations.py`).
- **Prove sibling volumes absent at teardown, before `complete`.** judgment: by construction a
  sibling's leftover volume, which teardown does not remove (a #3486 operator exclusion), would
  then keep the System's teardown from reaching `complete`.
- **Hold the store lock across the libvirt proof.** judgment: blocks every adoption and finalize
  on that host for the duration of libvirt calls; re-reading the claims under the lock fails
  closed instead.
- **Ship a one-time sweep for pre-#3472 stores.** judgment: a new entry point that deletes
  persisted state on hosts nobody is watching, for a backlog a drained-host procedure clears once.

### Amendment (2026-11-03): release finalize is bounded, System teardown is not (#3475)

This amendment supersedes the #3419 bullets "Accepted limitation: the finalize directory scans
stay unbounded" and "System teardown's existing 4096-name bound in `teardown_preparation` is
kept", and the 2026-11-02 sentence "The #3419 accepted limitation on unbounded scans stands until
#3475 replaces it". The #3419 rejection "Bound the finalize scan and refuse past it" no longer
holds: #3472 and #3486 gave the bound a remedy.

- Remote `finalize_cleanup_quarantine`, reached by release cleanup finalize and by the orphan
  delete, counts the store's names of 64 hex characters followed by `.request`,
  `.lifecycle-request` or `.preparation`. The count runs under the store lock, after the
  torn-down no-op check and before the `cleanup-finalized` marker is written. Above 4096 it
  raises a fixed `ProviderRecoveryRefusal` (ADR-0707) and writes nothing. A refused release
  finalize takes the existing failure path: the authority publishes the cleanup quarantine, and
  a later release attempt or orphan delete retries finalize.
- The counted suffixes are the files finalize parses by content and a completed System teardown
  collects, so the remedy lowers the count. Finalize reads a `cleanup-finalized` marker and its
  own `.recovery` record by exact name; only collection scans `.recovery` by content (orphan
  `.recovery` collection stays out of scope). Each `.teardown` record stays for the life of the
  host, so counting it could make the refusal permanent. None of the three is counted, and
  bounding markers and `.teardown` records stays out of scope.
- The refusal names two remedies: tear down Systems on the host, so teardown collects their
  records, or, once every System on the host is torn down, the runbook procedure
  [Clear store files of torn-down Systems](../operating/runbooks/remote-libvirt-host-setup.md#clear-store-files-of-torn-down-systems).
  Teardown does not remove three kinds of counted file: an activation whose volumes were present
  at teardown, a System torn down before #3472, and a counted file that does not parse.
  Collection leaves the last with a warning, and the strict scans in `activation_claims` then
  fail, so every never-finalized sibling on the host is kept. Only the runbook procedure clears
  these. A host where they alone exceed the bound refuses every release and orphan delete,
  possibly from the first deploy of this change, until it is drained.
- `teardown_preparation` no longer bounds its scan, and System teardown and its collection
  (`system_activations`, `activation_claims`, `_collect`) stay unbounded. They are the remedy: a
  teardown bound stopped teardown before `complete`, so nothing was collected and no operator
  action short of deleting files by hand freed names. Teardown's cost grows with the store:
  collection parses about (activations of the System) x (counted files), because each sibling
  proof rescans. Nothing bounds it; it runs after `complete` and logs a failure instead of
  raising.
- `os.listdir` in finalize still lists every name, so uncounted files cost a name comparison each.

Considered & rejected:

- **Bound teardown too, above the release bound (for example 16384).** judgment: the trap moves
  further out; a teardown refusal could then only truthfully name deleting files by hand on a
  host with a live non-finalized activation, outside the runbook's precondition.
- **Bound release and teardown at one value.** judgment: with a live non-finalized activation on
  the host no operator action frees names, so the refusal text could not be true (attempt-1
  park note on #3475).
- **Bound inside `_scan_owned`.** judgment: collection shares it, and bounding collection
  bounds the remedy.
- **Do nothing.** judgment: #3475 requires a bound on release, and the teardown bound kept the
  trap.

### Amendment (2026-11-03): remote finalize re-checks `managed` under the store lock (#3493)

The 2026-11-02 amendment's sentence "Finalize still does not re-check `managed` under the lock"
no longer holds. Both callers read the receipt before `finalize_cleanup_quarantine` takes the
store lock, so an adoption could land between their read and the lock, and finalize then
removed the adopted receipt, its recovery record and the indexes.

- `_finalize_cleanup_quarantine` reads the receipt again under the lock before it writes the
  finalized marker or unlinks anything. An absent or `managed` receipt returns with nothing
  written or removed, as System teardown collection already treats an adopted one. A receipt
  that differs from the caller's in any other way is refused as
  `remote cleanup proof differs from the quarantine receipt`.
- The System lane still orders these calls in one authority process; the store no longer
  depends on that.

### Amendment (2026-11-03): remote finalize keeps a plan's materialization pair another activation needs (#3494)

Remote `finalize` removed the plan's `<identity>.materialization` record and plan index with the
rest of the activation's files, though every activation of a Run shares that pair. Finalizing
one activation then made the prepare or recovery of another fail with `remote materialization
plan index is absent` or `remote materialization record is absent`. Teardown collection already
protects the pair through `keep_plan` (#3486); finalize had no equivalent.

- `_finalize_cleanup_quarantine` decides `keep_plan` under the store lock, after its `managed`
  re-check (#3493). It keeps the pair while `system_activations` names another activation with
  the same `run_id` and plan identity that has no `cleanup-finalized` marker.
- A finalized sibling keeps its marker and so no longer counts. The last activation of the Run
  to finalize removes the pair, either at its own finalize or through teardown collection.
- The scan reads the same anchors teardown collection reads. It adds no refusal; its cost is
  linear in the store's files, after the existing 4096-name refusal.
- An activation with no anchor file yet is not found, so its plan pair is not kept for it.
- A sibling whose anchor files are unparseable is not found, so its plan pair is not kept for
  it. Such a file is left in place with a warning (#3486) and nothing proves whose it is.

Considered & rejected:

- **Reference-count the pair.** judgment: a new persisted counter that a crash between its
  update and the unlink would leave wrong, for a fact the anchors already carry.
- **Never remove the pair at finalize; leave it to teardown collection.** judgment: a Run
  released and finalized without a System teardown would leak the pair indefinitely. The chosen rule also keeps it while a sibling stays
  unfinalized, until that sibling finalizes or teardown collection removes it (#3486).
