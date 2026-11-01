# System teardown keeps an adopted cleanup receipt (#3467)

Decision record: [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md), amended
2026-11-01 by this change; its amendment carries the rejected alternatives and the lifecycle of
the kept receipt. Follows #3435 (PR #3445).

## Problem

`ops.resolve_recovery_orphan` with the `adopt` disposition marks a cleanup quarantine receipt
`managed` and moves its row to `adopted`. System teardown then disagrees between providers:

- Local: `discard_system_teardown_recovery`
  (`local_libvirt/lifecycle/boot/external_boot.py`) always calls the store's
  `finalize_tombstone`, which unlinks the receipt and removes the recovery directory.
- Remote: teardown removes no store file (`recovery_absent` reads libvirt volumes only), so the
  receipt stays and teardown completes.

## Decision

System teardown leaves a `managed` receipt in place on both providers and still completes. On
local the recovery directory keeps exactly what an adopted release `finalize` leaves: the
tombstone and the managed receipt. Criterion (2)'s "observable" means the provider store holds
the receipt as `managed`; the local recovery-object port cannot open a session once the domain
is gone, so on local that observation is the store read (ADR amendment).

## Design

Local only; remote already behaves this way.

- **Discard path.** In `LocalSystemTeardownIO.discard_system_teardown_recovery`, after the
  proof check, read the standing receipt. When it is `managed`, require
  `receipt.tombstone.recovery_point == recovery` (else `ValueError`), remove validated intent
  and preparation residue beside the exact tombstone (`drop_tombstone_residue`, the same checks
  `finalize_tombstone` applies), and return. Unmanaged or absent receipts take today's path.
- **Teardown absence.** `RecoveryMetadataStore.exact_recovery_absence(binding, *,
  adopted_receipt_absent=False)`. With the flag, the recovery directory (not the `.partial`
  one) counts as absent when its entries are exactly the tombstone and the receipt, the receipt
  reads canonically as `managed`, and the tombstone file equals `receipt.tombstone`. Every other
  location keeps its rule. The five System-teardown callers pass the flag:
  `begin_system_teardown`, `observe_system_teardown`, `system_teardown_recovery_is_absent`, and
  both checks in `teardown_system`.
- **Release readers** (`recovery_is_absent`, `_resolve_point` in the local authority) do not
  take the flag. They see the same directory an adopted release `finalize` leaves, so their
  result for that activation equals the one #3435 already produces.
- **Remote.** No code change. A test pins that teardown after adoption completes and the port
  still observes the adopted receipt, so #3472's collection cannot silently remove it.

## Failure model

1. **Actors and deployments.**
   - The provider authority service on a local-libvirt or remote-libvirt host, running an
     authorized System teardown.
   - A platform administrator who earlier adopted the receipt.
2. **Invariants and assets at stake.**
   - A `managed` receipt survives teardown on both providers' stores.
   - Teardown completes (domain, overlay, baseline and recovery absent) with it standing.
   - An unmanaged or absent receipt is torn down exactly as before on both providers.
   - The local absence change applies only to the five System-teardown callers.
3. **Accepted failure classes.**
   - Adopted receipts accumulate on hosts. Accepted: one small directory or file per adoption,
     bounded by audited operator decisions; the ADR amendment names who removes it.
   - After local teardown the recovery-object port cannot observe or act on the receipt.
     Accepted: the port's session needs the System domain for every row, not only adopted
     ones; a follow-up candidate, not this change.
   - A corrupt adopted receipt or tombstone makes the local teardown absence check raise, so
     teardown does not complete. Accepted: both are canonical JSON the store wrote, and a
     corrupt one already refuses every other read.
4. **Covered elsewhere.**
   - Remote collection of a torn-down activation's other store files: #3472.
   - Re-recording a managed receipt: #3468. Release finalize: #3435.
   - The adopted row's schema and transitions: unchanged, no migration.

## Success

1. ADR-0787 carries a 2026-11-01 amendment stating the teardown rule, the receipt's lifecycle,
   the #3472 boundary, and the rejected alternatives.
2. Local: with an adopted receipt, teardown discard leaves exactly the tombstone and receipt
   (removing intent residue when present), teardown completes, and the store reads the receipt
   as adopted.
3. Local: with an unmanaged receipt standing, teardown absence stays false (discard still runs).
4. Local: the existing `test_system_teardown_*` cases still pass.
5. Remote: with an adopted receipt, teardown completes and the port observes the adopted receipt.

## Validation

- Success 2 and 3: `focused-test`,
  `tests/providers/local_libvirt/test_external_boot.py::test_system_teardown_keeps_an_adopted_receipt`.
  Red before the fix: the recovery directory is gone after discard (`assert directory.is_dir()`).
- Success 4: the existing cases in the same file.
- Success 5: `focused-test`,
  `tests/providers/remote_libvirt/test_external_boot_authority.py::test_remote_system_teardown_keeps_an_adopted_receipt`.
  It passes on base; its red is a controlled fault that finalizes the receipt during teardown.
- Success 1: `task-test-not-applicable`. The amendment is prose with no executable consumer;
  `just records` checks the record shape.
