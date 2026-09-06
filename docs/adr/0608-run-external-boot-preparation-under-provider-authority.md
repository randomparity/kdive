# 0608 — Run external-boot preparation under provider authority

## Status

Accepted (2026-09-06)

## Context

External-boot materialization and preparation mutate provider-host storage and guest state. The
existing admission helper performs those phases on the server before enqueueing the authority-bound
activate job. Its database System lock does not supply ADR-0584's provider-host mutation fence, and
a worker restart cannot recover the exact plan unless that plan is durable.

Authority bindings, operation identities, and operation digests are immutable. The provider-host
journal also binds terminal replay to an exact operation identity. Treating materialize and prepare
as enum aliases of activate would therefore either fail closed or weaken the binding.

## Decision

One activate job and one authority generation own the ordered materialize, prepare, and activate
commit points. The job stores the closed canonical external-boot plan. Admission from `preparing`
validates that plan against the activation, System, Run, and resolved Resource without invoking a
provider.

Trusted SQL derives each preparation operation identity and digest from the complete immutable root
authority binding, exact plan identity, generation, and operation discriminator. No child binding
table or mutable phase counter is stored. A preparation resolver returns that derived binding and
the exact plan from the bound job payload only for the authenticated active worker, current root
generation, job attempt, and lease.

Materialize and prepare use a distinct closed mutation request carrying the plan. Existing takeover,
ordinary mutation, and journal wire values retain their exact version-1 canonical bytes. The
operation field distinguishes journal phases; no new field is added to retained journal records.
The one journal head admits only terminal materialize to admitted prepare, then terminal prepare to
admitted activate, within the current root generation.

A worker-only intermediate commit rechecks the credential, job lease, System lock, current root and
derived phase binding, anchored terminal journal observation, plan, activation state, and receipt.
Materialize stores its receipt while leaving the activation `preparing`; prepare stores its receipt
and advances it to `prepared`. Both leave the job running and authority current. Only the existing
final activate commit may finish the job and retire authority. Exact receipt replay is idempotent;
identity, plan, generation, state, or evidence disagreement writes nothing.

## Consequences

Provider mutation starts only after authority allocation and acknowledgement. A successor worker can
reconstruct the plan and exact phase bindings from durable state, observe committed receipts, and
resume without retagging provider objects. Supersession before an intermediate commit prevents the
old generation from advancing. Preparation failures use the guarded authority failure path and
cannot manufacture success.

The additional plan increases the bounded boot-job payload. Existing operations, canonical request
vectors, and retained journal hashes remain compatible because their wire shapes do not change.
Initial Run plan construction and production provider advertisement remain separate integration
work.

Graceful shutdown first stops request admission, then drains completion-owned mutations before it
closes the provider adapter. Caller cancellation, including cancellation of the shutdown waiter,
does not cancel work that crossed the mutation boundary. The service manager's stop timeout is an
outer operational bound, not evidence of completion: a hard stop can leave a nonterminal journal,
which the next authority incarnation must recover before accepting a successor mutation.

## Considered & rejected

- **Prepare on the server before enqueue.** A database lock does not establish provider-host
  mutation authority.
- **Allocate one authority per preparation phase.** This needlessly consumes generations and makes
  one operation depend on mid-job authority replacement.
- **Store derived child bindings.** Their contents are deterministic from immutable trusted state.
- **Add a phase field to version-1 wire and journal values.** Even a default changes canonical bytes
  and invalidates retained hashes.
