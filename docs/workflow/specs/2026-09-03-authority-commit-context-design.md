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
                 proof.phase            == context.phase        (required, no default)
             point unresolved, any operation:
               provider_conflict — absence never identifies its own cause

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
- **N4a — withdrawn during implementation, and the reason is the finding.** An earlier draft
  required every refusal between the `mutation-started` anchor and the provider call to
  anchor `terminal` with `outcome="never-began"` first, so the lane was left resolved. It is
  not implementable, and on inspection it was also wrong.

  Not implementable: the journal enforces phase ordering, and
  `journal._NEXT_OPERATION_PHASES` allows `mutation-started` to be followed only by
  `provider-returned`. `terminal` is a legal successor of `admitted` — which is why
  `execute_mutation`'s `stop_before_start` path can anchor `never-began` — but not of
  `mutation-started`. Writing one raises "authority journal phase ordering is invalid".
  Changing that table is journal behaviour, which this charter excludes.

  Also wrong: the ordering encodes ADR-0584's rule that `mutation-started` is anchored
  *before* any provider access precisely because, after it, the authority cannot know whether
  the provider was reached. A `never-began` record there would assert something the journal's
  own model says is unknowable, and a crash-recovery reader could not distinguish it from a
  true one. The designed resolution is the observation cycle `_finish_recovery` runs: it calls
  the adapter's `observe` and journals what was *observed*, which is accurate whether or not
  the provider was touched. So leaving the operation unresolved is correct, and both refusals
  on this path — the new head-disagreement one and the pre-existing `resolve_current` recheck
  — raise without a terminal record, as the recheck already did.

- **N5.** The local adapter's `cleanup` commit point calls `finalize_cleanup_tombstone` with a
  proof whose every field comes from the context or the resolved recovery point; none is
  defaulted or synthesized. To make that literally true of `phase`,
  `FinalizeCleanupProof.phase` loses its model default and becomes required. With a default it
  carried no information — `AuthorityCommitContextV1.phase` pins the same single literal, so
  passing it and defaulting it produce the same value and no assertion could distinguish them.
  Required, omitting it is a `ValidationError`. Source: criteria 3 and 7.
- **N6.** The adapter never infers from the absence of the recovery record. A `cleanup` commit
  whose recovery point does not resolve is `provider_conflict`, exactly as it is today.

  `recovery_point` raises `FileNotFoundError` in at least four states, and no discriminator
  available at this seam separates them:

  1. **Tombstone live** — only `intent.json` is unlinked; the directory remains. Raised by
     `_read_private_file`.
  2. **Fully finalized** — the tombstone and directory are gone. Raised by
     `_open_private_directory`.
  3. **Never prepared** — the directory never existed, and the binding is peer-chosen. Same
     raise site as 2.
  4. **Prepare interrupted** — only `.{name}.partial` exists, holding the captured recovery
     archive, because `complete_preparation`'s rename never ran. Same raise site as 2.

  Keying on `intent.json` conflates 1 with 2; keying on the directory conflates 2, 3 and 4.
  Either way a peer-chosen binding that never owned a recovery object would reach a success
  answer while skipping `_require_matching_identities` and the
  `_ownership_is_proven(require_named=True)` deletion gate. Refusing all four together is the
  only answer that bypasses no gate.

  **The idempotency the seam owes is met on positive evidence instead.**
  `self._adapter.commit` is called at one site, once per `execute_mutation`;
  `_finish_recovery` re-drives only `observe`. So the authority never re-drives a cleanup
  commit for the same operation, and within the one commit that runs, `cleanup` early-returns
  on `cleanup_complete` — a positive match against the on-disk tombstone's recorded
  `point_digest` — and `finalize_tombstone` returns cleanly when the tombstone is already
  gone, both with the recovery point in hand.

  Source: criterion 8's intent. **Criterion 8's literal form is not satisfiable and this design
  does not claim it.** A test that "commits cleanup twice against the same recovery point and
  asserts the second call succeeds" cannot be written honestly at this seam, because the first
  commit destroys the record the adapter needs to address the second and any passing version
  would be discriminating on absence. The primitive-level idempotency the criterion points at
  is already proven at `tests/providers/local_libvirt/test_external_boot.py:3566-3567`.
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

**The no-leak property is checked against the exception types actually enforced, not just
asserted.** `AuthorityServiceError.__init__` passes only its four-valued `category` literal to
`RuntimeError`, so the exception carries no message, no path, no `filename` and no `strerror` —
unlike the `OSError` subclasses (`FileNotFoundError`, `NotADirectoryError`) the store raises
underneath it, which carry all three. The boundary holds because every raise inside an `except`
block on this path uses `from None`, never `from exc`: chaining would re-attach the leaking path
through `__cause__` and the traceback, which looks fixed and is not. That is already true of the
existing sites, and the new code preserves it:

- the head-disagreement refusal and the shared abandonment helper raise outside any `except`, so
  nothing chains;
- the `FinalizeCleanupProof` construction sits **inside** `_apply`'s existing `try`, so a
  `ValidationError` — which would render field values — is converted by the existing
  `except Exception:` arm into a bounded `provider_conflict` `from None`;
- the full diagnostic still reaches `logger.exception` inside the authority, where ADR-0584 allows
  it to exist, and only the category crosses the seam.

Tests assert `raised.value.category`, never a rendered message, so they cannot pass on text that
happens to contain a path.

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
- Unresolvable recovery point: refused as `provider_conflict` with no branch of any kind, so
  `_require_matching_identities` and `_ownership_is_proven(require_named=True)` are reached on
  every path that could delete recovery evidence (N6). There is no absence-derived success
  answer to bypass them.
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
- N4a is proved twice, once per arm: after a head-disagreement refusal, and after a
  `resolve_current` recheck refusal, the journal's last record is `terminal` with
  `outcome="never-began"` and no `provider-returned` record exists for that operation
  identity. A third test drives a second `execute_mutation` on the same lane afterwards and
  asserts the adapter's `observe` was never called for the abandoned operation — which is what
  the unresolved lane would otherwise cost. The fault each catches is removing the helper call
  from its arm.
- N6 is proved by driving a `cleanup` commit through the service for each of the four
  unresolvable states — tombstone live, fully finalized, never prepared, prepare interrupted —
  and asserting every one raises `provider_conflict` and that the coordinator recorded no
  `cleanup` and no `finalize` action. The fault each catches is adding back any
  absence-derived success branch: with one present, the state it keys on returns instead of
  raising. Positive-evidence idempotency is proved separately at the primitive, by committing
  a cleanup whose tombstone already exists and asserting `cleanup_complete` short-circuits it
  with no second provider mutation.
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

## Criterion 13 and the fault-inject port

Criterion 13 requires the fault-inject port to keep working. It is met by non-modification, and
that is worth stating rather than leaving as an absent component: the fault-inject provider
implements no `AuthorityMutationAdapter`, so the widened `commit` signature does not reach it.
`tests/providers/contract/bindings/fault_inject.py` binds the external-boot *contract*, not the
authority seam. Nothing in the permitted surface touches `providers/fault_inject/`, and the
guardrail suite is what proves it stayed working.

## Out of scope

The remote adapter (#2200); the `teardown` commit point's missing finalizer; the
`unreadable`/`conflict` observation a cleanup mutation already produces because cleanup
destroys the record the observation reads; job handlers and the claim path; any change to
what the journal records or how it is anchored; any change to `TargetProjectionV1.digest`;
`lifecycle/boot/session.py`, which #2211 holds.
