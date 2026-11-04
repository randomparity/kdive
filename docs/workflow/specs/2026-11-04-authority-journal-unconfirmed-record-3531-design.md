# Recover from an unconfirmed authority journal record (#3531)

## Problem

`AuthoritySystemService._anchor` appends a record to the System's file journal and only then
asks the database to confirm it (`advance_head`). A crash between the two leaves one record in
the file past the database head. A later request adopts it only if it computes the identical
record; a different one (a `systems.teardown` after a crashed provision) raises
`journal-conflict`, and the System cannot be torn down.

No journal phase can express the recovery. The database function
`advance_authority_system_journal_head` (0149) accepts only `sequence = head + 1` and
`previous_digest = head digest` from the calling attempt, and the reader requires
`sequence = line number` and `previous_digest = prior line digest`. The first record of the new
attempt therefore needs the very slot the unconfirmed record holds. The schema is excluded
(#3530), so the unconfirmed record must leave the active chain.

## Decision

Quarantine by rename (operator-approved 2026-11-04). When the file holds exactly one valid record
past the confirmed head and the next record differs from it, the journal copies that record to
an evidence file, builds the confirmed prefix in a scratch file, and atomically renames it over
the active journal. The database is the authority: an unconfirmed record had no effect, because
the provider call always follows confirmation. Journal bytes are never edited in place and the
reader is unchanged. ADR-0623 gets an appended amendment (no new ADR; no new phase or field).

Rejected: a new journal phase or record field (cannot satisfy the head function); a migration
(excluded); truncating the file in place (forbidden); storing a full copy of the original file
as evidence (up to 16 MiB each; the prefix is byte-identical in the active journal, so prefix
plus the evidence record is the original).

## Scope

- `journal.py`: `FileAuthoritySystemJournal.abandon_unconfirmed_tail(confirmed_sequence,
  confirmed_digest)`; `inventory_authority_system_journals` skips the `abandoned` directory.
- Layout: a private `abandoned/` directory (0700, owner-checked, `O_NOFOLLOW`) inside
  `system-operations/`, because the authority unit's `ReadWritePaths` covers only that directory.
  Evidence file `<system_id>.<sequence>.<sha256-hex>.jsonl` holds the one canonical record line
  (0600, `O_EXCL`); scratch file `<system_id>.pending` holds the prefix.
- Order: write evidence, fsync file and directory; write scratch, fsync; re-check the active
  file is unchanged since validation; `rename(scratch, active)`; fsync both directories. Every
  crash point leaves a valid journal: before the rename the active file is untouched and a retry
  reuses identical evidence (same bytes) and replaces a stale scratch file; after it, the new
  journal is complete.
- Bounds: 4 evidence files per System and 256 in the directory (at most 256 MiB, records are at
  most 1 MiB). Past either, the call raises `ValueError` and the service reports
  `journal-conflict` and logs it, as with the #3526 headroom refusal.
- Preconditions, else `ValueError`: the journal validates under the unchanged reader, holds
  exactly `confirmed_sequence + 1` records, and record `confirmed_sequence` has
  `confirmed_digest` (genesis when 0). A divergence inside the confirmed prefix is still
  refused earlier by `_journal_state`.
- `service.py` `_anchor`: on a mismatching file-ahead record, call the journal method, drop the
  tail from `state.records`, then append the computed record. Identical record: adopted as now.
- `service.py` readers of the file tail use the confirmed prefix instead
  (`state.records[:state.db_sequence]`) in `_begin`, `_quiescence` and
  `_ensure_mutation_started`. Without this, a retry would mistake an unconfirmed record for
  confirmed progress, and a quarantine could record a terminal outcome without the provider
  call. `_ensure_mutation_started` also skips the `admitted` anchor when `admitted` is already
  confirmed (an existing gap on the same crash path).
- Out: SQL, requeue bound (#3530), journal bounds, reader validation, deploy templates.

### Failure model

- Actors: the authority daemon (one process per host, one lane lock per System) and a
  same-uid operator; deployments: local-libvirt and remote-libvirt authority hosts.
- Invariants: the database head and the confirmed prefix agree with the active journal; no
  journal byte is lost; the reader accepts every state a crash can leave; disk use is bounded.
- Accepted: a System with 4 quarantines refuses a fifth (fail closed; each needs a crash in a
  one-record window); evidence files are never garbage collected (operator reclaims them);
  a same-uid actor can already rewrite the journal and is not a new threat.
- Covered elsewhere: journal size bounds and teardown headroom (#3526); retry bound (#3530).

## Success

- After a crash between `append` and `advance_head` in a provision attempt, a teardown
  completes to a terminal receipt.
- A retry of the same request after that crash adopts the identical record; no quarantine.
- A journal diverging inside the confirmed prefix, or holding more than one record past the
  head, is refused; evidence-directory bounds and ownership failures refuse closed.
- The reader accepts the journal after quarantine, after a crash at each step, and the
  inventory still lists it.

## Validation

- Teardown after a crashed provision, real service and file journal: focused-test,
  `tests/providers/system_authority/test_service.py`.
- Same-request retry adopts: focused-test, same file.
- Prefix divergence still refused: focused-test, same file.
- Quarantine result, evidence content, reader acceptance, idempotent retry after a crash at each
  step, bounds, mode and owner and symlink refusal, inventory skip: focused-test,
  `tests/providers/system_authority/test_journal.py`.
- Confirmed-prefix readers (`_begin`, `_quiescence`, `_ensure_mutation_started`): focused-test,
  `test_service.py` (retry after crashed `watermark-installed`, `mutation-started`).
- ADR-0623 amendment: task-test-not-applicable, prose; `just records` checks its form.
- Fault: reverting the quarantine call in `_anchor` turns the teardown test red: controlled-fault.
- Live proof: task-test-not-applicable; the tests run the real service and file journal.
