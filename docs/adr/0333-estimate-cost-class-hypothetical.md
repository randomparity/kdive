# ADR 0333 — `accounting.estimate` cost_class is a documented hypothetical

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-12
- **Deciders:** accounting / mcp-api

## Context

`accounting.estimate` prices a hypothetical selector — `{vcpus, memory_gb, window,
cost_class}` — with no target host. It resolves the pricing coefficient straight from
the caller-supplied `cost_class` (`resolve_coeff(conn, selector.cost_class)`), defaulting
to `"local"`. Actual usage, however, is billed under the **persisted** `cost_class` of
the Resource an allocation books (`ledger._cost_class` reads
`SELECT cost_class FROM resources WHERE id = …`). The two can diverge: an agent that
estimates against `"local"` but is later admitted onto a Resource carrying a different
class is billed under that class, not the one it priced (`BLACK_BOX_REVIEW.md` F8, verified
— estimated under `local`, billed under `homer`).

Two contract gaps compound this (both verified):

1. The `cost_class` field on `EstimateRequestPayload` has **no `Field` description**, so
   nothing in the agent-facing schema says the class is a caller-supplied hypothetical or
   that billing uses the Resource's persisted class.
2. The estimate handler's fail-closed `except ValueError` maps to a bare
   `configuration_error` with **no detail** naming which input was rejected. (The domain
   guards — `validate_size`/`validate_window`/`resolve_coeff` — already raise
   `CategorizedError` with field-level `details`; only this catch-all dropped detail.)

Two directions were on the table (issue #1099): (a) resolve the target Resource's real
cost class when the caller names a resource/allocation, or (b) document `cost_class` as a
caller-supplied hypothetical.

## Decision

We will **keep `cost_class` caller-supplied and document it as a hypothetical**, and we
will **enrich the estimate's fail-closed error path with field detail**.

- `accounting.estimate` is a read-only price with no target host, so it has no persisted
  class to resolve. Its `cost_class` field gains a `Field` description stating it is the
  hypothetical class the caller prices against, that actual billing uses the booked
  Resource's persisted class, and that a Resource's class is discoverable via
  `catalog.resources`. An unknown class stays a `configuration_error` (fail closed).
- The persisted-class resolution stays at **admission**, which already reads the chosen
  Resource's `cost_class` — that is the point at which a concrete Resource exists.
- The handler's catch-all `ValueError` branch now surfaces a `detail` (and, for a Pydantic
  `ValidationError`, the rejected field name(s) in `data.rejected_fields`), so a rejected
  estimate names what failed rather than returning an opaque `configuration_error`.

## Consequences

- The estimate's public contract is now honest about the hypothetical-vs-billed
  distinction without a breaking schema change: `cost_class` stays optional with the same
  `"local"` default, so every existing caller keeps working.
- An agent can price against the class of the Resource it intends to book by first reading
  that class from `catalog.resources`, closing the divergence in practice.
- No new resource/allocation selector is added to the read-side estimate, so the estimate
  stays a pure price with no host lookup. The tradeoff: the estimate cannot *guarantee* it
  matches the billed class — that guarantee remains admission's, where the Resource is known.
- The regenerated tool reference (`just docs`) now carries the `cost_class` description.

## Alternatives considered

- **Resolve the Resource's persisted class on the read-side estimate.** Rejected: the
  estimate carries no resource selector, so honoring this would mean adding a
  resource/allocation lookup to a pure price — a new, breaking surface for a `priority:low`
  clarity gap, duplicating admission's resolution. If the estimate ever grows a concrete
  resource reference, resolving and preferring that Resource's persisted class is the right
  follow-up; it is out of scope here.
- **Leave the error path as a bare `configuration_error`.** Rejected: an agent cannot act
  on an opaque failure. Mirroring the domain guards' field-level `details` costs little.
