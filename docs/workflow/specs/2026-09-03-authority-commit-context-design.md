# Authority commit context carries the anchored journal proof

Issue: [#2207](https://github.com/randomparity/kdive/issues/2207) (epic #2105).
Decision record: [ADR-0592](../../adr/0592-authority-commit-context-carries-the-anchored-journal-proof.md).
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

**Ordering constraint.** The `target_xml_sha256` addition is a breaking change for any durable
record already on disk — `RecoveryMetadataStore._read` requires byte-exact canonical
re-encoding after validation, so an optional field with a default fails canonicality just as a
required field fails on absence. It is free only because the feature is dormant:
`ProviderRuntime.external_boot` is `None` and `RealLocalExternalBootIO` is constructed nowhere
in `src/`, so no deployment holds a record. **#2212 wires that construction, so this change
must land before #2212**; after it, the same addition owes a real migration.

## Data flow

    execute_mutation
      -> _anchor(MUTATION_STARTED)                     records[-1] is the anchored record
      -> AuthorityCommitContextV1.for_record(records[-1])
           refuses any phase but mutation-started
      -> _require_anchored_head(binding, context)      read_head must still equal it
      -> adapter.commit(request, context)
           local: legality checks, then resolve the recovery point
             point resolved, CLEANUP:
               ports.cleanup(point, authority)
               ports.finalize_cleanup_tombstone(point, proof, authority)
                 proof.journal_sequence == context.journal_sequence
                 proof.journal_digest   == context.journal_digest
                 proof.operation_id     == context.operation_identity
                 proof.attempt_id       == str(context.attempt_id)
                 proof.point_digest     == point_digest(point)
                 proof.phase            == context.phase        (carried, not defaulted)
             point unresolved, CLEANUP, recovery directory provably gone:
               already accounted — observation only, no provider mutation
             anything else:
               provider_conflict

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
  `completion_binding` path that exists to let the in-flight commit finish.

  State what the scoping leaves the check able to catch, because it is narrower than it first
  looks. Within one operation identity, nothing writes between the `mutation-started` anchor
  and the provider call — the only intervening statement is `resolve_current`, which reads. So
  the check cannot fire on a same-instance race. What it catches is a trusted head that no
  longer matches what `advance` reported as accepted under this identity: a second authority
  instance sharing the operation identity, or a repository row that changed underneath. That
  is the whole of its value.
- **N4a.** A head-disagreement refusal anchors `terminal` with `outcome="never-began"` before
  it raises, reusing the shape `execute_mutation`'s `stop_before_start` path already uses.
  Without it the operation stays unresolved at `mutation-started`, and the next admission on
  the lane runs `_finish_recovery`'s `MUTATION_STARTED` arm, which calls the adapter's
  `observe` and journals `provider-returned`, `observed`, and a terminal outcome — a full
  provider-observation cycle for a mutation that provably never reached the provider. ADR-0584
  makes the journal the evidence of record for exactly that question. Source: necessary
  consequence of criterion 5, which requires the refusal to be a bounded category and not a
  new falsehood in the journal.
- **N5.** The local adapter's `cleanup` commit point calls `finalize_cleanup_tombstone` with a
  proof whose every field comes from the context or the resolved recovery point; none is
  defaulted or synthesized. `phase` is included in that: it is passed from `context.phase`
  rather than left to `FinalizeCleanupProof`'s model default, so an assertion on the proof's
  phase observes the anchored record's phase reaching the proof instead of observing the
  default. Source: criteria 3 and 7.
- **N6.** A second `cleanup` commit against the same recovery point succeeds and performs no
  provider mutation.

  Cleanup leaves **two** distinct states, and the branch keys on the second only:

  1. **Tombstone live** — `publish_tombstone` wrote `tombstone.json` and unlinked
     `intent.json`. The recovery directory still exists; `recovery_point` fails with
     `FileNotFoundError` from `_read_private_file` on the missing `intent.json`.
  2. **Fully finalized** — `finalize_tombstone` unlinked the tombstone and `rmdir`ed the
     directory, so the same `FileNotFoundError` comes one level earlier from
     `_open_private_directory`.

  The already-accounted branch requires state 2 — the **recovery directory** provably gone —
  and is bounded to `AuthorityOperation.CLEANUP`. State 1 keeps today's `provider_conflict`,
  which quarantines rather than reporting a success that never finalized; so does every other
  operation and every read that merely failed. Keying on `intent.json` instead would let a
  crash between the cleanup and the finalization return success while the tombstone leaked
  silently, and would skip `_require_matching_identities` and the
  `_ownership_is_proven(require_named=True)` deletion gate for a binding whose record never
  existed. Source: criterion 8, plus the necessary consequence that no implementation
  satisfies it while a fully finalized cleanup is a conflict.
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
seam now carries evidence a provider acts on, where it previously carried a name. The durable
on-disk recovery record's boundary is **unchanged** — say so plainly rather than calling it
strengthened. `target_xml_sha256` and the bytes it measures live in the same file, and
`_metadata_extends_intent` compares the metadata against the intent, both of which the actor
named below can rewrite consistently. Against that actor the field adds nothing. What it does
buy is real and smaller: it catches non-adversarial corruption and truncation, and it brings
`target_xml` to the same footing as `source_xml_sha256`, closing an asymmetry in a record
where one XML blob was digest-bound and the other was not. The controls on that boundary
remain the `0o700` mode and euid checks. No new external entry point is added;
`decode_authority_request` is untouched.

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
- Idempotent-retry branch: bounded to `cleanup` and to a provably absent **recovery
  directory** at the owner-derived path for exactly this binding. A live tombstone is not
  absence, and neither is a read that merely failed; neither takes the branch (N6). This is
  the control that keeps the branch from bypassing `_require_matching_identities` and the
  `_ownership_is_proven(require_named=True)` deletion gate.
- Durable record: `target_xml_sha256` is checked in the same validator arm as
  `source_xml_sha256`, on both records, because `_metadata_extends_intent` compares the pair
  to each other and a consistently tampered pair would otherwise pass. This is an integrity
  check against corruption and an asymmetry fix, not a control against the actor above.

**Explicitly out of scope.** An attacker who can write the recovery directory can still
substitute a *self-consistent* record whose digests match its own bytes; nothing here signs
the record, and the `0o700`/euid requirement is the control that bounds it. A crash between
the cleanup and its finalization strands a tombstone that this change cannot recover, because
the proof the store compares needs a `RecoveryPoint` that no longer exists — the shape change
that would fix it belongs to ADR-0586, and the stranded state is exactly the one #2199 already
leaves. The remote adapter has no tombstone seam and is not touched. Journal content and
anchoring are unchanged.

## Testing

Every criterion gets a test that drives a real entry point.

- N1, N5, N7 (adapter side) are proved through `ExternalBootAuthorityService.execute_mutation`
  with a recording adapter, not by calling `adapter.commit` directly — a green test through
  the wrong entry point proves nothing about the service.
- N2 is proved by asserting `set(AuthorityMutationRequestV1.model_fields)` is unchanged and
  that `model_validate` rejects a payload carrying `journal_sequence`.
- N3 is proved by calling `for_record` with a `provider-returned` and an `observed` record.
- N4 is proved by a repository double whose `read_head` returns a different sequence/digest
  under the *same* operation identity after the anchor, asserting `journal_conflict` and that
  the adapter recorded no `commit`; and by a companion asserting that a head reporting a
  *different* operation identity — the concurrent-takeover shape — still lets the commit run,
  so the scoping is proved rather than asserted.
- N4a is proved by asserting the journal's last record after a refused commit is `terminal`
  with `outcome="never-began"`, and that no `provider-returned` record was written.
- N6 is proved by two `execute_mutation` cleanup rounds against one recovery point, asserting
  the second returns and that the coordinator's `cleanup` and finalize action counts did not
  grow; and by a companion asserting that the *tombstone-live* state — the recovery directory
  present with `intent.json` gone — still raises `provider_conflict`, which is the arm that
  makes the branch key on the directory rather than on the record.
- N7's refusal is proved per record by writing a record to the store, substituting
  `target_xml` on disk, and asserting the reopen is refused. Criterion 10's reachability is
  proved by driving the coordinator path that would define — `LocalLibvirtExternalBoot.activate`
  over the real `RealLocalExternalBootIO`/session harness — against a record whose `target_xml`
  was substituted on disk, and asserting it raises *and* that the session recorded no
  `define_xml` call. The fault that test must catch is deleting the `target_xml_sha256`
  comparison from the validator: with the comparison gone the substituted record validates,
  `activate` proceeds, and `define_xml` is recorded. A test that hands the session an
  unsubstituted record and asserts the substituted bytes are absent proves nothing and is not
  acceptable here.
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
