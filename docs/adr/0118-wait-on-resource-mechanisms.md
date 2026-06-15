# ADR 0118 — Wait-on-resource mechanisms: `allocations.wait`, queue position, derived `retryable` (#430)

- **Status:** Proposed
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0019](0019-tool-response-envelope.md) (the
  `ToolResponse` envelope — extended with one derived field, contract unchanged),
  [ADR-0023](0023-discovery-allocation-admission.md) (the allocation surface),
  [ADR-0036](0036-reservation-lease-semantics.md) (leases / renewal),
  [ADR-0069](0069-reservation-pending-queue-scheduler.md) (the FIFO pending queue this
  reads), [ADR-0070](0070-fleet-availability-system-reuse.md) (cross-project `queue_depth`
  disclosure
  precedent), [ADR-0113](0113-flat-tool-output-schema.md) (the flat advertised schema,
  unaffected).
- **Issue:** [#430](https://github.com/randomparity/kdive/issues/430).
- **Spec:** [`../design/wait-on-resource-mechanisms.md`](../design/wait-on-resource-mechanisms.md).

## Context

An agent that decides to wait for a resource should not burn context busy-polling, and
should have a hint to decide whether to wait at all (#430). Most of the machinery #430's
design notes call for already ships and is verified in source: durable-job async handles
with `jobs.wait` bounded long-poll, leases with TTL + `allocations.renew`,
server-as-source-of-truth recovery via `*.list`, idempotency keys, cancellation, the
reference-not-log-dump `ToolResponse` envelope, and a fleet `queue_depth` aggregate. The
spec's table maps each design note to its existing implementation.

Three gaps remain, all narrow: (1) a queued `requested` allocation has no bounded wait —
only repeated `allocations.get` across turns (the `jobs.wait` analogue is missing); (2) an
enqueued request carries no per-request queue position, only the fleet aggregate; (3)
failures carry an `error_category` but no retryable-vs-terminal signal, which #430 asks
for by name. This is not milestone- or epic-sized; it is one ADR + spec + the implementing
commits on one branch, closing #430. The deferred items (historical-timing ETAs, a
`kdivectl --watch`, MCP progress push) are recorded as out of scope, not sub-issues.

## Decision

We will close the three gaps at single insertion points in existing code. The only schema
change is one additive nullable column (Gap 1's failed-settle cause); the rest is new
tool, derived field, and a count query:

1. **`allocations.wait(allocation_id, timeout_s=30.0)`** — a read-only tool that mirrors
   `wait_job` exactly: poll `ALLOCATIONS.get` every `POLL_INTERVAL_S`, holding no
   connection while sleeping, returning when the allocation **leaves `requested`** (settles
   to `granted`/`released`/`failed`) or the clamped `≤ MAX_WAIT_S` deadline elapses. Auth,
   no-leak, malformed-id, and non-finite/non-positive-timeout behavior are identical to
   `allocations.get` / `wait_job`. Because the most important settle for a waiting agent is
   `failed` and `_envelope_for_allocation` today collapses every failed allocation to
   `infrastructure_failure` (the table stores no cause), we add **one additive nullable
   column `allocations.failure_category`**: the queued-terminate transitions (`_terminate`
   → `allocation_denied`, `_reap_one` → `queue_timeout`) persist the cause, the envelope
   reads it (NULL falls back to `infrastructure_failure`), so the derived `retryable`
   (decision 3) is correct on the wait path. Without it a budget-terminated queued request
   would report `retryable=true` and the agent would re-queue a request that can never be
   granted — the spin #430 exists to stop.

2. **Queue-position hint** — for a `requested` row only, `allocations.get` and
   `allocations.wait` surface `queue_position` (1-based FIFO rank among `requested` rows
   for the **same target** — by-id `requested_resource_id` or by-kind `requested_kind` —
   ordered `created_at, id`) and `queue_ahead` (`queue_position - 1`). It is an explicit
   **advisory hint**, not an ETA or guaranteed ordering, because promotion is
   work-conserving and per-host (`promotion.py`). `allocations.list` omits it (no N+1).

3. **Derived `retryable`** — a new `retryable: bool | None` field on `ToolResponse`,
   computed in the existing `_category_iff_failed` validator from a static
   `{ErrorCategory → bool}` table (`None` on success, a `bool` iff `error_category` is
   set). It is never caller-set, so it cannot drift from the category. The table is
   exhaustive over the enum (a test asserts equality of key sets) and **biased to
   terminal when transience is ambiguous**, since the flag exists to stop retry-hammering
   of permanent failures. The lone conflated category, `allocation_denied` (over-budget =
   terminal, host-cap-full = transient, split only by `data.reason`), is classified
   **terminal**: correct for the budget sub-case, and the capacity sub-case's right
   recourse is `on_capacity=queue` + `allocations.wait`, not blind-retrying a deny.

## Consequences

- A queued allocation becomes awaitable in one bounded call instead of a cross-turn poll
  loop, and the wait response carries the position hint, so an agent can decide
  wait-versus-pick-free with the data already in hand. This is the token-efficiency win
  #430 asks for.
- Every failure envelope now answers "should I retry?" uniformly across all planes, in
  one place the agent already reads. Adding the field touches: `ToolResponse`
  (`mcp/responses.py`), one `allocations.wait` handler + registrar entry, the
  `_envelope_for_allocation` path (position + failure-cause read), the new
  `failure_category` migration + its writes in `promotion.py`, and the regenerated
  agent-facing reference (`just docs`, the `docs-check` gate) plus the `test_tool_docs`
  mapping for the new tool. One additive nullable column is the only schema change; no
  dependency or auth-model change, and the advertised `outputSchema` stays flat (ADR-0113).
- New obligation: the classification table is now a maintained contract. A new
  `ErrorCategory` will fail the table-completeness test until classified — deliberate, so
  no category ships unclassified.
- The position query is one count per `get`/`wait` on a `requested` row, riding the
  `created_at` partial index `promote_pending` already uses (same-target + `id` are
  residual filters); negligible added load, and only on the queued path.
- Rollback is removal of the tool surface (drop the tool, the two keys, the field +
  validator clause) with no data to unwind. The migration runner is forward-only
  (ADR-0015), so the added `failure_category` column is left in place — a harmless nullable
  column once the envelope's read is reverted.

## Alternatives considered

- **Put `retryable` in per-op `data` instead of the envelope top level.** Rejected: it is
  cross-cutting agent control-flow with the same shape on every plane; ADR-0019's premise
  is "one envelope, one place to look." A `data` key would scatter it and make the agent
  branch on a per-tool location.
- **Store `retryable` at each failure construction site (an argument).** Rejected: ~15
  call sites, and the value is a pure function of `error_category` — storing it invites
  drift. Deriving it once in the validator is a single source of truth with zero call-site
  churn and a completeness test as the guardrail.
- **Split `allocation_denied` into budget vs capacity categories so `retryable` is exact.**
  Rejected here as out of scope: it would change the established error taxonomy and the
  admission/promotion `reason` plumbing (ADR-0007/0069) for a flag whose conservative
  terminal classification already steers the capacity case to the better affordance
  (queue + wait). If the taxonomy split is wanted, it is its own ADR.
- **An `estimated_seconds` ETA instead of (or beside) `queue_position`.** Rejected: a
  trustworthy ETA needs per-(kind, shape) historical timings — a new metrics subsystem.
  Position is honest with data we already hold; an ETA now would be a guess presented as a
  number. Deferred to its own ADR if demanded.
- **A single combined `resources.wait` over both jobs and allocations.** Rejected: jobs
  and allocations are distinct durable objects with distinct settle predicates and authz
  scoping; `jobs.wait` already exists. Mirroring it as `allocations.wait` keeps the
  per-object symmetry agents already learn (`jobs.*` / `allocations.*`), rather than
  inventing a cross-object tool.
- **Make `allocations.wait` mutating / lease-touching (e.g. auto-renew while waiting).**
  Rejected: waiting is a read. Renewal stays the explicit `allocations.renew` opt-in
  (ADR-0036); coupling a side effect into a poll would surprise the agent and complicate
  authz (the tool stays `viewer`, not `operator`).
