# Plan: remote module restore crash window (#3427)

Spec: [2026-10-30-restore-crash-window-3427-design.md](../specs/2026-10-30-restore-crash-window-3427-design.md).
Branch `fix/restore-crash-window-3427`, base `main`.

Expected implementation size: 150–230 changed lines (tests ~120–170 in one file, source ~20–40 across two files, ADR amendment ~15).

## Task 1: failing tests

File: `tests/providers/remote_libvirt/test_remote_module_authority_host.py`.

- Add a `_Quiesce.crash_after_host_restore()` helper: set the scratch result to RESTORED (as
  `restore()` does), stage the release RECOVER restore request in the store, and await
  `host.execute_lifecycle(request, terminal, None, deadline)` directly, publishing nothing.
- Test 1 `test_system_teardown_after_a_crash_before_the_restore_evidence_reaps_the_restored_result`.
- Test 2 `test_restore_retry_after_a_crash_before_its_evidence_publishes_it`: retry `restore()`,
  then `reap("cleanup")` succeeds.
- Test 3 `test_crash_between_the_restore_evidence_writes_is_recovered_by_a_retry`: wrap the
  store's `_publish` so the second write inside `publish_lifecycle_completion` raises once, then
  retry `restore()` and `reap("cleanup")`.
- Test 4 `test_system_teardown_refuses_an_unfinished_restore_in_the_scratch_volume`, parametrized
  over a `restore-ready` success and a `restored` failure result.

Verify: tests 1 and 3 fail with the spec's named errors; 2 and 4 pass.

## Task 2: write order

File: `src/kdive/providers/remote_libvirt/external_boot_authority.py`,
`publish_lifecycle_completion`: publish `.restored` before the completion for a restore response.
Verify: test 3 passes.

## Task 3: reap recognises a finished restore

File: `src/kdive/providers/remote_libvirt/remote_module_authority_host.py`, `_reap`: in the
`state == "absent"` check, when `restored is None` and the scratch result differs from the
installed one, accept a finished restore of this attempt and continue with the restore operation
and that result. Verify: tests 1-4 pass; the existing `differs before reap` tests still pass.

## Task 4: ADR-0620 amendment

Append `## Amendment (2026-10-30): A restore's crash window does not strand teardown (#3427)`.

## Task 5: controlled faults and guardrails

Commit, then revert each rule in turn and observe the matching test red (the `_reap` rule: test
1; the write order: test 3), and make `execute_lifecycle` refuse a restore whose request is already
staged with no completion (test 2 red); `git checkout --` the source file after each. Run `just lint`, `just type`, `just test-changed`, `just records`.
