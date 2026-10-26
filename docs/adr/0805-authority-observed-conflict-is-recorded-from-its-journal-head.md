# 0805 — An authority-observed conflict is recorded from its journal head

## Status

Accepted (2026-10-21)

## Context

[ADR-0583](0583-external-run-boot-uses-prepared-recovery-points.md) parks an external-boot
activation in `recovery_conflict` when the provider holds a third or unowned state, and
`systems.resolve_external_boot_conflict` resolves it from there.
[ADR-0584](0584-provider-host-authority-fences-external-boot-mutations.md) says a host
administrator's out-of-band change is detected as an unexpected identity and enters conflict.
No production path did this (#3327):

- The provider coordinators refuse a third identity before they write. For example, recovery
  requires the target domain XML before it stops the domain. The authority sends every such
  refusal as `provider-conflict`. The worker retries it, and
  [ADR-0714](0714-repeated-provider-conflict-ends-the-external-boot-job.md) ends the job `failed`
  on the second identical failure.
- When the authority did return a `conflict` or `mixed` observation, the worker refused it as a
  terminal configuration error.
- `ExternalBootActivationRepository.record_conflict` wrote the state directly, but the worker role
  has only `SELECT` on the activation tables (migration 0121), and nothing called it.

## Decision

1. **The authority answers a conflict it can prove.** When an `activate`, `recover` or
   `resolve-conflict` provider call is refused after `mutation-started`, the authority observes
   the provider again.
   - If the category is `conflict`, it journals `provider-returned`, `observed` and `terminal`
     with outcome `conflict`, and returns that observation.
   - Otherwise it leaves the operation at `mutation-started` and answers `provider-conflict` as
     before. This covers `source`, `target`, `mixed` and `unreadable`, and a failed observation.
   - A refusal raised by a proof-barrier checkpoint, and a `superseded` or other bounded verdict,
     are not observed again.
2. **`mixed` after a refusal stays retryable.** The coordinators resume by recorded phase, so
   a transient failure between the module and definition writes leaves a `mixed` state that the
   next attempt completes. That is ADR-0583's "mixture proven by durable evidence". The adapter's
   `mixed` category does not consult that evidence, so treating it as a conflict would park a
   recoverable activation.
   The operator chose this narrowing over also parking `mixed` (2026-10-21).
3. **The worker carries a conflict observation to a dedicated commit.** A `conflict` or `mixed`
   observation returned by any of those calls, or by a release's derived recover phase, becomes a
   conflict result rather than terminal evidence. The worker commits it through
   `record_external_boot_conflict_from_head` (migration 0178).
4. **Core records the conflict only from the authority's terminal journal head.** The function
   runs as `SECURITY DEFINER` for the worker role under the System advisory lock. It applies only
   when all of these hold:
   - the worker incarnation is active, the authority is `current` for this job and attempt, and
     the job's lease is live;
   - the published journal head is this authority's generation and is `terminal` for the
     authority's operation (for a release, its derived `recover` phase), with outcome `conflict`;
   - the evidence's observation id and composite state are exactly the head's;
   - the activation is `activating`, or `recovering` with a current `recovering` attempt no newer
     than this generation.

   It then moves the activation to `recovery_conflict` and records the evidence on a conflict
   attempt: a new attempt from `activating`, or the current attempt from `recovering`. It fails
   the job with category `conflict`, retires the authority and writes an audit row. It leaves the
   Run alone. Any other input returns `superseded` and writes nothing.
5. The unused server-side `record_conflict` is removed. No production path other than the
   authority's journal puts an activation into `recovery_conflict`. The repository's
   `finish_recovery_attempt` still accepts a conflict state without journal evidence; it has no
   caller in `src/`, and removing that branch is separate work.

## Consequences

- A host administrator's change to an active external boot now ends its release, recovery or
  activation job `failed` with category `conflict`. The activation stays in `recovery_conflict`
  and keeps its reservation and recovery evidence. The operator resolves it with
  `systems.resolve_external_boot_conflict`. `systems.teardown` and `ops.force_teardown` do not
  yet complete from `recovery_conflict`: on the local authority lane the teardown mutation is
  refused as `provider-conflict`, its journal head stays `mutation-started` and the System stays
  `ready`. That gap is not owned by any record yet.
- Such a refusal costs one generation instead of the two that ADR-0714's repeat check needed.
  Refusals over any other observed state still follow ADR-0714 unchanged.
- The conflict evidence's composite is the value `resolve_external_boot_conflict` needs as
  `observed_identity`. No MCP read exposes it yet (#3331).
- Resolution still restores only through the provider's ordinary recovery. Restoring from an
  acknowledged third identity is #3330.
- `unreadable` never becomes a conflict here. ADR-0583's promotion of an unreadable recovery to
  `recovery_conflict` at its deadline is still unimplemented.
- A conflict found only while the authority finishes an earlier generation's unresolved
  operation is journaled against that earlier generation. The current request still answers
  `provider-conflict`, so that path still ends through ADR-0714.
- Preparation (`preparing`, `prepared`) and teardown refusals are unchanged.

## Considered & rejected

- **Call `record_conflict` from the worker.** verified: migration 0121 grants the worker role
  only `SELECT` on `external_boot_activations` and `external_boot_recovery_attempts`. Granting
  writes would let the worker set lifecycle state without an authority fence.
- **Add a `conflict` operation to `commit_external_boot_authority_result`.** judgment: that
  function is patched in place by string replacement across many migrations. A separate function
  that follows the derived-release `_from_head` functions is smaller and keeps the head check
  beside the write.
- **Trust the worker's observation without the journal head.** judgment: the core role must not
  forge a provider acknowledgement (ADR-0584). The head is the authority's own record of what it
  observed.
- **Also park a refusal re-observed as `mixed`.** judgment: Decision 2.
- **Record `preparing` and `prepared` conflicts too.** verified: those states are refused
  through preparation and the `deadline` commit, not through the mutation path that this changes,
  so the branch would have no producer.
