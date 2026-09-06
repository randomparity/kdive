# Authority-owned external-boot preparation

Issue: #2246

Decision: [ADR-0608](../../adr/0608-run-external-boot-preparation-under-provider-authority.md)

## Outcome

Move materialize and prepare from server-side admission into the existing authority-bound activate
job. Preserve one immutable root authority, one serialized provider-host journal, and byte-for-byte
compatibility for existing version-1 wire and journal values.

## Contract

The boot payload stores one closed canonical `ExternalBootPlan`. Admission accepts a `preparing`
activation only when the plan's ownership and identity match durable activation, System, Run, and
resolved provider facts. It performs no provider operation.

After allocating and acknowledging the activate authority, the worker runs three exact operations:

1. `materialize` observes or creates immutable provider artifacts and commits the materialization
   receipt while the activation and job remain `preparing` and `running`.
2. `prepare` observes or captures the recovery point, commits it, and advances the activation to
   `prepared` while the job remains `running` and authority remains current.
3. `activate` uses the existing final result commit, which alone may complete the job and retire the
   authority.

Trusted SQL derives preparation operation identities and digests from the immutable root binding,
generation, exact plan identity, and operation discriminator. The current-binding resolver returns
the bound durable plan with those derived facts. The preparation request must carry that exact plan.
Ordinary mutation requests cannot select preparation operations, and preparation requests cannot
select any other operation.

The single journal head permits each ordinary operation lifecycle and only two cross-operation
edges: terminal materialize to admitted prepare, and terminal prepare to admitted activate, under
the same current root generation. Takeover, positive-quiescence, head-CAS, and recovery-object
ownership rules remain unchanged. No phase field is added to retained journal values.

Intermediate commits use a worker-only SQL boundary. It rechecks the active incarnation credential,
running exact job attempt and lease, current authority generation, derived phase binding, anchored
terminal record, plan, activation tuple, and closed receipt while holding the System lock. Exact
replay returns the stored result. Any mismatch or supersession writes nothing and starts no later
phase.

## Restart and failure behavior

A reclaimed job allocates a newer root generation and reconstructs the plan from its durable
payload. Existing activation receipts guide exact provider observation and prevent duplicate capture
or ownership replacement. After the journal authenticates a predecessor terminal receipt, the
provider atomically rebinds that receipt to the successor authority and phase identity without
repeating its provider operation. The fresh generation receives freshly derived digests;
old-generation resolver and commit calls are rejected. A failure after acknowledgement is bound to the current
phase and uses the guarded failure path. Neither failure nor cancellation can report an intermediate
phase as job success.

## Verification

Tests cover PREPARING admission and plan rejection, SQL phase derivation and privilege gates,
unchanged canonical vectors, closed preparation request parsing, sequential journal transitions,
intermediate commit state and replay, supersession at every provider-return/core-commit boundary,
restart from both receipts, cancellation pin lifetime, and final-only job completion and authority
retirement. Full installed provider advertisement and initial Run plan construction remain later
integration work.
