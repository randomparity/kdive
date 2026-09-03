# 0592 — Authority commit context carries the anchored journal proof

## Status

Accepted (2026-09-03)

## Context

ADR-0584 makes the authority journal the evidence of record for what mutation may have
happened, and requires `admitted` and `mutation-started` to be anchored before any provider
access. ADR-0586 gives the local provider an accounted-cleanup obligation: cleanup publishes
a durable tombstone that is retained until the authority finalizes it, and finalization must
present "an unresolved exact `mutation-started` proof" for the still-current operation.

Those two halves cannot currently be joined. `AuthorityMutationAdapter.commit` takes
`(request: AuthorityMutationRequestV1, commit_point: str)`, and the service calls it as
`commit(request, request.operation)`. The adapter therefore receives the operation name and
nothing else. `AuthorityMutationRequestV1` extends `_AuthorityBinding` with `attempt_id`,
`expected_source_identity`, `intended_target_identity`, and `recovery_objects`; it carries no
journal sequence and no journal digest, and both models are closed, so neither value can be
smuggled through as an extra field.

The service holds both values at the moment it calls the adapter. `execute_mutation` anchors
the `mutation-started` record immediately beforehand and keeps it as the last element of the
lane's record list. It simply does not pass it.

The constraint that shapes the decision: `AuthorityMutationRequestV1` is the client-supplied
wire request, decoded from peer bytes by `decode_authority_request`. Journal sequence and
digest are service-owned facts. A client that could assert them could name a journal record
it did not cause, which is the deletion authority ADR-0586 gates on. So the values must reach
the adapter over a channel the wire request does not touch.

## Decision

We will widen the adapter seam from `commit_point: str` to a closed, service-constructed
`AuthorityCommitContextV1` carrying the commit point, the operation identity, the attempt,
and the `sequence` and digest of the `mutation-started` record the service just anchored for
that mutation. `AuthorityMutationRequestV1` gains no journal field.

The context is constructible only from a `JournalRecordV1` whose phase is
`mutation-started`; `AuthorityCommitContextV1.for_record` refuses any other phase, so the
phase a downstream proof asserts is proven rather than assumed. Before handing the context to
the adapter, the service re-reads the trusted journal head; a head that is absent, or that
still belongs to this operation identity while disagreeing with the anchored record, is
`AuthorityServiceError("journal_conflict")`. A head that has moved to a *different* operation
identity is the concurrent takeover this protocol already stalls behind, and is left to the
existing `completion_binding` path.

The local adapter builds a `FinalizeCleanupProof` from that context and calls
`LocalLibvirtExternalBoot.finalize_cleanup_tombstone` on the `cleanup` commit point, which
gives ADR-0586's tombstone its finalizer and ADR-0584's accounted-cleanup evidence an
end-to-end path.

## Consequences

`AuthorityMutationAdapter` is an internal `Protocol` with one production implementer
(`LocalExternalBootAuthorityAdapter`) and one in-tree test double
(`tests/providers/external_boot_authority/service_support.py`), so the signature change is a
replacement rather than a migration. Both update in the same change, along with the roughly
two dozen call sites in `tests/providers/local_libvirt/test_external_boot_authority.py` that
pass a bare operation string today.

The service gains one `read_head` call per mutation on the commit path. State plainly what
that buys, because the obvious answer is the wrong one: the check is scoped to the mutation's
own operation identity, and within that identity nothing writes between the anchor and the
provider call — `resolve_current` only reads. So it does **not** catch the concurrent-takeover
window, which `acknowledge_takeover` opens under a *different* operation identity and which the
scoping deliberately excludes. What it catches is a trusted head that no longer matches the
record `advance` reported as accepted under this identity: a second authority instance sharing
the operation identity, or a repository row that changed underneath. That is a narrower
guarantee than a compare-and-set alone, not a broader one, and it is the honest reason to pay
for it.

`FinalizeCleanupProof.operation_id` was patterned `^[0-9a-f-]{36}$` when it was written
against an unresolved seam. The shared contract's operation identifier is
`_AuthorityBinding.operation_identity`, bounded text that ADR-0584 names "operation identity"
and that is not UUID-shaped in practice. The pattern widens to that same bounded-text rule so
the field can carry the real identity; deriving a UUID from it would make the proof's own
operation field synthesized, which is what the field exists to avoid. The model is
pre-release with no production caller, so it is replaced in place.

Cleanup destroys the durable recovery record it commits against, and that produces **two**
distinct post-cleanup states, which must not be conflated:

1. **Tombstone live.** `publish_tombstone` writes `tombstone.json` and unlinks `intent.json`.
   The recovery directory still exists, and `recovery_point` fails with `FileNotFoundError`
   raised by `_read_private_file` on the missing `intent.json`.
2. **Fully finalized.** `finalize_tombstone` unlinks the tombstone and `rmdir`s the directory,
   so the same `FileNotFoundError` is raised one level earlier, by `_open_private_directory`.

The adapter's already-accounted branch is keyed on state 2 — the **recovery directory** being
provably gone — never on `intent.json` alone. State 1 stays the bounded `provider_conflict` it
already is today, which quarantines rather than reporting a success that never finalized. So
does any other operation, and any read that merely failed. Without that distinction a crash
between the cleanup and the finalization would make the retry return success while the
tombstone leaked silently, which is the opposite of ADR-0586's accounted cleanup.

Three residuals this decision does not close. The first is new and is the cost of this
decision; the other two are inherited from #2199's adapter and neither is made worse here.

- **A crash between `cleanup()` and finalization strands the tombstone with no recovery path,
  and this decision does not add one.** Recovering state 1 would need finalization addressable
  without a `RecoveryPoint`, and that is not constructible: `FinalizeCleanupProof.point_digest`
  is computed from the recovery point, so once `intent.json` is gone the proof the store
  compares cannot be built. Reading the digest back out of the tombstone instead would make
  the comparison assert what it is supposed to check. Closing this needs a different
  `FinalizeCleanupProof`/`CleanupTombstoneV1` shape, which is ADR-0586's to decide, not this
  seam's. **Non-regression boundary:** state 1 is exactly the state #2199 already leaves after
  every cleanup commit — the record unlinked, the tombstone retained, a retry answering
  `provider_conflict`. This decision does not widen it; it only declines to narrow it.
- The `teardown` commit point also publishes a tombstone through `cleanup()` and still has no
  finalizer, so teardown retains its tombstone directory.
- Because cleanup destroys the record the post-commit observation reads, the service's
  `observe` for a cleanup mutation classifies `unreadable` and journals a terminal outcome of
  `conflict` — true before this change and unchanged by it.

A head-disagreement refusal (above) leaves the operation unresolved at `mutation-started`
rather than raising bare, so the service anchors `terminal` with `outcome="never-began"` before
it raises. Without that, the next admission on the lane would run `_finish_recovery`'s
`MUTATION_STARTED` arm and journal a full provider-observation cycle for a mutation the
authority knows never reached the provider — and ADR-0584 makes the journal the evidence of
record for exactly that question.

### The durable records gain a required field, and that has an ordering constraint

`LocalRecoveryMetadataV1` and `LocalPreStopIntentV1` gain a required `target_xml_sha256` with
no migration. Three things make that the correct call rather than a shortcut, and all three
have to be stated together or the last one gets lost.

**No default or optional field would have helped.** `RecoveryMetadataStore._read` validates
the JSON and *then* requires `_metadata_bytes(metadata) == data`, raising "recovery intent is
not canonical JSON" otherwise; `_read_pre_stop` does the same through `_pre_stop_bytes`.
Re-serializing a model that filled in a default produces bytes that differ from the file, so
an optional field fails canonicality even though it parses. Any schema addition to these two
records is a breaking change for existing files. There is no cheap compatible variant to
prefer.

**No existing file can break, because the feature is dormant.** `ProviderRuntime.external_boot`
is `None`, `LocalExternalBootMaterializer` has no implementation under `src/`, and
`RealLocalExternalBootIO` — the only writer of a `RecoveryMetadataStore` — is constructed
nowhere in `src/`: `rg -n 'RealLocalExternalBootIO' src/ tests/` returns nine test hits and a
single `src/` hit that is a docstring mention in `composition.py`. No durable recovery record
exists in any deployment, so no compatibility contract is owed and the repository's
replace-by-default rule for pre-release persisted data applies.

**That window closes when the feature is wired, so this must land before #2212.** #2212
constructs `RealLocalExternalBootIO` and binds composition. Once it lands and a deployment
runs it, durable records exist and adding a required field to either record stops being free
and becomes a real migration against the canonicality rule above. **This change is therefore
ordered strictly before #2212**, and a reordering silently converts a free change into a
breaking one. Whoever implements `LocalExternalBootMaterializer.inspect_prepare` inherits the
new field as a write site.

## Considered & rejected

- **Add `journal_sequence` and `journal_digest` to `AuthorityMutationRequestV1`.** verified:
  the request is decoded from peer bytes by `decode_authority_request`
  (`src/kdive/providers/external_boot_authority/protocol.py:194-206`), so a field on it is a
  field the client asserts; ADR-0586 gates recovery-object deletion on the authority's own
  `mutation-started` proof, which a client-assertable sequence would forge. Ruled out
  explicitly by issue #2207 and its orchestrator comment.
- **Pass the anchored `JournalRecordV1` itself as the second argument.** judgment: the record
  carries `expected_source_identity`, `intended_target_identity`, `recovery_objects`,
  `observation`, and `outcome`, none of which the adapter needs to finalize a tombstone.
  Handing the provider seam the whole journal record widens what an adapter may select from,
  which the issue's "Expected" section rules out in as many words.
- **Add a third parameter to `commit` beside `commit_point`.** judgment: the second shape the
  issue offered. It leaves two arguments that must agree about which operation is being
  committed, and the existing adapter check that `commit_point` equals `request.operation`
  would have to grow a second arm. One closed value carrying both is strictly fewer moving
  parts for the same information.
- **Have the adapter read the journal itself.** verified: the journal is a
  `FileAuthorityJournal` the service owns per lane (`service.py:233-240`), and ADR-0584 makes
  the authority the sole principal for its own records. An adapter that reads it would be a
  second reader of the evidence it is supposed to be presented with.
- **Do nothing and leave `finalize_cleanup_tombstone` uncalled.** verified: `rg` over `src/`
  reaches only the local module's own definitions of `finalize_cleanup_tombstone` and
  `finalize_tombstone`; every exercising call site is in
  `tests/providers/local_libvirt/test_external_boot.py`. ADR-0584's accounted-cleanup
  evidence stays incomplete for as long as that holds, and the tombstone accumulates with no
  finalizer.
- **Skip the trusted-head recheck and trust the just-completed compare-and-set.** judgment:
  the recheck is worth one read because `advance` reports what the repository accepted, not
  what it still holds. Under this operation identity it is the only thing that would catch a
  second authority instance sharing the identity, or a head row that changed after `advance`
  returned. It is explicitly *not* justified by the concurrent-takeover window — see the next
  bullet — and the earlier draft of this record made that wrong argument.
- **Make finalization addressable from the binding alone, so a crashed cleanup can be
  recovered on retry.** verified: not constructible with the current value shapes.
  `RecoveryMetadataStore.finalize_tombstone` compares the caller's `proof.point_digest`
  against `CleanupTombstoneV1(binding=..., point_digest=LocalLibvirtExternalBoot.point_digest(recovery))`,
  and `point_digest` is `sha256` over `recovery.to_canonical_json()` — a `RecoveryPoint` that
  can only be rebuilt from `intent.json`, which `publish_tombstone` has already unlinked.
  `CleanupTombstoneV1` carries `binding` and `point_digest` and none of the identities
  `RecoveryPoint` needs, so the digest cannot be recomputed from the tombstone either, and
  reading it back out of the tombstone to fill the proof would make the store's comparison
  assert its own input. Closing this needs a different proof or tombstone shape, which
  belongs to ADR-0586.
- **Compare the context against the bare lane head, whatever operation it belongs to.**
  verified: `acknowledge_takeover` anchors `TAKEOVER_SUPERSEDED` and `WATERMARK_INSTALLED`
  under the takeover request's own operation identity
  (`src/kdive/providers/external_boot_authority/service.py:628-639`) *before* awaiting
  `active.done` (`:695-696`), so the lane head legitimately moves past an in-flight mutation.
  An unscoped comparison would reject that overlap, which ADR-0584 explicitly designs for and
  which the `completion_binding` path (`:681-687`, `:888-895`) exists to serve.
