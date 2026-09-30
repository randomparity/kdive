# Non-System partial abort proves recovery absence — plan

Goal: implement `docs/workflow/specs/2026-09-29-activation-absence-proof-design.md` (#2926).
Architecture: `_ConcreteSession` opens its artifact root on first use instead of at construction
(ADR-0710); the non-System `abort_preparation` prunes still-empty activation parents.
Tech stack: Python 3.14, pytest.

Expected implementation size: 150–230 changed lines (M) — ~40 lines in `session.py`, ~5 in
`external_boot.py`, docstrings, ~40 lines of updated session tests, ~100 lines of new regressions.

## Global Constraints

- No change to `OpenArtifactRoot`, the on-disk layout, persisted records, `composition.py`, or
  `external_boot_authority.py`. No migration.
- Guardrails: `just lint`, `just type`, `just records`, focused `just test-verbose <paths>`;
  pre-push runs `just ci`. Commit before each controlled-fault arm; revert the fault with
  `git checkout -- <file>` only after that commit.
- Paths below are relative to `src/kdive/providers/local_libvirt/lifecycle/boot/` and
  `tests/providers/local_libvirt/` where abbreviated.

## Task 1 — the session opens its artifact root on first use

Files: modify `session.py`, `session_mechanisms.py` (`LocalArtifactRoot` docstring: creation
happens on a session's first artifact use, not at session open), `lifecycle/boot/test_session.py`;
add a test to `lifecycle/boot/test_session_mechanisms.py`.
Interfaces: `_ConcreteSession.__init__` takes `open_artifact_root: Callable[[], int]` in place of
`artifact_fd: int`; new private `_ConcreteSession._artifact_root(self) -> int`. Task 2 relies on
the factory creating nothing at open.

Verification:

- Contract S1 "open/close creates nothing; first use creates once", `Mode: focused-test`. New
  `test_artifact_root_is_created_on_first_artifact_use_only` in `test_session_mechanisms.py`:
  build the factory as `_session` does but with `open_artifact_root` wrapping
  `LocalArtifactRoot(recovery_root).open` and counting calls; open, `inspect_closed()`, close →
  `recovery_root` is empty and the count is 0. Reopen, `open_artifact("kernel", O_WRONLY|O_CREAT)`
  twice (closing each fd) → the chain exists with mode 0700 and the count is 1; close → the artifact
  descriptor was closed. Red before the change: count 1 and `<system>` present after the first
  open. Green: `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py`.
- Contract "ownership snapshot and close order are preserved", `Mode: focused-test`. Update the
  existing `test_session.py` tests that assumed an open-time call
  (`test_pinner_mutation_cannot_change_atomic_ownership_snapshot`,
  `test_artifact_callback_cannot_redirect_snapshot_by_mutating_caller_lease`,
  `test_artifact_callback_type_is_pin_free_and_cannot_release_lane`, and any close-order or
  `artifact.close` count test the run reports red) so each first calls
  `session.open_artifact("kernel", os.O_RDONLY)` (with `open_relative` stubbed where the fd is
  fake) and then asserts the original snapshot/order. Green:
  `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py`.

Steps:

1. Write the S1 test; run it; expect red (count 1 after open).
2. In `LocalExternalBootSessionFactory.open`, delete `artifact_fd` handling (the local, its
   assignment, and its closer in the `except` block) and pass
   `open_artifact_root=lambda: self._open_artifact_root(facts)`.
3. In `_ConcreteSession`, store `self._open_artifact_root_once = open_artifact_root` and
   `self._artifact_fd: int | None = None`, and add:

```python
    def _artifact_root(self) -> int:
        """Open the activation artifact root on first use; it creates the directories (#2926)."""
        with self._lifecycle_lock:
            self._require_open_domain()
            if self._artifact_fd is None:
                self._artifact_fd = self._open_artifact_root_once()
            return self._artifact_fd
```

4. Replace each `assert self._artifact_fd is not None` + use with `artifact_fd = self._artifact_root()`
   in `projection_directory`, `reopen_projection`, `projection_artifact_path`, `open_artifact`,
   `open_projection_artifact`, `unlink_artifact`, `cleanup_payloads`, `_download_artifact`.
   `close()` keeps its existing `artifact_fd is not None` guard.
5. Run both test files; fix the listed `test_session.py` expectations; green; `just lint`,
   `just type`; commit `fix(local-libvirt): open the session artifact root on first use`.
6. Controlled fault: restore the eager call in the factory; S1 goes red; revert.

## Task 2 — non-System abort prunes empty parents; port-sequence regression

Files: modify `external_boot.py` (`_RealLocalExternalBootOperation.abort_preparation`;
`prune_empty_activation_parents` docstring now names pre-#2926 sessions and interrupted
materialization as the sources); add tests to `lifecycle/boot/test_session_mechanisms.py`.
Interfaces: consumes Task 1's lazy factory; uses existing `RealLocalExternalBootIO(root,
materializer, writer, resolve_operation_lease, session_factory, capacity_bytes)`,
`LocalLibvirtExternalBoot(io)`, `LocalPartialAbortReceiptV1(binding=, plan_identity=,
authority=)`, and `RecoveryMetadataStore.exact_recovery_absence`.

Verification:

- Contract S2 "the authority's port sequence proves absence", `Mode: focused-test`. New
  parametrized `test_non_system_partial_abort_proves_absence_through_authority_reads` over the four
  start states (nothing; canonical `.<system>.<activation>.abort.json` receipt, mode 0600; empty
  mode-0700 complete directory `<system>.<activation>`; empty mode-0700 `<system>/<run>/<activation>`).
  Ports use a real factory with `LocalArtifactRoot(root).open` and a lease resolver returning
  `LocalOperationLease(system_id, binding)` pinned by one `LocalOperationLane`. Drive:
  `abort_preparation` (expect `removed` for the receipt, else `absent`), `recovery_is_absent`,
  `observe_state` (raises: no metadata), `recovery_is_absent`, `recovery_point` (raises),
  `cleanup_receipt` (`None`), `recovery_is_absent`; every `recovery_is_absent` is `True` and
  `root/<system>` does not exist at the end. Red without Task 1 (second proof false); the
  parent-state case is red without the prune.
- Contract S3 "residue keeps absence false", `Mode: focused-test`. Same harness with a `kernel`
  file in the activation directory: abort `absent`, `recovery_is_absent` false, file intact.
- Green for both: `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py tests/providers/local_libvirt/test_external_boot.py`.

Steps:

1. Write S2/S3; run; expect the parent-state case red.
2. In `abort_preparation`, assign the `_abort_preparation(...)` result, and when it is in
   `{"removed", "absent"}` open `RecoveryMetadataStore(self._recovery_root)` and call
   `store.prune_empty_activation_parents(binding)` before returning it.
3. Green; `just lint`, `just type`; commit `fix(local-libvirt): prune activation parents after a
   partial abort`.
4. Controlled faults: drop the prune (parent-state case red); restore Task 1's eager open (every
   case red on the second proof). Revert each.
5. `just records`; run `just test-verbose` over `tests/providers/local_libvirt/`.
