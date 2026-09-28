# Journal tail retraction — implementation plan (#2793)

Goal: remove exactly one unanchored final journal record after a definitive head-advance refusal
and at authority startup, preserving its bytes, per
[the spec](../specs/2026-09-28-journal-tail-retraction-design.md) and the ADR-0584 amendment.

Architecture: `FileAuthorityJournal.retract` owns the file operation and the evidence write.
`ExternalBootAuthorityService._anchor` calls it on a definitive refusal. A startup-only host step
decides eligibility under the request-socket flock and the lane's advisory lock, then calls the
same `retract`.

Tech stack: Python 3.14, psycopg 3 async, pytest; Postgres via testcontainers for `tests/db/`.

Expected implementation size: 300–400 changed lines (M) — three source files (~150 lines) plus
four test files (~200 lines), from the task list below.

## Global Constraints

- No migration, no schema change, no new ADR, no new dependency. Do not touch `_execute_teardown`,
  `_recovery_observation`, or `src/kdive/db/schema/`.
- Ruff line length 100; `ty` strict; plain prose (no "critical/robust/comprehensive/elegant").
- Guardrails: `just lint`, `just type`, `just records` (after `git fetch origin main`), focused
  `just test-verbose <paths>`; pre-push runs `just ci > <file> 2>&1 < /dev/null` in the foreground.
- Log only the System id, sequence, digest, and the exception type — never record contents.

## Task 1 — `FileAuthorityJournal.retract`

Files: `src/kdive/providers/external_boot_authority/journal.py`;
test `tests/providers/external_boot_authority/test_journal.py`.

Interfaces (produced): `RETRACTED_DIRECTORY = "retracted"` and
`FileAuthorityJournal.retract(self, record: JournalRecordV1) -> None`. The evidence directory is
the fixed child of the lane's own validated parent descriptor, so the constructor is unchanged.
`retract` raises `ValueError` when `record` is not the cached final record or the file changed
since validation, `PermissionError`/`OSError` when the evidence directory is a symlink, not a
directory, foreign-owned, or not mode 0700, and `OSError` on any other filesystem failure.

Verification:
- Mode: focused-test — contract: retract truncates to the previous record (or unlinks the only
  record), writes `<system_id>.<sequence>.<hex>.jsonl` with the exact line bytes (mode 0600, dir
  0700), and leaves a journal whose next `append` of the same sequence succeeds. Tests:
  `test_retract_removes_the_final_record_and_preserves_its_bytes`,
  `test_retract_of_the_only_record_unlinks_the_lane`,
  `test_retract_accepts_identical_existing_evidence`,
  `test_retract_refuses_a_record_that_is_not_the_tail`,
  `test_retract_refuses_a_changed_file`, `test_retract_refuses_an_unsafe_evidence_directory`
  (parametrized: symlink, regular file, mode 0755).
  Red: `AttributeError: 'FileAuthorityJournal' object has no attribute 'retract'`.
  Green: `just test-verbose tests/providers/external_boot_authority/test_journal.py`.

Steps:
1. Write the tests with the journal fixtures already in `test_journal.py` (records built through
   the file's existing helpers). Run: expect the red failure above.
2. Implement. `_preserve(record, encoded)` runs `os.mkdir(RETRACTED_DIRECTORY, 0o700,
   dir_fd=self._parent_fd)`, treating `FileExistsError` as success. It opens the child with
   `O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC` and requires a directory owned by the service
   identity with exact mode 0700, then creates `f"{record.system_id}.{record.sequence}."
   f"{record_digest(record).removeprefix('sha256:')}.jsonl"` with
   `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode 0600. It writes the full bytes, fsyncs
   the file and the directory, and on `FileExistsError` requires the existing regular 0600 file
   to hold identical bytes. `retract` loads when the cache is empty, requires
   `cache.records[-1] == record` and `cache.tail_bytes == encoded`, and calls `_preserve`. It then
   opens the lane with `O_RDWR|_OPEN_BASE`, validates the descriptor, and requires the fstat
   identity to equal `cache.identity`, the `pread` tail to equal `encoded`, and the path's
   dev/inode to match the descriptor (the same checks `append` makes). When
   `cache.tail_offset == 0` it calls `os.unlink(self._name, dir_fd=self._parent_fd)`, otherwise
   `os.ftruncate` to `tail_offset`, then fsyncs the file (or parent), closes, and calls
   `self.load()` to rebuild the cache.
3. Green command above; `just lint`; `just type`. Commit `fix(authority): retract an unanchored
   journal tail with preserved evidence`.

## Task 2 — `_anchor` retracts on a definitive refusal

Files: `src/kdive/providers/external_boot_authority/service.py` (`_anchor` only); test
`tests/providers/external_boot_authority/test_service.py`, using the `service_support.py`
harness.

Interfaces: consumes `FileAuthorityJournal.retract` from Task 1.

Verification:
- Mode: focused-test — contract: a `superseded` or `conflict` advance status leaves the lane
  file byte-identical to its pre-anchor content, preserves the refused record, and still raises
  the same `AuthorityServiceError` category. An `advance` exception keeps the record. After a
  `superseded` refusal, a following anchor on the same lane advances. Tests:
  `test_refused_anchor_retracts_its_record` (parametrized over both statuses),
  `test_anchor_error_keeps_the_unanchored_record`,
  `test_lane_anchors_again_after_a_superseded_refusal`,
  `test_retraction_failure_still_raises_the_refusal`. Red: the file still holds the refused
  record.
  Green: `just test-verbose tests/providers/external_boot_authority/test_service.py`.

Steps:
1. Write the tests (repository fake returning the chosen status from `advance`). Expect red.
2. In `_anchor`, replace the refusal block with:

   ```python
   if status != "advanced":
       try:
           journal.retract(record)
       except (OSError, ValueError) as error:
           self._logger.warning(
               "authority journal retraction failed: %s", type(error).__qualname__,
               extra={"system_id": str(record.system_id), "sequence": record.sequence},
           )
       else:
           self._logger.warning(
               "authority journal retracted a refused record",
               extra={"system_id": str(record.system_id), "sequence": record.sequence,
                      "digest": record_digest(record)},
           )
       raise self._reject(...)  # unchanged
   ```

3. Green; lint; type. Commit
   `fix(authority): retract a record whose head advance was refused`.

## Task 3 — startup reconcile

Files: `src/kdive/providers/external_boot_authority/host.py`; tests
`tests/providers/external_boot_authority/test_host.py` and
`tests/db/test_external_boot_authority_head_inventory_migration.py`.

Interfaces (produced, private): `_head_matches(record, head, config) -> bool`, the predicate
extracted from `_restore_journal_inventory` and reused there. `_retract_unanchored_tail(config,
system_id: str, head: JournalHead | None) -> bool` runs synchronously in a thread and returns
whether it retracted. `_lock_journal_lane(connection, system_id: str) -> None` runs
`SELECT pg_advisory_xact_lock(hashtextextended('kdive:system:' || %s, 2126))`.
`async _reconcile_journal_tails(config) -> None` is called from `_check_static_authority_host`
only when its new keyword `reconcile_tails=True` is set, after `validate_credential_paths` and
before `_database_heads`. Only `run_authority_host`'s startup call passes it.

Verification:
- Mode: focused-test — contract: eligibility. Tests: chained tail retracted; lone first record
  with no head retracted; head at tail sequence kept; two-record suffix kept; record at
  `head.sequence` differing from the head kept; foreign instance kept; already-equal lane
  untouched; every kept case then fails `restore_journal_inventory` exactly as before.
  Red: `AttributeError: module ... has no attribute '_retract_unanchored_tail'`.
- Mode: focused-test — contract: `_reconcile_journal_tails` raises
  `HostReadinessError("journal", "reconcile-busy")` when the request-socket lock is held
  (`acquire_socket_lock` on the same path in the test first), and only startup passes
  `reconcile_tails=True` (monkeypatch `_reconcile_journal_tails` to record calls; run
  `check_authority_host_once` and the periodic `check_authority_host` and assert none).
- Mode: focused-test — contract: the authority login role can run the lane lock, and it waits
  while an admin session holds the same key, failing with `LockNotAvailable` under
  `lock_timeout`. Test `test_authority_role_takes_the_lane_advisory_lock` in the 0125 DB test file.
  Green: `just test-verbose tests/providers/external_boot_authority/test_host.py
  tests/db/test_external_boot_authority_head_inventory_migration.py`.

Steps:
1. Tests first; expect red.
2. Implement in `host.py`. `_reconcile_journal_tails` acquires
   `acquire_socket_lock(config.request_socket.with_suffix(".lock"), config.authority_uid)`
   (`SocketLockBusyError` → `reconcile-busy`, other `OSError` → `unsafe-path`) and closes it in
   `finally`. It opens `_database_connection(config)`, runs `check_database_role`, lists the heads,
   and gets the local lanes via `_validate_journal_root` and `_local_lanes`. For each local lane
   whose cached comparison differs (no head, or the file's last record fails `_head_matches`), it
   runs `async with connection.transaction()`: `_lock_journal_lane`, re-list the heads, then
   `await asyncio.to_thread(_retract_unanchored_tail, config, system_id, head)`. A database
   exception maps to `HostReadinessError("journal", "reconcile-failed")`. A retraction logs a
   warning with the System, sequence, and digest.
   `_retract_unanchored_tail` opens `FileAuthorityJournal(config.journal_dir, f"{id}.jsonl",
   owner_uid=...)` and loads it. It
   retracts `records[-1]` only when the head is `None`, `len(records) == 1`, and the record's
   instance and System match, or when `len(records) == head.sequence + 1` and
   `_head_matches(records[-2], head, config)`. It closes the journal in `finally`.
   `OSError`/`ValueError` → `HostReadinessError("journal", "invalid-lane")`.
3. Pass `reconcile_tails=True` from `run_authority_host`'s first `_check_static_authority_host`.
4. Green; lint; type. Commit `fix(authority): retract one unanchored journal tail at startup`.

## Task 4 — the evidence directory in the lane inventory

Files: `src/kdive/providers/external_boot_authority/host.py` (`_local_lanes`); test
`tests/providers/external_boot_authority/test_host.py`.

Verification:
- Mode: focused-test — contract: `retracted` is accepted only as a real directory owned by the
  authority with mode 0700; a symlink named `retracted`, a regular file named `retracted`, a
  0755 or foreign-owned `retracted`, and any other directory fail `journal: unsafe-tree`; a
  `<uuid>.jsonl` inside `retracted/` is not a lane (an empty head set passes). Red: `journal:
  unsafe-tree` for the valid directory.
  Green: `just test-verbose tests/providers/external_boot_authority/test_host.py`.

Steps: in `_local_lanes`, before the per-name loop, handle `RETRACTED_DIRECTORY`: `os.stat(...,
dir_fd=root_fd, follow_symlinks=False)` and require `S_ISDIR`, the authority uid, and mode 0700,
else `unsafe-tree`; drop it from the names and apply the lane limit to the remaining names.
Commit `fix(authority): admit the reserved retraction directory in the lane inventory`.

## Task 5 — host-level proof against Postgres

Files: `tests/db/test_connected_authority_acceptance.py`.

Verification:
- Mode: focused-test — contract: a refused anchor produced by the real advance function, with
  the runtime retraction forced to fail, leaves a one-record tail; `run_authority_host` over that
  journal then reaches `READY=1`, the lane ends at the head, and `retracted/` holds the exact
  bytes. Only `_validate_access_boundary` and `_check_provider_socket` are stubbed. Red (source
  changes stashed): `journal: head-mismatch` from startup.
  Green: `just test-verbose tests/db/test_connected_authority_acceptance.py`.

## Task 6 — records and final gate

`git fetch origin main && just records`; the ADR amendment and spec are already committed.
Run `just ci > <scratch>/ci.log 2>&1 < /dev/null` in the foreground before push. Expected: exit 0.
