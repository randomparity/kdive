# Startup recovers a torn authority journal tail (#2983) — design

Decision record: the dated #2983 amendment to
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md).

## Problem

`FileAuthorityJournal.append` writes one record with an `os.write` loop on an `O_APPEND`
descriptor, then `fsync`s. A crash, power loss, or a write error (`ENOSPC`) part-way through
leaves a final line with no newline. `load` refuses that lane ("partial final record"), the
static check reports `journal: invalid-lane`, and startup does not reconcile on that reason, so
the host stays out of service until an operator edits the lane by hand. The torn record was
never anchored: `_anchor_record` (`service.py`) appends before it advances the head.

## Design

Recovery lives in the existing startup reconcile step (`host._reconcile_journal_tails`), which
already holds the request-socket lock and, for the removal, the System's advisory lock.

1. **Journal recovery load.** `journal.py` adds a frozen dataclass `TornTail(offset: int,
   data: bytes)` and `FileAuthorityJournal.load_recovering(*, deadline=None) ->
   tuple[tuple[JournalRecordV1, ...], TornTail | None]`. It validates exactly as `load` does,
   with one difference: a final line without a newline, whose read ended at the size observed
   by `fstat` and whose length is at most `MAX_MESSAGE_BYTES`, is returned as `TornTail`
   instead of raising, and so is an existing zero-byte lane (`TornTail(0, b"")`: a first
   append that failed after creating the file). Every other defect raises `ValueError` as
   today: a longer unterminated line (oversized), an empty, non-canonical, or invalid complete
   line anywhere, a broken chain. `load` keeps its strict behavior and message, including `()`
   for a zero-byte lane. After a recovery load that found a torn tail the journal holds no
   append cache, so `append` and `retract` reload strictly and refuse.
2. **Journal removal.** `FileAuthorityJournal.remove_torn_tail() -> None` requires a torn tail
   found by the latest `load_recovering` on that object, and the lane's file
   identity (device, inode, size, mtime, ctime) unchanged since that load, with `pread` of the
   torn range equal to `torn.data`. It preserves the bytes in `retracted/` as
   `<lane stem>.torn.<sha256 hex of data>` through the existing `_preserve` (private temp file,
   fsync, rename, directory fsync), then truncates the lane to `offset` and fsyncs it, or unlinks
   the lane and fsyncs the directory when `offset == 0`, then reloads strictly. `retract` and
   `remove_torn_tail` share one private tail-removal helper; `_preserve` takes the evidence name.
3. **Host decision.** `_retract_unanchored_tail` loads with `load_recovering`. With a torn tail
   it recovers only when (a) a head exists and the last complete record matches it
   (`_head_matches`), or (b) no head exists and there is no complete record. Otherwise it raises
   `HostReadinessError("journal", "invalid-lane")`. A recovered torn tail is the one tail removed
   for that lane in this startup; the complete-record retraction is not also attempted. The
   function returns `JournalRecordV1 | TornTail | None`; the reconcile log line names the torn
   tail's system, offset, length, and SHA-256. An `OSError` from the removal is logged with its
   errno name before it is reported as `invalid-lane`, so a full disk is distinguishable.
4. **Startup trigger.** `run_authority_host` also runs the reconcile step when the first static
   check fails with `journal: invalid-lane`. A lane that is corrupt in any other way fails the
   reconcile's first (read-only) pass with the same `invalid-lane`.

Periodic readiness and `check_authority_host_once` call neither function and stay read-only.
The append path, the on-disk record format, and the `retracted/` inventory rule are unchanged.

## Failure model

1. **Actors and deployments** — the authority host process (systemd unit, one per authority
   instance) starting after a crash, power loss, or write error; the operator reading
   `retracted/`.
2. **Invariants and assets at stake** — no anchored record is removed or altered; the trusted
   head never moves; at most one tail is removed per lane per startup; removed bytes are
   preserved before the lane changes; a lane whose defect is not a torn unanchored tail still
   refuses service.
3. **Accepted failure classes** — power loss that corrupts an earlier, already-fsynced record,
   or zero-fills a torn region that contains a newline: refused as `invalid-lane` (not an
   interrupted append; operator repair, excluded). A crash between preserving the evidence and
   truncating: the next startup preserves the same bytes under the same name and truncates.
   With another authority holding the socket lock, a torn lane now reports
   `journal: reconcile-busy` instead of `invalid-lane` (still refuses). A lane torn by `ENOSPC`
   refuses while the evidence write cannot complete, with the lane unchanged, and recovers at the
   first startup after space is freed.
4. **Covered elsewhere** — reader exclusion for the periodic check (#2933 amendment);
   operator repair tooling (operator, excluded); an atomic append write path (rejected in the
   amendment, excluded).

### Threat model

1. **Boundary inventory** — widened: startup now writes to a lane on a torn tail, and adds
   `retracted/` evidence names derived from lane contents (a hex digest, never raw bytes).
2. **Actor model** — only the authority identity can write the journal directory (mode 0700,
   owner-checked, `O_NOFOLLOW` chain); the worker and other callers reach the journal only
   through the service. An actor able to write the lane already controls the fence.
3. **Control per boundary** — the removal requires the head match (DB-trusted), the socket and
   advisory locks, an unchanged file identity, and the exact torn bytes; the evidence path keeps
   `_preserve`'s ownership and mode checks. Refusals report only component and reason.
4. **Explicitly out of scope** — a hostile local writer of the journal directory (it can already
   forge the lane; ADR-0584 trusts the authority identity).

## Success

- A lane of complete records ending at the head, plus a torn line, starts: lane equals the
  prefix, evidence holds the torn bytes, the static check passes.
- A headless lane holding only a torn line, or zero bytes, starts: lane unlinked, evidence
  preserved.
- Each refusal case in Design 3 and the oversized and non-final corrupt cases in Design 1 still
  refuse with `journal: invalid-lane` and leave the lane and `retracted/` untouched.
- The standalone check leaves a recoverable torn lane and `retracted/` untouched.
- The real startup (`run_authority_host`, Postgres-backed heads) recovers a lane whose refused
  record was torn mid-append.

## Validation

Focused tests in `tests/providers/external_boot_authority/test_journal.py` (recovery load,
removal, refusals), `tests/providers/external_boot_authority/test_host.py` (decision table,
startup trigger), and `tests/db/test_connected_authority_acceptance.py` (real startup). Each
refusal is proven to bite by a controlled fault. The ADR amendment is prose with no executable
consumer.
