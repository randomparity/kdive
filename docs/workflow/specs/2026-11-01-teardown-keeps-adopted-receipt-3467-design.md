# System teardown keeps an adopted cleanup receipt (#3467)

Decision record: [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md), amended
2026-11-01 by this change. Follows #3435 (PR #3445), which made release `finalize` leave an
adopted receipt in place.

## Problem

`ops.resolve_recovery_orphan` with the `adopt` disposition marks a cleanup quarantine receipt
`managed` and moves its `external_boot_recovery_quarantine` row to `adopted`. System teardown
then disagrees between providers:

- Local: `discard_system_teardown_recovery`
  (`local_libvirt/lifecycle/boot/external_boot.py`) always calls the store's
  `finalize_tombstone`, which unlinks the receipt and removes the recovery directory. The row
  says `adopted` for a receipt the provider deleted.
- Remote: teardown removes no store file (`execute_system_teardown` in
  `remote_libvirt/external_boot_authority.py`; its `recovery_absent` reads libvirt volumes
  only). The adopted receipt stays and teardown completes.

## Decision

Adoption protects the receipt from destruction as it does from release. System teardown on
both providers leaves a `managed` receipt in place and still completes. The receipt is the
only activation file teardown keeps for it. ADR-0583 limits teardown's authority to objects
whose activation ownership is provable; an adopted object belongs to the operator.

Considered alternatives are recorded in the ADR amendment: removing the receipt on both
providers and settling the row (needs a row transition from `adopted`, so a migration, plus the
remote store collection #3472 owns), and doing nothing.

## Design

Local only; the remote provider already behaves this way.

- **Discard path.** In `LocalSystemTeardownIO.discard_system_teardown_recovery`, after the
  proof check, read the standing receipt. When it is `managed`, require
  `receipt.tombstone.recovery_point == recovery`, call the new store method
  `retire_adopted_tombstone(recovery)` and return, without recording a receipt or calling
  `finalize_tombstone`. Unmanaged or absent receipts take today's path.
- **`RecoveryMetadataStore.retire_adopted_tombstone(recovery)`.** Opens the recovery
  directory. Refuses an entry outside {tombstone, intent, preparation, receipt}, a receipt that
  is not `managed`, or one whose tombstone differs from the expected one. When the tombstone is
  present, validates it and its residue as `finalize_tombstone` does. Unlinks intent and
  preparation, then the tombstone, and fsyncs. Keeps the receipt and the directory, then prunes
  empty activation parents. A directory already holding only the receipt is a no-op, so a retry
  after any crash point repeats safely.
- **Teardown absence.** `exact_recovery_absence(binding, *, adopted_receipt_absent=False)`.
  With the flag, the recovery directory counts as absent when its only entry is the receipt and
  that receipt reads (canonically) as `managed`. Every other location keeps its rule. The five
  System-teardown callers pass the flag: `begin_system_teardown`, `observe_system_teardown`,
  `system_teardown_recovery_is_absent`, and both checks in `teardown_system`. Release-side
  `recovery_is_absent` does not, so release observation is unchanged.
- **Remote.** No code change. A test pins that teardown after adoption completes and the
  receipt still observes as the adopted one, so #3472's collection cannot silently remove it.

### Lifecycle after the System is gone

Nothing in kdive removes an adopted receipt. The operator who adopted it owns it. On local it
is the only file in its recovery directory; removing that directory (not only the file) after
teardown completes leaves teardown absence true. On remote it is
`<recovery identity>.cleanup-quarantine` in the module store, found by the row's object
reference without the index or the finalized marker. The marker, when a finalize began before
adoption, carries an unmanaged copy and does not own the receipt. #3472 may collect the
activation's other files, including the marker, but keeps a `managed` receipt.

## Failure model

1. **Actors and deployments.**
   - The provider authority service on a local-libvirt or remote-libvirt host, running an
     authorized System teardown.
   - A platform administrator who earlier adopted the receipt.
2. **Invariants and assets at stake.**
   - A `managed` receipt survives teardown on both providers and observes as adopted.
   - Teardown still completes (domain, overlay, baseline and recovery absent; reservation
     reported) with an adopted receipt standing.
   - An unmanaged or absent receipt is torn down exactly as before on both providers.
   - The local absence change applies only to System teardown callers.
3. **Accepted failure classes.**
   - Adopted receipts accumulate on hosts. Accepted: one small file per adoption, bounded by
     audited operator decisions, which is what adoption means.
   - An operator who deletes only the local receipt file leaves an empty recovery directory, so
     a later teardown observation reports recovery present. Accepted: the lifecycle note says to
     remove the directory; the state is operator-made and visible.
   - A corrupt adopted receipt makes local teardown refuse instead of completing. Accepted: the
     receipt is canonical JSON the store wrote; a corrupt one already refuses every other read.
4. **Covered elsewhere.**
   - Remote collection of a torn-down activation's other store files: #3472.
   - Re-recording a managed receipt: #3468 (merged). Release finalize: #3435 (merged).
   - The adopted row's schema and transitions: unchanged, no migration in this change.

## Success

1. ADR-0787 carries a 2026-11-01 amendment stating the teardown rule, the lifecycle of the kept
   receipt, the #3472 boundary, and the rejected alternatives.
2. Local: with an adopted receipt, teardown discard leaves the receipt managed and alone in its
   recovery directory, teardown completes, and the object port observes the adopted receipt.
3. Local: a crash during the retire step, retried, reaches the same end state.
4. Local: the existing discard and teardown tests (unmanaged receipt) still pass.
5. Remote: with an adopted receipt, teardown completes and the object port still observes the
   adopted receipt.

## Validation

- Success 2: `focused-test`,
  `tests/providers/local_libvirt/test_external_boot.py::test_system_teardown_keeps_an_adopted_receipt`.
  Red before the fix: the receipt reads `None` after discard.
- Success 3: `focused-test`,
  `test_system_teardown_keeps_an_adopted_receipt_after_a_crash` in the same file. Red before
  the fix: the receipt reads `None`.
- Success 4: the existing `test_system_teardown_*` cases in the same file.
- Success 5: `focused-test`,
  `tests/providers/remote_libvirt/test_external_boot_authority.py::test_remote_system_teardown_keeps_an_adopted_receipt`.
  It passes on base; its red is a controlled fault that finalizes the receipt during teardown.
- Success 1: `task-test-not-applicable`. The amendment is prose with no executable consumer;
  `just records` checks the record shape.
