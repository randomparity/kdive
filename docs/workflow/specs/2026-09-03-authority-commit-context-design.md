# Authority commit context carries the anchored journal proof

Issue: [#2207](https://github.com/randomparity/kdive/issues/2207) (epic #2105).
Decision record: [ADR-0591](../../adr/0591-authority-commit-context-carries-the-anchored-journal-proof.md).
Amends the cleanup-evidence consequence of
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md);
the tombstone obligation is [ADR-0586](../../adr/0586-local-external-boot-recovery-uses-an-owned-host-directory.md).

## Goal

Carry the anchored `mutation-started` record's `sequence` and digest across the
`AuthorityMutationAdapter.commit` seam as a closed value the authority service constructs,
and use it to give `LocalLibvirtExternalBoot.finalize_cleanup_tombstone` its first production
caller. Separately, bind `target_xml` to its own digest on the two durable local recovery
records, the way `source_xml` already is.

## Architecture

Three seams change, in one direction each.

1. **`protocol.py`** gains `AuthorityCommitContextV1`, a closed frozen value carrying
   `commit_point`, `operation_identity`, `attempt_id`, `journal_sequence`, `journal_digest`,
   and `phase`, pinned to `mutation-started`. Its only constructor from a record,
   `for_record`, refuses a record in any other phase.
2. **`service.py`** builds that context from the record it just anchored, verifies it against
   the trusted journal head, and passes it to `commit` in place of the bare `commit_point`
   string. `AuthorityMutationRequestV1` is untouched.
3. **`local_libvirt/external_boot_authority.py`** accepts the context, keeps the existing
   commit-point legality checks against it, and on `AuthorityOperation.CLEANUP` builds a
   `FinalizeCleanupProof` from it and calls `finalize_cleanup_tombstone`.

`external_boot.py` changes only where those seams meet it: `FinalizeCleanupProof.operation_id`
widens to carry a real operation identity, the stale `#2140` comment is corrected, and the
two record models gain `target_xml_sha256`.

## Data flow

    execute_mutation
      -> _anchor(MUTATION_STARTED)                     records[-1] is the anchored record
      -> AuthorityCommitContextV1.for_record(records[-1])
           refuses any phase but mutation-started
      -> _require_anchored_head(binding, context)      read_head must still equal it
      -> adapter.commit(request, context)
           local: legality checks, then for CLEANUP
             ports.cleanup(point, authority)
             ports.finalize_cleanup_tombstone(point, proof, authority)
               proof.journal_sequence == context.journal_sequence
               proof.journal_digest   == context.journal_digest
               proof.operation_id     == context.operation_identity
               proof.attempt_id       == str(context.attempt_id)
               proof.point_digest     == point_digest(point)
               proof.phase            == "mutation-started"

Nothing on that path reads `AuthorityMutationRequestV1` for a journal value, because the
request has none.

## Normative behaviour

- **N1.** The context's `journal_sequence` and `journal_digest` equal the anchored
  `mutation-started` record's `sequence` and `record_digest(...)`. Source: issue acceptance
  criterion 1.
- **N2.** No protocol input reaches either field. `AuthorityMutationRequestV1` gains no
  journal field and stays `extra="forbid"`. Source: criteria 2 and 6.
- **N3.** `AuthorityCommitContextV1.for_record` raises for a record whose phase is not
  `mutation-started`, `provider-returned` and `observed` included. Source: criterion 4.
- **N4.** Before calling the adapter, the service re-reads the trusted journal head. When the
  head is absent, or when it still belongs to *this* operation identity but disagrees with the
  anchored record by sequence, digest, or phase, the service raises
  `AuthorityServiceError("journal_conflict")` and the adapter is never called. The error
  carries no provider output. Source: criterion 5.

  The check is scoped to the mutation's own operation identity on purpose. ADR-0584 prefers a
  stalled takeover over two concurrent mutators, and `acknowledge_takeover` anchors its
  supersession and watermark records — under a *different* operation identity — while an
  admitted mutation is still in flight, then waits on it. Comparing against the bare lane head
  would turn that intended overlap into a `journal_conflict` and defeat the
  `completion_binding` path that exists to let the in-flight commit finish. The window the
  check does close is real: `_anchor`'s compare-and-set runs inside the lane lock, and the
  provider call does not, while the existing `resolve_current` recheck compares against the
  takeover *acknowledgement*'s sequence and digest rather than the mutation's.
- **N5.** The local adapter's `cleanup` commit point calls `finalize_cleanup_tombstone` with a
  proof whose every field comes from the context or the resolved recovery point; none is
  defaulted or synthesized. Source: criteria 3 and 7.
- **N6.** A second `cleanup` commit against the same recovery point succeeds and performs no
  provider mutation. Reaching it requires treating a *proven absent* recovery record as the
  already-accounted cleanup: `publish_tombstone` unlinks the metadata and finalization removes
  the directory, so the retry cannot resolve a point. The branch is bounded to
  `AuthorityOperation.CLEANUP` and to `FileNotFoundError` from the durable store; every other
  operation, and every read that merely failed, keeps today's `provider_conflict`. Source:
  criterion 8, plus the necessary consequence that no implementation satisfies it while
  proven absence is a conflict.
- **N7.** `LocalRecoveryMetadataV1` and `LocalPreStopIntentV1` each carry `target_xml_sha256`
  and each validator refuses a record whose `target_xml` does not digest to it.
  `_metadata_extends_intent` compares the new field too. Source: criteria 9 and 10.
- **N8.** `TargetProjectionV1.digest` keeps measuring the projection inputs. It is not
  redefined and gains no XML. Source: criterion 11.

## Error handling

Every new refusal reuses an existing bounded category. The service raises
`journal_conflict` for a head disagreement. The adapter raises `provider_conflict` for an
illegal or mismatched commit point, exactly as it does today. The two record validators raise
`ValueError` on a digest mismatch, exactly as the `source_xml` arm does, and that surfaces
through the store's existing `model_validate_json` path. No new category, no new message
shape, and no provider output crosses the boundary.

## Threat model

**Boundary inventory.** One boundary is *widened*: the `AuthorityMutationAdapter.commit`
seam now carries evidence a provider acts on, where it previously carried a name. One is
*strengthened*: the durable on-disk recovery record's `target_xml` becomes digest-bound. No
new external entry point is added; `decode_authority_request` is untouched.

**Actor model.** The untrusted party at the protocol boundary is an authenticated worker
incarnation supplying an `AuthorityMutationRequestV1`. It is authenticated but not trusted to
assert authority facts. The untrusted party at the durable-record boundary is anyone who can
already write the recovery directory — which `_require_private_owned_directory` restricts to
mode `0o700` owned by the running euid, so this is a local actor who has already crossed a
privilege boundary. The design trusts the authority service's own journal and the process
euid; it trusts nothing the peer sends.

**Control per boundary.**

- Commit seam: the context is constructed only by the service, only from a record it
  anchored, and only in `mutation-started` phase (N3), then checked against the trusted head
  (N4). The peer's request never reaches either field (N2). Failure leaks a category name.
- Cleanup finalization: `finalize_cleanup_tombstone` already compares `proof.binding` and
  `proof.point_digest` against the recovery point, and the store re-reads and re-compares the
  tombstone before unlinking. Those are the existing controls and they are kept.
- Idempotent-retry branch: bounded to `cleanup` and to a `FileNotFoundError` raised by the
  durable store at the owner-derived path for exactly this binding. An I/O failure is not
  absence and does not take the branch (N6).
- Durable record: `target_xml_sha256` is checked in the same validator arm as
  `source_xml_sha256`, on both records, because `_metadata_extends_intent` compares the pair
  to each other and a consistently tampered pair would otherwise pass.

**Explicitly out of scope.** An attacker who can write the recovery directory can still
substitute a *self-consistent* record whose digests match its own bytes; nothing here signs
the record, and the `0o700`/euid requirement is the control that bounds it. The remote
adapter has no tombstone seam and is not touched. Journal content and anchoring are unchanged.

## Testing

Every criterion gets a test that drives a real entry point.

- N1, N5, N7 (adapter side) are proved through `ExternalBootAuthorityService.execute_mutation`
  with a recording adapter, not by calling `adapter.commit` directly — a green test through
  the wrong entry point proves nothing about the service.
- N2 is proved by asserting `set(AuthorityMutationRequestV1.model_fields)` is unchanged and
  that `model_validate` rejects a payload carrying `journal_sequence`.
- N3 is proved by calling `for_record` with a `provider-returned` and an `observed` record.
- N4 is proved by a repository double whose `read_head` returns a different sequence/digest
  after the anchor, asserting `journal_conflict` and that the adapter recorded no `commit`.
- N6 is proved by two `execute_mutation` cleanup rounds against one recovery point, asserting
  the second returns and that the coordinator recorded no second `cleanup` call.
- N7's refusal is proved per record by writing a record to the store, substituting
  `target_xml` on disk, and asserting the reopen is refused; and by asserting `_host_state`
  and `define_target` never reach `define_xml` with the substituted bytes.
- N8 is proved by asserting `TargetProjectionV1.digest` equals `sha256` over
  `canonical_bytes()` and changes with a projection input but not with any XML.

Each new test is bite-proved: with the implementation committed, a controlled fault is
injected, the test must fail on its own assertion rather than on collection, and the fault is
reverted and the file verified byte-identical.

## Out of scope

The remote adapter (#2200); the `teardown` commit point's missing finalizer; the
`unreadable`/`conflict` observation a cleanup mutation already produces because cleanup
destroys the record the observation reads; job handlers and the claim path; any change to
what the journal records or how it is anchored; any change to `TargetProjectionV1.digest`;
`lifecycle/boot/session.py`, which #2211 holds.
