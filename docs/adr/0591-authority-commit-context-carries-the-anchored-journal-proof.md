# 0591 — Authority commit context carries the anchored journal proof

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
(`LocalExternalBootAuthorityAdapter`) and two in-tree test doubles, so the signature change
is a replacement rather than a migration. Every implementer updates in the same change.

The service gains one `read_head` call per mutation on the commit path. That is a real cost
on a hot lane, and it buys the only check that can catch a head advanced between the
`mutation-started` anchor and the provider call — a window that is outside the lane lock
today, where the existing `resolve_current` recheck compares against the *takeover
acknowledgement*'s sequence and digest, not the mutation's.

`FinalizeCleanupProof.operation_id` was patterned `^[0-9a-f-]{36}$` when it was written
against an unresolved seam. The shared contract's operation identifier is
`_AuthorityBinding.operation_identity`, bounded text that ADR-0584 names "operation identity"
and that is not UUID-shaped in practice. The pattern widens to that same bounded-text rule so
the field can carry the real identity; deriving a UUID from it would make the proof's own
operation field synthesized, which is what the field exists to avoid. The model is
pre-release with no production caller, so it is replaced in place.

Cleanup destroys the durable recovery record it commits against: `publish_tombstone` unlinks
the metadata, and finalization then removes the tombstone and its directory. A retried
`cleanup` commit therefore observes a *proven absent* record. The adapter treats that one
case — `cleanup` only, `FileNotFoundError` only, never a read that merely failed — as the
already-accounted cleanup, and performs no second provider mutation. Any other operation, and
any unproven absence, stays the bounded `provider_conflict` it is today.

Two residuals this decision does not close, both inherited from #2199's adapter and neither
made worse here. First, the `teardown` commit point also publishes a tombstone through
`cleanup()` and still has no finalizer, so teardown retains its tombstone directory. Second,
because cleanup destroys the record the post-commit observation reads, the service's
`observe` for a cleanup mutation classifies `unreadable` and journals a terminal outcome of
`conflict` — true before this change and unchanged by it. Both belong to the local adapter's
owner, not to the shared seam.

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
  `_anchor`'s `advance` runs inside the lane lock, but the provider call does not, so the
  check is not the tautology it looks like from inside one lane; without it the context can
  attest to a record that is no longer the head.
- **Compare the context against the bare lane head, whatever operation it belongs to.**
  verified: `acknowledge_takeover` anchors `TAKEOVER_SUPERSEDED` and `WATERMARK_INSTALLED`
  under the takeover request's own operation identity
  (`src/kdive/providers/external_boot_authority/service.py:628-639`) *before* awaiting
  `active.done` (`:695-696`), so the lane head legitimately moves past an in-flight mutation.
  An unscoped comparison would reject that overlap, which ADR-0584 explicitly designs for and
  which the `completion_binding` path (`:681-687`, `:888-895`) exists to serve.
