# Torn authority System journal tail Implementation Plan (#3543)

**Goal:** a torn final journal record the database head does not reference is moved aside as
evidence; anything else stays refused.
**Architecture:** the journal gains `abandon_torn_tail` and a typed torn-tail error; `_read` is
split without changing acceptance; `_journal_state` recovers on that error. Spec:
`docs/workflow/specs/2026-11-04-authority-journal-torn-tail-3543-design.md`.
**Tech stack:** Python 3.14, pytest, `just lint`, `just type`.

## Global Constraints

- Reader acceptance, journal bounds, `src/kdive/db/schema/` and deploy templates are untouched.
- Evidence bounds: 4 per System, 256 in `abandoned/`; files 0600, directories 0700,
  `O_NOFOLLOW | O_CLOEXEC`, owner `self._owner_uid`.

Expected implementation size: 200–280 changed lines (M) — journal about 60, service about 20,
tests about 150, ADR amendment about 15.

## File map

- `src/kdive/providers/system_authority/journal.py`: owns the format. Gains the error type,
  `_load`, `_validate`, `_quarantine`, `abandon_torn_tail`. `abandon_unconfirmed_tail` migrates to
  `_quarantine`; its behaviour is unchanged. No obsolete path.
- `src/kdive/providers/system_authority/service.py`: `_journal_state` and `_recover_torn_tail`.
- `tests/providers/system_authority/test_journal.py`, `test_service.py`.
- `docs/adr/0623-authority-owned-system-provisioning.md`: appended amendment.

## Task 1: journal recovery

**Interfaces:** consumes `_validate_directory`, `_check_evidence_bounds`, `_write_scratch`,
`_open_abandoned`, `_identity`, `canonical_system_authority_bytes`,
`authority_system_record_digest`, `GENESIS_DIGEST`, `MAX_MESSAGE_BYTES` (all present in
`journal.py`). Provides `AuthoritySystemJournalTornTailError(ValueError)` and
`FileAuthoritySystemJournal.abandon_torn_tail(confirmed_sequence: int, confirmed_digest: str) ->
None`.

**Verification:**
- Contract: recovery result. Mode: focused-test, `test_journal.py::test_abandon_torn_tail_keeps_prefix_and_evidence`
  (parametrized cut offsets) and `test_abandon_torn_tail_of_the_first_record_leaves_an_empty_journal`;
  red: `AttributeError`; green: `uv run python -m pytest tests/providers/system_authority/test_journal.py -q -k torn`.
- Contract: refusals leave bytes unchanged and write no evidence. Mode: focused-test,
  `test_abandon_torn_tail_refuses_*`, same command; red: `AttributeError`.
- Contract: crash at the active rename, then idempotent retry; bounds; changed after validation.
  Mode: focused-test, `test_abandon_torn_tail_is_idempotent_after_partial_crash`,
  `test_abandon_torn_tail_counts_against_evidence_bounds`,
  `test_abandon_torn_tail_refuses_a_journal_changed_after_validation`; same command.
- Contract: `read`, `head`, `append` still refuse. Mode: focused-test,
  `test_torn_journal_still_refuses_read_head_and_append`; passes before and after; its red is a
  controlled fault (return early from the torn check).
- Contract: #3531 unchanged after the helper move. Mode: focused-test, existing cases,
  `uv run python -m pytest tests/providers/system_authority/test_journal.py -q -k abandon`.

Steps: write the tests (red), implement, run (green), commit.

```text
class AuthoritySystemJournalTornTailError(ValueError): ...

_load() -> tuple[bytes, os.stat_result] | None      # the open/read/size body of _read
_validate(payload: bytes) -> tuple[Record, ...]     # the splitlines/count/chain body of _read
_read(): load; None -> ((), None); no final newline -> raise the error; return validate, status

_quarantine(prefix, evidence_name, evidence, validated):  # the try block of abandon_unconfirmed_tail

abandon_torn_tail(confirmed_sequence, confirmed_digest):
    loaded = self._load()
    if loaded is None: raise ValueError("authority System journal has no torn final record")
    payload, validated = loaded
    end = payload.rfind(b"\n") + 1
    complete, torn = payload[:end], payload[end:]
    if not torn: same ValueError
    if len(torn) > MAX_MESSAGE_BYTES: raise ValueError("... torn final record is oversized")
    records = self._validate(complete)
    digest = authority_system_record_digest(records[-1]) if records else GENESIS_DIGEST
    if (len(records), digest) != (confirmed_sequence, confirmed_digest):
        raise ValueError("authority System journal torn final record is not past the head")
    name = f"{self._system_id}.{confirmed_sequence + 1}.torn.{sha256(torn).hexdigest()}.jsonl"
    self._quarantine(records, name, torn, validated)
```

## Task 2: service recovery

**Interfaces:** consumes `abandon_torn_tail`, `AuthoritySystemJournalTornTailError`. Provides no
new public surface.

**Verification:**
- Contract: same-request retry and teardown after a torn tail. Mode: focused-test,
  `test_service.py::test_retry_recovers_a_torn_final_record`,
  `test_teardown_recovers_a_torn_final_record`; red: `ValueError` incomplete final record; green:
  `uv run python -m pytest tests/providers/system_authority/test_service.py -q -k torn`.
- Contract: head-referenced torn tail refused as `journal-conflict`, bytes unchanged. Mode:
  focused-test, `test_torn_record_the_head_references_is_refused`; same command.
- Contract: recovery is what the service path depends on. Mode: focused-test with a
  controlled fault: remove the `except` in `_journal_state`, observe the retry test red, restore.

Steps: write the tests (red), implement, run (green), commit. Tests tear the journal by truncating
the file after `_crash_provision_at`, which leaves one whole record past the head, so the
truncation removes at least the trailing newline of that record.

```text
try:
    records = list(journal.read())
except AuthoritySystemJournalTornTailError:
    AuthoritySystemService._recover_torn_tail(resolved, journal)
    records = list(journal.read())
```

`_recover_torn_tail` mirrors `_abandon_unconfirmed`: `except ValueError, OSError` logs with
`_log.exception` and raises `AuthoritySystemServiceError("journal-conflict") from None`; success
logs a warning naming the System and the confirmed sequence. Neither logs torn bytes.

## Task 3: ADR-0623 amendment

**Verification:** Mode: task-test-not-applicable, prose with no executable consumer; run
`just records` after `git fetch origin main` for its shape.

Append `### Amendment (2026-11-04): a torn final record is quarantined (#3543)` after the #3531
amendment: what changed (evidence may be torn bytes, `.torn.` name, same bounds and rename), and
what did not (the reader, the bounds, the refusal when the head references the tail).
