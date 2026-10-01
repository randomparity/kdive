# Session-pinned recovery-object delete and adopt (#3045) — implementation plan

Goal: `LocalLibvirtExternalBoot.delete_recovery_object` and `adopt_object` re-read the quarantine
receipt and make their store write under `self._io.open(authority, _expected_binding(binding.binding))`.

Spec: [2026-10-01-session-pinned-recovery-object-writes-design.md](../specs/2026-10-01-session-pinned-recovery-object-writes-design.md).
Decision: ADR-0710 amendment (2026-10-01), on the branch.

Expected implementation size: 150–220 changed lines (M) — two port bodies (~20 lines), one test
IO hook (~15 lines), three new tests (~150 lines).

## Global Constraints

- Edit only `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` and
  `tests/providers/local_libvirt/test_external_boot.py`.
- No change to `observe_object`, `record_cleanup_quarantine`, `finalize_cleanup_tombstone`,
  `RealLocalExternalBootIO`, `RecoveryMetadataStore`, or the authority adapter (spec Design 2).
- Guardrails: `just lint`, `just type`,
  `just test-verbose tests/providers/local_libvirt/test_external_boot.py`, `just test-changed`,
  and the push hook's `just ci` run as `just ci > <file> 2>&1 < /dev/null`.
- Tests import libvirt doubles only from `tests.providers.local_libvirt.lifecycle.boot.session_support`.

## Task 1: pin both writes to an operation session

Interfaces (existing at `780a4bfd8`):
- `LocalExternalBootIO.open(authority, expected)`, `_expected_binding(binding)`,
  `LocalLibvirtExternalBoot._quarantine_observation(receipt) -> RecoveryObjectObservation`.
- `RecoveryMetadataStore(root)`: `publish`, `publish_tombstone`, `record_cleanup_quarantine`,
  `read_cleanup_quarantine`, `cleanup_complete`, `exact_recovery_absence`.
- Test helpers in `test_external_boot.py`: `_WriteObservingIO`, `_resolving_io`,
  `_RealSessionFactory` (`expected`), `_RealSession` (`close_attempts`), `_EXPECTED_OWNERSHIP`,
  `_cleanup_proof_for`, `_metadata`, `_point`, `_BINDING`; and the production-factory setup of
  `test_session_pinned_cleanup_writes_keep_exact_recovery_absence`.

### Verification

- S1 and S3. Mode: focused-test. `test_recovery_object_writes_run_under_one_session`,
  parametrized over delete and adopt. `_WriteObservingIO` records the number of open sessions
  (opens minus closes) at each `finalize_tombstone` and `adopt_cleanup_quarantine`. The setup
  seeds through the store and takes the expected digest from `_quarantine_observation`, so it
  opens nothing. A digest mismatch call opens one session (its observation) and records no write.
  The valid call opens three, all with `_EXPECTED_OWNERSHIP`, and records exactly one write,
  during its second open with one session open. Red before the source edit: the valid call opens
  two and the write records zero open sessions.
- S2. Mode: focused-test. `test_recovery_object_writes_refuse_when_the_write_session_cannot_open`,
  parametrized over delete and adopt and over two failure points on the second session (the write
  session): lease resolution raising, and the session factory's `open` raising after the lease
  resolves. The port raises and the stored receipt equals the original, unmanaged, with the
  tombstone present. Red before the edit: the unpinned write lands and the store comparison fails.
- S4. Mode: focused-test. `test_session_pinned_recovery_object_writes_with_the_production_factory`:
  the production factory stack, one `scope.issue` per port call; delete then `observe_object`
  absent, empty root, `exact_recovery_absence`; adopt leaves the receipt managed; three
  `domain.open` events per port call; outside a scope each port raises with the store unchanged.
  Red before the edit: two `domain.open` events per call instead of three.

### Steps

1. Add the open-session counter to `_WriteObservingIO`, then the three tests. Run them and observe
   the reds above.
2. Wrap the re-read, checks, and write of both ports in the session (spec Design 1). Rerun the
   focused file green.
3. `just lint`, `just type`, `just test-changed`; commit.
