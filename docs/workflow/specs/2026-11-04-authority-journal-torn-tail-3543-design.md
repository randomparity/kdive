# Recover a torn final authority System journal record (#3543)

## Problem

`FileAuthoritySystemJournal.append` writes a record with an `os.write` loop. A crash or write
failure inside that loop leaves a final line with no trailing newline. `_read` then raises
`incomplete final record`, so every later request for the System fails: `_journal_state`
(`service.py`) reads the journal first, before the #3531 quarantine in `_anchor` is reachable, and
that quarantine would inherit the same refusal. The System needs manual repair, although the
torn bytes were never anchored: `_anchor` calls `advance_head` only after `append` returns.

The issue names `_abandon_unconfirmed` as the recovery caller. It cannot be: no parsed record
exists to reach it with, because the failure happens earlier, in `_journal_state`.

## Decision

Recovery is a second journal operation beside `abandon_unconfirmed_tail`, entered from
`_journal_state`. `journal.read()`, `append` and `head` keep refusing a torn tail, with the same
message, so the reader's acceptance is unchanged (an approved exclusion).

- `_read` raises `AuthoritySystemJournalTornTailError(ValueError)` for the existing condition.
  `_read` is split into `_load` (bytes and status) and `_validate` (the chain walk) without any
  change in what it accepts.
- `_journal_state` catches that one type, calls `journal.abandon_torn_tail(resolved.head.sequence,
  resolved.head.digest)`, and reads again. The happy path costs nothing.
- `abandon_torn_tail(confirmed_sequence, confirmed_digest)`:
  splits the payload after its last newline into complete lines and torn bytes; requires non-empty
  torn bytes of at most `MAX_MESSAGE_BYTES` (a real line cannot be longer); validates the complete
  lines with `_validate`; requires their count and head digest (`GENESIS_DIGEST` when empty) to
  equal the arguments. Anything else raises `ValueError` before any write.
- Then it runs the #3531 sequence, moved unchanged into one private helper that both operations
  call: evidence to scratch, rename to its final name, prefix to scratch, identity re-check,
  atomic rename over the active file, fsync both directories.
- Evidence is the torn bytes verbatim, no newline added, named
  `<system_id>.<sequence>.torn.<sha256-hex>.jsonl`, where `<sequence>` is `confirmed_sequence + 1`.
  The `torn` token follows the #2983 precedent and tells an operator the file is not a record;
  the name stays inside the existing per-System and directory bounds.
- `ValueError` or `OSError` from the recovery becomes `journal-conflict` with a logged exception,
  as `_abandon_unconfirmed` does. Journal bytes stay as they were.

A torn record is dropped whatever attempt wrote it, including the current one. Unlike a whole
unconfirmed record, it cannot be compared with the record the request computes. The next request
writes its own record from database state.

ADR-0623 gets an appended amendment: its #3531 text calls evidence "one canonical record line per
file", which stops being true. No new ADR.

Rejected:

- Tolerating the torn tail in `_read`: excluded, and it would let `append` bury the tail mid-file.
- Calling recovery before every read: a second file read on each request for a rare path.
- Extending `abandon_unconfirmed_tail`: its contract is one validated record past the head; the
  torn path has no record, so the two preconditions would share only the rename sequence.
- Truncating in place: forbidden by the #3531 amendment (no journal byte edited in place).

## Scope

- `journal.py`, `service.py`; tests in `tests/providers/system_authority/`; the ADR-0623 amendment.
- Out: power-loss atomic append, other journals, the reader's acceptance, SQL, deploy templates,
  journal phases or fields.

## Failure model

- **Actors and deployments**
  - The authority daemon, one process per host (held by the socket lock), one lane lock per
    System, `append` and recovery both synchronous under it. A same-uid operator.
  - Local-libvirt and remote-libvirt authority hosts.
- **Invariants and assets at stake**
  - A record the database head references is never moved aside.
  - Torn bytes are durable under their final name before the active file changes.
  - The active journal stays valid or torn-as-found at each crash point; never a third state.
  - Refusal leaves the journal unchanged; a refused recovery may leave an evidence file only if
    the identity re-check fails after evidence is written, as in #3531.
  - Evidence stays inside the #3531 bounds (4 per System, 256 total, at most 1 MiB each).
- **Accepted failure classes**
  - A fifth quarantine for a System, whole or torn, fails closed with `journal-conflict`, as #3531
    accepted; the operator clears `abandoned/<system_id>.*.jsonl`.
  - A complete record past the head with a torn line after it is refused; `_anchor` never appends
    past an unconfirmed record, so it is corruption.
  - A torn tail of the current attempt cannot be compared with the recomputed record, so the
    changed-recovery-observation refusal does not fire for it. Bounded: no torn record reached the
    database, and provider effects are re-observed from the confirmed `mutation-started`.
  - The active-file identity check and rename are not atomic with each other, as in #3531; only a
    same-uid actor races it. A second writer that does race it fails `append`'s inode re-check.
- **Covered elsewhere**
  - Power-loss atomic append, including tails that end in garbage past a newline: operator.
  - Journal bounds and teardown headroom: #3526. Retry bound: #3530.

## Threat model

- Boundary: none added or widened. The torn bytes come from the daemon's own journal, are not
  parsed, and reach only an evidence file. The evidence path is built from a UUID, an integer and
  a hex digest, never from file content.
- Actors: a same-uid local actor, already able to rewrite the journal.
- Controls: reused from #3531 (0700/owner/`O_NOFOLLOW` directory, 0600 `O_EXCL` scratch, bounds).
  New: the `MAX_MESSAGE_BYTES` torn-size bound.
- Out of scope: a same-uid actor forging a head-matching prefix, as in #3531.

## Success

- After a final record torn at a named offset (first byte, middle, all but the newline), with the
  database head on the complete prefix, a retry of the same request and a teardown of another
  generation each reach a terminal receipt, and the torn bytes are in `abandoned/` verbatim.
- A first record torn on an empty prefix at head 0 recovers to an empty journal.
- Refused, journal bytes unchanged, no recovery: head above the prefix (it references the tail),
  head below it, digest mismatch, a corrupt complete line before the tail, an oversize tail.
- Each refusal above reaches the caller as `journal-conflict`.
- `read`, `head` and `append` still raise `incomplete final record` on a torn journal.
- A crash before the active rename leaves a torn journal that a retry recovers without a second
  evidence file.

## Validation

- Recovery result, offsets, empty prefix, evidence bytes and mode: focused-test,
  `test_journal.py::test_abandon_torn_tail_*`.
- Refusals, evidence bounds, changed-after-validation, crash retry: focused-test, same file.
- Reader unchanged: focused-test, existing `test_noncanonical_or_partial_record_*` plus a new
  `read`/`head`/`append` case.
- Service recovery and refusal through the real service and file journal: focused-test,
  `test_service.py::test_*_torn_*`.
- #3531 behaviour after the helper move: focused-test, the existing `test_abandon_*` cases.
- ADR-0623 amendment: task-test-not-applicable, prose with no executable consumer; `just records`
  checks only the record's shape.
- Fault: removing the `_journal_state` recovery call turns the service test red: controlled-fault.
- Live proof: task-test-not-applicable; the tests run the real service and file journal, and no
  host-specific path changes.
