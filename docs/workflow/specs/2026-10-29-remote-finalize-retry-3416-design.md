# Remote finalize accepts the receipt an earlier attempt recorded (#3416)

Decision record: [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md), amended. No new
decision, migration, wire or interface change.

## Problem

A remote release whose first CLEANUP is interrupted after the quarantine receipt is recorded, and
before finalize, fails on retry with `remote cleanup proof differs from the quarantine receipt`.

## Facts verified in source (main 5c3686fd6)

- The service gives a retried release generation the earlier generation's terminal CLEANUP
  (`adopted_release_phase` in `external_boot_authority/service.py`), so it skips the provider commit.
  `commit_cleanup` does not run on the retry, so nothing re-records the receipt.
  Where `commit_cleanup` does run, `record_cleanup_quarantine` already replaces a same-operation
  receipt.
- The retry still anchors its own `mutation-started` record and calls `finalize` with a context
  built from it: same `operation_identity`, new `attempt_id`, later `journal_sequence`, new digest.
- `finalize` required all four receipt fields to equal that context, so it always raised.
- The local finalizer compares the tombstone's owner and point digest, not the journal position
  (`finalize_tombstone`), so a retry finalizes there.

## Options

- **Re-anchor the receipt on the retry.** Rejected. The quarantine row published from attempt 1
  carries the receipt's journal digest, and the object port fences `delete`/`adopt` on that
  digest. Rewriting the receipt would invalidate the evidence the resolver acts on. It also
  writes a receipt that `finalize` deletes straight away.
- **Accept the recorded proof for the same operation (chosen).** The receipt stays as attempt 1
  wrote it, so published evidence and digest fences are unchanged, and finalize completes.

## Design

`finalize` accepts the receipt when `operation_identity` is equal and either:

- the attempt is the same and journal sequence and digest are equal (unchanged rule), or
- the attempt differs and the context's `journal_sequence` is later than the receipt's, which is
  what a retry anchored after the recording attempt looks like.

A different operation, a same-attempt mismatch, and a different attempt at an equal or earlier
position are refused as before.

## Not changed

`cleanup_quarantine_inventory` still requires the request's attempt to equal the receipt's. A
retry that is aborted again before `finalize` publishes against the earlier receipt and answers
`provider_conflict`. That second-abort case is outside #3416 and is reported as a follow-up.

## Tests

`tests/providers/remote_libvirt/test_external_boot_authority.py`: a receipt recorded by one attempt
finalizes under a later attempt's context; the refusal cases above stay red-on-regression.
The live cells are re-proved by the #3339 worker.
