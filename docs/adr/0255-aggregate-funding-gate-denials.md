# ADR 0255 — Aggregate the quota + budget funding gates into one allocation denial (#833)

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0245](0245-alloc-denial-remedy-breadcrumbs.md) (the
  remedy breadcrumb that deferred this collapse as needs-design), #838 (budget denial echoes
  its figures), #841 (role-aware remedies), [ADR-0069](0069-reservation-pending-queue-scheduler.md) (the
  queueable-vs-terminate routing the promotion sweep shares),
  [ADR-0007](0007-metering-budgets-admission.md) (fail-closed funding invariants).
- **Issue:** [#833](https://github.com/randomparity/kdive/issues/833).
- **Spec:** [`../superpowers/specs/2026-06-26-aggregate-funding-gates-833.md`](../archive/superpowers/specs/2026-06-26-aggregate-funding-gates-833.md).

## Context

A fresh project's first `allocations.request` fails closed on **two sequential funding gates**:
`quota_exceeded`, then — after `accounting.set_quota` — `allocation_denied / budget_exceeded`,
then it succeeds on the third try. `admission_gate` is a short-circuit chain (affinity → quota
→ budget → host-cap → PCIe), so a denial names only the first unmet gate. ADR-0245 + #838 +
#841 made each single denial actionable (it names the remedy tool and the budget shortfall),
but the caller still discovers the second unmet gate only after fixing the first — two
avoidable round-trips. ADR-0245 explicitly deferred the single-denial collapse as needs-design;
this is that design.

The gate is **shared**: synchronous `admit` and the reconciler promotion sweep
(`services/allocation/promotion.py`) both replay `admission_gate`. The sweep's terminate-vs-wait
decision keys off the **single-category** short-circuit denial — a `budget_exceeded`
`ALLOCATION_DENIED` terminates a queued request (waiting frees no budget); a quota / host-cap
denial waits. Any aggregation must not perturb that routing.

## Decision

Aggregate the two **project-funding** gates (quota + budget) into one denial **on the
synchronous request path only**, as a presentation enrichment that leaves the gate's routing
contract untouched.

1. **The gate keeps its short-circuit routing.** `admission_gate` still returns the first
   unmet gate's denial with its existing `category` / `reason` / `queueable`. The promotion
   sweep is therefore byte-for-byte unchanged and never reads the aggregated detail.
2. **The synchronous path enriches a funding denial** (`quota_exceeded`, or
   `allocation_denied` with `reason=budget_exceeded`) with `details["unmet"]`: the list of
   *every* unmet funding gate (quota first, then budget), each carrying its current/required
   figures. A new `funding_unmet(conn, project, estimate)` re-reads quota and budget under the
   already-held `PROJECT` lock (the cold denial path only), mirroring how #838 re-reads the
   budget snapshot rather than widening the gating predicates' bool contract.
3. **The top-level `error_category` stays the gate's primary** (onboarding order: quota before
   budget). No new error category is introduced; the complete picture rides `data["unmet"]` +
   `suggested_next_actions` + `detail`.
4. **The MCP layer owns the remedy tool names.** `funding_unmet` emits a transport-neutral
   `gate` discriminator; `mcp/tools/lifecycle/allocations/request.py` maps gate → remedy tool
   (`accounting.set_quota` / `accounting.set_budget`), surfaces it in `data["unmet"]`, leads
   `suggested_next_actions` with every unmet remedy for an admin caller (a non-admin keeps
   `["allocations.list"]`, #841), and composes the role-aware `detail` from the entries.
5. **Remove the #838 flat budget detail keys** (`estimate_kcu` / `limit_kcu` / `spent_kcu` /
   `budget_remaining_kcu`). The budget figures now ride the uniform `unmet` budget entry —
   `replace, don't deprecate`, no redundant dual representation of the same figures.

`unmet` entry shapes and the omit-figures-when-no-row rule are in the spec. kcu figures are
stringified `Decimal`; allocation counts are JSON integers. No schema or migration change — the
change is entirely read-path.

## Consequences

- A fresh project provisions quota **and** budget from a single denial — the two-detour
  onboarding trap collapses to one round-trip.
- The promotion sweep, the queue path, and the all-or-nothing no-write-on-denial invariant are
  unchanged; the gate's routing semantics are preserved deliberately.
- `data["unmet"]` is a nested array (allowed by the `JsonValue` envelope contract); agents read
  a uniform, machine-parseable list instead of category-specific flat keys.
- A both-unmet denial reports `error_category=quota_exceeded` even though budget is also unmet;
  the category is the gate's primary and the aggregate lives in `data`/`detail`/next-actions.
- The cold denial path does one extra quota read and one extra budget read; the hot grant path
  and the reconciler are untouched.

## Considered & rejected

- **A new aggregate error category** (e.g. `funding_not_provisioned`). Rejected: it adds a
  category to the surface-wide contract (and a `retryable` mapping), breaks the one-category
  typed-response model, and buys nothing the `unmet` array does not already carry.
- **Aggregating inside `admission_gate` for all callers.** Rejected: it would either change the
  promotion sweep's single-category routing (waiting-frees-no-budget terminate logic) or add
  two DB reads to every reconciler denial. Enriching only the synchronous path keeps the gate's
  contract and the reconciler lean.
- **Evaluating budget even when quota fails, then routing terminate on budget.** Rejected: it
  changes promotion behaviour (a both-unmet queued row would terminate instead of waiting for a
  quota slot to free) — out of scope and a regression for the queue UX.
- **Keeping the #838 flat keys alongside `unmet`.** Rejected: the same budget figures in two
  shapes in one payload is exactly the redundancy the project's standards forbid.
- **Surfacing the queue path's diagnostic too.** Rejected: an enqueued request returns a queued
  allocation, not a denial; the aggregate is an onboarding diagnostic for the deny path.
