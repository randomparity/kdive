# Unconfirmed authority journal record recovery Implementation Plan (#3531)

**Goal:** a crash between `journal.append` and `advance_head` no longer blocks teardown.
**Architecture:** the journal gains `abandon_unconfirmed_tail`; `_anchor` calls it on a
mismatching file-ahead record; `_JournalState` keeps the file-ahead record apart from the
confirmed prefix. Spec:
`docs/workflow/specs/2026-11-04-authority-journal-unconfirmed-record-3531-design.md`.
**Tech stack:** Python 3.12, pytest (`just test-verbose <path>`), `just lint`, `just type`.

## Global Constraints

- Reader validation, journal bounds and `src/kdive/db/schema/` are untouched.
- Files 0600, directories 0700, `O_NOFOLLOW | O_CLOEXEC`, owner `self._owner_uid`.
- Evidence bounds: 4 per System, 256 in `abandoned/`; evidence records are at most 1 MiB.

Expected implementation size: 200–300 changed lines (M) — journal method about 110, service
about 25, tests about 150.

## File map

- `src/kdive/providers/system_authority/journal.py`: owns the file format and its safety; gains
  the quarantine method, constants and the inventory skip.
- `src/kdive/providers/system_authority/service.py`: `_anchor` calls it; `_begin`, `_quiescence`,
  `_ensure_mutation_started` read the confirmed prefix. No caller migration, no obsolete path.
- Tests: `tests/providers/system_authority/test_journal.py`, `test_service.py`.

## Task 1: journal quarantine

**Interfaces:** consumes `FileAuthoritySystemJournal._read`, `_validate_descriptor`,
`_identity`, `_validate_directory`, `canonical_system_authority_bytes`,
`authority_system_record_digest`. Provides
`FileAuthoritySystemJournal.abandon_unconfirmed_tail(confirmed_sequence: int) -> None` and constants `ABANDONED_DIRECTORY = "abandoned"`,
`MAX_ABANDONED_PER_SYSTEM = 4`, `MAX_ABANDONED_FILES = 256`.

**Verification:**
- Contract: quarantine result and reader acceptance. Mode: focused-test,
  `test_journal.py::test_abandon_unconfirmed_tail_keeps_prefix_and_evidence`; red: no attribute;
  green: `just test-verbose tests/providers/system_authority/test_journal.py -k abandon`.
- Contract: refused preconditions (extra records, wrong digest, evidence bounds, foreign mode,
  symlinked directory). Mode: focused-test, `test_abandon_refuses_*`, same command.
- Contract: crash points. Mode: focused-test, `test_abandon_is_idempotent_after_partial_crash`
  (short stale scratch file; evidence already present), same command.
- Contract: inventory skip. Mode: focused-test, `test_inventory_ignores_abandoned_directory`.

Steps: write the tests (red), implement, run the command (green), commit.

Implementation (method body; helpers `_open_abandoned`, `_write_scratch`, `_check_evidence_bounds`):

```text
def abandon_unconfirmed_tail(self, confirmed_sequence: int) -> None:
    records, validated = self._read()
    if validated is None or len(records) != confirmed_sequence + 1:
        raise ValueError("authority System journal has no single unconfirmed record")
    prefix, tail = records[:confirmed_sequence], records[confirmed_sequence]
    digest = authority_system_record_digest(tail)
    evidence = canonical_system_authority_bytes(tail) + b"\n"
    name = f"{self._system_id}.{tail.sequence}.{digest.removeprefix('sha256:')}.jsonl"
    scratch = f"{self._system_id}.pending"
    directory = self._open_abandoned()
    try:
        self._check_evidence_bounds(directory, name)
        self._write_scratch(directory, scratch, evidence)
        os.rename(scratch, name, src_dir_fd=directory, dst_dir_fd=directory)
        self._write_scratch(directory, scratch, b"".join(
            canonical_system_authority_bytes(r) + b"\n" for r in prefix))
        current = os.stat(self._name, dir_fd=self._journal_fd, follow_symlinks=False)
        if self._identity(current) != self._identity(validated):
            raise ValueError("authority System journal changed since validation")
        os.rename(scratch, self._name, src_dir_fd=directory, dst_dir_fd=self._journal_fd)
        os.fsync(self._journal_fd)
        os.fsync(directory)
    finally:
        os.close(directory)
```

`_write_scratch` unlinks a stale scratch file, opens `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW |
O_CLOEXEC` mode 0600, writes in a loop, and `fsync`s the file and the directory. The evidence
rename replaces an identical earlier copy atomically, so the evidence name never holds a short
file. `_open_abandoned` runs `os.mkdir` (ignore `FileExistsError`; `fchmod` 0700 only when
created), opens `O_DIRECTORY | O_NOFOLLOW`, and `_validate_directory`.
`_check_evidence_bounds` counts `<system_id>.*.jsonl` (at most 4, the evidence name being
checked excluded) and all `*.jsonl` (at most 256). The inventory skips the name `abandoned`
after the same open-and-validate.

## Task 2: service wiring

**Interfaces:** consumes Task 1's method; `_JournalState.records/db_sequence/db_digest`.

**Verification:**
- Contract: teardown after crashed provision. Mode: focused-test,
  `test_service.py::test_teardown_succeeds_after_unconfirmed_provision_record`; red:
  `journal-conflict`; green: `just test-verbose tests/providers/system_authority/test_service.py -k unconfirmed`.
- Contract: same-request retry adopts. Mode: focused-test, `test_retry_adopts_unconfirmed_record`
  for crashed `watermark-installed` and `mutation-started`, same command; and
  `test_teardown_after_unconfirmed_provider_returned`.
- Contract: prefix divergence still refused. Mode: focused-test,
  `test_confirmed_prefix_divergence_is_still_refused`, same command.
- Contract: refused quarantine maps to `journal-conflict` with a log line. Mode: focused-test,
  `test_quarantine_bound_refuses_with_journal_conflict`, same command.

Edits: `_JournalState` gains `unconfirmed: AuthoritySystemJournalRecordV1 | None`;
`_journal_state` puts `records[:head.sequence]` in `records` and the one extra record there. In
`_anchor`, `unconfirmed is None` appends as now; an equal `unconfirmed` is adopted (appended to
`records`, cleared); otherwise `_abandon_unconfirmed` runs, then the computed record is appended.
`_abandon_unconfirmed` calls `journal.abandon_unconfirmed_tail(state.db_sequence)`, clears
`unconfirmed`, logs a warning with the System id and sequence, and maps the `ValueError` of the
bound or precondition checks to `AuthoritySystemServiceError("journal-conflict")` (logged).
`_ensure_mutation_started` skips the `admitted` anchor when the last own record is `admitted`.

Crash simulation in tests: wrap the repository so `advance_head` raises once after the file
append, then run the next request.

Final: `just lint`, `just type`, `prek run`, focused tests, `just records`.
