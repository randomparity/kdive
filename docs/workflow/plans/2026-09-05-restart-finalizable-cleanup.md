# Restart-finalizable local external-boot cleanup implementation plan

**Goal.** Make cleanup and teardown finish exact durable tombstones after authority restart, while
continuing only canonical producer-owned tombstone-publication residue.

**Architecture.** The existing v1 tombstone remains the sole durable receipt and carries the
complete recovery point. The recovery store validates and removes only exact producer records;
the authority adapter reconstructs from that receipt only after the service reaches its existing
authenticated terminal-finalization path.

**Tech stack.** Python 3.14, Pydantic closed models, descriptor-relative POSIX filesystem calls,
pytest, ruff, ty, and the repository `just` recipes.

## Global Constraints

- Support project targets `x86_64` and `ppc64le`; this campaign executes no native ppc64le proof.
- Preserve the existing v1 tombstone schema and introduce no database or filesystem migration.
- A tombstone alone is never deletion authority; finalization requires the authenticated,
  still-current terminal context selected by the authority service.
- Validate all directory content before deletion, use literal descriptor-relative names, and never
  recursively remove or accept unknown entries.
- Keep provider exception detail in host-local logs and return only the closed authority category.
- Use `just` recipes as the guardrail source of truth and keep 100-character lines.

Expected implementation size: 120–220 changed lines (M) — derived from two production modules and
their focused adapter/store regression matrices.

## File map

| Path | Responsibility |
| --- | --- |
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | validate and continue exact tombstone publication residue |
| `src/kdive/providers/local_libvirt/external_boot_authority.py` | reconstruct and finalize cleanup/teardown after restart |
| `tests/providers/local_libvirt/test_external_boot.py` | real filesystem continuation and fail-closed matrix |
| `tests/providers/local_libvirt/test_external_boot_authority.py` | service/adapter restart, teardown, replay, and mutation-count contracts |

## Task 1 — Continue exact tombstone publication residue

### Interfaces

- Consume `CleanupTombstoneV1`, `LocalRecoveryMetadataV1`, `LocalPreparationReceiptsV1`,
  `_read_private_file`, `_metadata_bytes`, `_preparation_bytes`, and `RecoveryPoint` as defined on
  current main.
- Preserve `RecoveryMetadataStore.cleanup_complete(reference: OpaqueProviderRef,
  recovery: RecoveryPoint) -> bool` for the coordinator and Task 2.
- Add only private helpers needed to reconstruct a point from metadata and validate preparation
  receipts against the expected tombstone.

### Verification

- Mode: focused-test. Contract: tombstone plus matching intent/preparation is reduced to the exact
  tombstone, including restart after either unlink. Tests:
  `test_cleanup_complete_continues_exact_tombstone_publication` and
  `test_cleanup_complete_retries_each_residual_unlink_boundary`. Expected red: existing
  `cleanup_complete` leaves residual files. Green command:
  `uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py -k 'tombstone_publication or residual_unlink' -q`.
- Mode: focused-test. Contract: foreign, changed, malformed, symlinked, non-private, or unknown
  residue is never removed. Test family:
  `test_cleanup_complete_refuses_untrusted_tombstone_residue`. Expected red: existing code returns
  true instead of rejecting. Use the same focused command and require all parameters pass.

### Steps

1. Add real-store fixtures that publish an exact tombstone, restore selected canonical residuals,
   and snapshot directory bytes/names before each call.
2. Run the focused command and record that matching continuation and rejection cases fail against
   the current `cleanup_complete` behavior.
3. Change `cleanup_complete` to validate the exact tombstone, inventory the complete directory,
   validate every known residual against its recovery point, and only then unlink the present
   literal residual names followed by directory fsync and tombstone re-read.
4. Inject failure after each unlink, reopen a fresh store, and prove the remaining subset completes
   without touching unknown or mismatched content.
5. Run the focused command and require every new case pass. Commit production code and tests as one
   logical crash-consistency change.

Acceptance: exact interrupted publication converges; every unproven directory remains byte-for-byte
unchanged; the tombstone-only finalizer contract is preserved.

Rollback: reverting the task restores quarantine of interrupted states and does not change record
formats.

## Task 2 — Reconstruct and finalize cleanup and teardown

### Interfaces

- Consume `LocalLibvirtExternalBoot.cleanup_receipt(binding, authority) -> RecoveryPoint | None`,
  `cleanup_is_accounted(recovery, authority) -> bool`, and
  `finalize_cleanup_tombstone(recovery, proof, authority) -> None` from current main.
- Preserve `LocalExternalBootAuthorityAdapter.commit`, `observe`, and `finalize` signatures.
- Reuse `_DELETING_OPERATIONS`, `_require_matching_identities`, `_ownership_is_proven`, and
  `_cleanup_proof`; do not add a second durable store or provider mutation.

### Verification

- Mode: focused-test. Contract: a fresh adapter finalizes both cleanup and teardown after the
  service anchors terminal state, and replay is idempotent without a second cleanup. Tests:
  `test_deleting_operation_restart_finalizes_durable_tombstone` and
  `test_deleting_operation_terminal_replay_is_idempotent`. Expected red: cleanup leaves the
  tombstone after restart and teardown never finalizes. Green command:
  `uv run python -m pytest tests/providers/local_libvirt/test_external_boot_authority.py -k 'restart_finalizes or terminal_replay' -q`.
- Mode: focused-test. Contract: mismatched request identities, unnamed recovery objects, malformed
  receipts, and stale operations never finalize. Test family:
  `test_restart_finalization_refuses_unproven_receipt`. Expected red is either missing teardown
  handling or insufficient identity rejection. Use the same focused command.
- Mode: focused-test. Contract: receipt reconstruction is load-bearing. Controlled fault: disable
  the tombstone fallback and require the restart-finalization test to fail with the recovery
  directory still present; restore production code before commit. Use the same focused command.

### Steps

1. Extend the fake IO and service fixtures to model a fresh adapter, exact tombstone, cleanup and
   teardown requests, terminal replay, malformed receipt, and provider mutation counters.
2. Run the focused command and record the expected stranded-tombstone and teardown failures.
3. Remove the in-memory pending-finalization map. Permit durable receipt fallback for both deleting
   operations in commit and observation, while preserving intent-first resolution.
4. In `finalize`, reject non-deleting operations; reconstruct the point, compare every request
   identity, require the request's named recovery ownership, and call the existing finalizer with
   the proof made from the supplied anchored context.
5. Make `_apply` use one cleanup branch for both deleting operations and preserve closed error
   mapping. Run the focused command and require all cases pass.
6. Apply the controlled fault, record the expected test failure and stranded directory, restore the
   fallback, and rerun green. Commit this adapter behavior separately.

Acceptance: cleanup and teardown converge after restart or terminal replay, no provider mutation is
repeated, and request/tombstone/context substitution fails closed.

Rollback: reverting the task leaves durable receipts intact for later recovery; it deletes no
persisted state and changes no schema.

## Task 3 — Verify the integrated contract

### Interfaces

- Consume the unchanged public/provider protocols and the Task 1/2 behavior.
- Produce no new runtime interface.

### Verification

- Mode: focused-test. Contract: combined real-store and adapter matrices cover all issue criteria.
  Expected red is not applicable after Tasks 1 and 2 because each executable contract has already
  been observed red before implementation. Green command:
  `just test-verbose tests/providers/local_libvirt/test_external_boot.py tests/providers/local_libvirt/test_external_boot_authority.py`.
- Mode: task-test-not-applicable. Surface: ADR/specification prose. No executable consumer validates
  prose wording; `docs-links`, `docs-paths`, `check-mermaid`, and `adr-status-check` validate its
  machine-checkable structure and links.

### Steps

1. Run the two focused test modules together and require all tests pass.
2. Run `just lint`, `just type`, `just docs-links`, `just docs-paths`, `just check-mermaid`, and
   `just adr-status-check`; require exit 0 from each bare command.
3. Re-read the diff for deletion scope, canonical parsing, identity checks, error redaction,
   idempotency, naming, and function size. Commit any behavior-preserving corrections separately.
4. Before push, run `just ci > <private-log> 2>&1 < /dev/null` as the exact full gate and require
   exit 0.

Acceptance: all focused and repository guardrails pass with no native ppc64le execution.

Rollback: no live resource is created. Test temporary directories clean themselves through pytest
fixtures.
