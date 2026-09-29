# Retract an unanchored journal tail (#2793)

## Scope and authority

Campaign scope for issue #2793, token `q2793-4edeb390` (successor to `q2793-3f982e7f`; the
exclusion set is unchanged). The operator approved these exclusions on 2026-09-28: the takeover-refusal root cause (#2884); an operator repair command for shorter or
divergent journals (deferred); per-lane rather than instance-wide readiness (follow-up
candidate); the retained-fixture settle (#2884); manual database or journal edits and host reset
(not authorized). No migration and no new ADR: this amends
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md).

Two orchestrator decisions (2026-09-28) refine the frozen criteria. The evidence lives in a
reserved, validated `retracted/` subdirectory of the journal directory, because every authority
unit runs `ProtectSystem=strict` and can write only its journal directory. The live proof is a
DB-backed, host-level integration test; the installed-host startup is proven by #2884's settle
on the retained lab host.

## Problem

`ExternalBootAuthorityService._anchor` appends and fsyncs a record, then asks Postgres to advance
the lane head. When the advance returns `superseded` or `conflict`, the record stays in the file
one sequence past the trusted head. The in-memory journal also keeps it while the lane's record
list does not, so the next append on that lane fails as non-contiguous. The periodic readiness
check compares every lane file with its head and raises `journal: head-mismatch` (or
`inventory-mismatch` when the refused record was the lane's first). The host exits, and every
restart fails the same check, so one refused anchor takes the authority out of service for every
System. ADR-0584 lists "a longer uncommitted suffix" among the cases that refuse service.

## Design

A record the database head never accepted is not evidence that anything happened: ADR-0584
anchors `admitted` and `mutation-started` before provider access, so an unanchored record either
precedes provider access or follows a `mutation-started` head that already marks the operation
unresolved. Removing exactly that one record restores the file/head equality. Two paths do it.

1. **Runtime retraction.** `FileAuthorityJournal.retract(record)` removes the exact final record
   that this journal instance validated or appended. It writes the record's exact bytes to the
   evidence directory first, then truncates the file to the previous record, or unlinks the
   file when this was the only record, fsyncs, and reloads its cache. `_anchor` calls it only
   when `advance` returned `superseded` or `conflict`, before raising the existing refusal. An
   exception from `advance` is an unknown outcome and keeps the record. If the retraction itself
   fails, `_anchor` logs the failure type and raises the same refusal. The record stays, and the
   startup path below handles it.
2. **Startup reconcile.** Only when its first readiness check fails with `journal:
   head-mismatch` or `journal: inventory-mismatch`, `run_authority_host` reconciles and then
   runs that check once more, so a clean start pays nothing. The reconcile holds the
   request-socket `flock` so that no other authority process for this instance is serving. It
   opens one database connection (role-checked, with the host's 5 s lock and idle-transaction
   timeouts) and lists the heads. For each local lane whose file ends in an eligible record
   against that head, it opens a transaction, takes `pg_advisory_xact_lock(hashtextextended('kdive:system:' ||
   id, 2126))`, the same key the advance function takes, lists the heads again, and retracts the
   final record only when one of these holds:
   - the file has exactly `head.sequence + 1` records and record `head.sequence` equals the head
     under the existing head predicate (instance, System, sequence, digest, phase, authority,
     generation, operation identity), or
   - no head exists and the file holds exactly one record for this instance and System.
   Any other difference is left in place, and the unchanged inventory check still refuses it.
   `check-external-boot-authority-host` and the periodic check never retract.

Evidence lives in `<journal_dir>/retracted/` as `<system_id>.<sequence>.<digest hex>.jsonl`:
mode 0600 files in a mode 0700 directory owned by the authority. The journal creates the
directory on first use, relative to its own validated parent descriptor, and refuses one that is
a symlink, not a directory, foreign-owned, or not exactly 0700. It cannot live under `state_dir`:
all three authority units run `ProtectSystem=strict`, and their `ReadWritePaths` name the
journal directory and fixed subtrees but never `state_dir` itself, so a new path there fails with
`EROFS`. Writing is idempotent: an existing file
with identical bytes is accepted, which covers a crash between the evidence write and the
truncation. Both paths log a warning with the System, sequence, and digest.

The readiness inventory (`_local_lanes`) accepts exactly the name `retracted`, and only after it
validates as a real directory (not a symlink) owned by the authority with mode 0700. It is not a
lane, it does not count toward the lane limit, and nothing inside it is enumerated or read. Every
other entry that is not `<uuid>.jsonl`, including any other directory, still fails
`unsafe-tree`.

ADR-0584 gains a dated amendment that permits exactly these two cases and keeps the refusal for
every other file/head difference.

## Success

- A refused anchor (`superseded`/`conflict`) leaves the lane file ending at the trusted head and
  the lane's journal cache equal to its record list, so the host keeps passing readiness. The
  refusal stays bounded to that System: a takeover `conflict` still marks only that lane failed
  (existing behaviour), and a `superseded` lane serves its next request against the head.
- An `advance` exception keeps the record.
- A host started over one unanchored final record in a lane (chained to the unchanged head, or a
  lone first record with no head) retracts it and becomes ready. Its bytes are preserved.
- A head that moved to the tail's sequence, a two-record suffix, a shorter file, a digest
  mismatch, or a busy socket lock still refuses or fails readiness.

## Failure model

1. **Actors and deployments** — the installed authority service (one systemd unit per authority
   instance) and its Postgres. Operators start it; workers reach it only through the transport.
2. **Invariants and assets at stake** — journal continuity (anchored records are never removed);
   the head never moves backward; retracted bytes are never lost; no retraction races an
   in-flight advance.
3. **Accepted failure classes** — a dead process's advance cannot commit after the lock is taken,
   because the client must send `COMMIT` and a dead client's transaction is aborted (5 s idle
   timeout). If a live process's advance is still queued behind the lock, the flock excludes it.
   `check-external-boot-authority-host` still fails over a tail until the service has started
   once. Evidence files are never pruned. Each is one record of at most 1 MiB, but their number
   grows with the count of refused anchors, which the retraction warning makes visible to the
   operator; removing files from `retracted/` is safe because nothing reads them. The reconcile's
   lane loads share a 10 s deadline inside the 20 s readiness timeout, so a slow lane fails in
   its thread rather than being cancelled while it still holds the locks. A retraction already
   past its load (one evidence write and one truncate) can still outlive a cancellation.
4. **Covered elsewhere** — the takeover refusal that produced the retained case (#2884); shorter
   or divergent journals (deferred operator repair); per-lane readiness (follow-up). A
   pre-existing race between the periodic check's head read and a concurrent anchor is not
   changed here and is reported in the PR as an adjacent observation.

## Threat model

- **Boundaries:** none is added. The file removal is local to the authority-owned journal
  directory, and the evidence directory is a reserved child of it. The inventory exemption is one
  exact name with owner, type, and mode checks, so it cannot hide a lane or admit a symlink.
- **Actors:** the local authority identity only. Workers cannot trigger startup retraction, and a
  worker can only cause a runtime retraction by getting an anchor refused, which already refuses
  its request.
- **Controls:** `O_NOFOLLOW` directory descriptors, owner and mode validation, exact cached bytes
  and identity checks before truncation, and lock-verified head equality.
- **Out of scope:** a compromised authority identity, which can already rewrite its own journal.

## Validation

- `tests/providers/external_boot_authority/test_journal.py`: retract truncates or unlinks,
  preserves bytes, is idempotent over existing evidence, and refuses a record that is not the tail
  or a changed file.
- `tests/providers/external_boot_authority/test_service.py`: a refused anchor retracts; an
  `advance` exception keeps the record; after a `superseded` refusal the next anchor on the lane
  succeeds.
- `tests/providers/external_boot_authority/test_host.py`: the eligibility cases and every refusal
  above, plus a busy socket lock. The evidence directory: a symlink named `retracted`, a
  wrong owner or mode, a regular file named `retracted`, and any other directory entry are
  rejected as `unsafe-tree`; a `<uuid>.jsonl` inside `retracted/` is never counted as a lane.
- `tests/db/`: the authority login role can take the lane lock, and blocks while another session
  holds it.
- `tests/db/test_connected_authority_acceptance.py`: the host-level proof. It builds the real
  SQL-backed service against Postgres, gets an anchor refused by the real head-advance function
  (the worker incarnation is fenced between the service's checks and the advance), keeps the
  record by failing the runtime retraction, and then runs `run_authority_host` over that journal.
  `main` refuses with `journal: head-mismatch`; this build retracts the tail, reaches
  `READY=1`, and keeps the record's exact bytes under `retracted/`. Only host facts that the test
  cannot own (the installed access boundary and the provider socket) are stubbed.
- Installed-host proof: not run on this PR. #2884's single settle on the retained lab host proves
  the installed startup with both builds deployed.
