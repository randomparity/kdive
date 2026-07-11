# ADR 0186 — Pool selection axis for allocation requests (#561)

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** platform maintainers
- **Builds on (does not supersede):** [ADR-0023](0023-discovery-allocation-admission.md) (the
  by-kind first-available candidate resolution and per-host capacity gate),
  [ADR-0069](0069-reservation-pending-queue-scheduler.md) (the work-conserving FIFO promotion
  sweep and the persisted request-input columns a queued row re-admits from),
  [ADR-0068](0068-custom-config-pcie-modeling.md) (the PCIe narrowing layered on a candidate
  set), [ADR-0112](0112-systems-inventory-config.md) (the `systems.toml` → `resources` reconcile
  that writes `resource.pool`), [ADR-0180](0180-lifecycle-recovery-context.md) (the recovery
  context that echoes the requested selector).
- **Spec:** [`../superpowers/specs/2026-06-19-system-pools-design.md`](../archive/superpowers/specs/2026-06-19-system-pools-design.md)

## Context

An allocation request targets capacity by exactly one of `resource_id` (an exact host) or `kind`
(any host of a provider kind). Naming an exact host forces the agent to enumerate hosts and race
other agents for a specific row; every agent naively picks the first match and they collide on
it while identical hosts idle. By-kind already spreads load — the scheduler picks first-available
across the kind — but `kind` is too coarse to express "this named group of interchangeable
hosts." Operators want to carve a kind's hosts into named groups and let an agent target a group
without hand-picking a member (#561).

The `resources` table already has a NOT NULL `pool` string column, but it carries **no selection
semantics**: discovery sets it to a per-kind constant and the inventory reconcile sets it to
`'default'`. The first-available machinery is also already general — `_schedulable_candidates`
resolves a candidate set and the FIFO promotion sweep re-runs that resolution from persisted
inputs (ADR-0069). What is missing is a third way to *define the candidate set* and a request
field to select it.

## Decision

Add **pool** as a third allocation-request selection axis: a pool is a free-form string label on
resources (the existing column), and a request may target the **first available resource whose
`pool` matches**, reusing the existing candidate-resolution and promotion machinery unchanged.

**No new entity.** A pool is a label, not a row. There is no `pools` table, no pool lifecycle, no
pool-level metadata or quota — nothing in #561 needs them, and a label keeps one source of truth
(the resource row) and zero new lifecycle.

**Exactly one selector.** The request target becomes **exactly one of `{resource_id, pool,
kind}`**, enforced structurally by the existing payload discriminated union (`ResourceById |
ResourceByKind`) extended with a `ResourceByPool` variant. A pool is its own axis and is assumed
to group interchangeable (same-kind) hosts, so `pool`+`kind` would be redundant; rejecting it
keeps the validation and the test matrix small. Internally `AdmissionRequestSpec.kind` (today a
non-optional `ResourceKind` with a by-id default) becomes optional and gains `pool`; the three
sites that assume `kind` is always set become selector-aware — the `object_id` derivation, the
`requested_kind`-vs-`requested_pool` persistence (a queued pool row must not carry a bogus
`requested_kind`), and the no-resource denial detail. A **pool** no-resource denial returns a
**generic** detail and does **not** enumerate available pools: unlike the fixed global
`ResourceKind` enum (the by-kind `available_kinds`, ADR-0132), pool names are operator-chosen
free-form strings on resources that may be affinity-scoped (`affinity_allowlist` /
`owner_project`), so a `SELECT DISTINCT pool` would leak another project's private pool names
across the tenant boundary.

**Candidate resolution mirrors by-kind.** `placement.PlacementRequest` gains `pool: str | None`;
`_schedulable_candidates` gains a pool branch — `SELECT * FROM resources WHERE pool=%s AND
status='available' AND NOT cordoned ORDER BY created_at, id` — after which the same affinity
filter and the same PCIe narrowing apply. The by-id and by-kind branches are untouched.

**Two denial modes, symmetric with by-kind.** A pool naming **no resource at all** (catalog has
zero rows for that pool, ignoring transient status/cordon) is a **configuration** denial — the
pool name is operator config and waiting will not conjure a host. A pool with members that are
all momentarily busy or `cordoned` is a **capacity** denial: with `on_capacity=queue` the request
rests in `REQUESTED` exactly as a by-kind capacity denial does. (Cordon is transient, an unknown
pool name is not — so an all-cordoned pool queues rather than hard-denies.)

**Queue + promotion reuse.** A queued pool request persists its pool in a new nullable
`requested_pool` column (migration 0045), mirroring `requested_kind` (ADR-0069). The promotion
sweep re-resolves candidates from `requested_pool` through the same `PlacementRequest` path, so a
freed pool member is filled on the next reconciler pass with **no pool-specific sweep code**.

**Declaring pools.** The `systems.toml` instance models gain an optional `pool` field; the
reconcile writes it to `resources.pool` on insert and overlays it on update (absent →
`'default'`). The imperative `resources.register_*` path exposes the same optional `pool`. A
resource's pool is operator-assigned; it is not derived from kind.

## Consequences

- An agent can request `pool=P` and get the first available member without reading the catalog or
  racing on a specific row; the repo tool spreads allocation across the pool. This moves host
  selection out of the agent's hands (the #561 ask).
- Pools are purely additive: existing by-id and by-kind requests are unchanged, and a resource
  with no declared pool keeps `'default'` and behaves as before.
- The migration adds one nullable `requested_pool` column to `allocations` and (optionally) a
  light CHECK consistent with the ADR-0069 `resource_id`-nullable style; the exactly-one-target
  invariant among `requested_resource_id` / `requested_kind` / `requested_pool` is enforced at the
  service layer (as 0016 did for `requested_kind`), with the migration kept minimal.
- **Mixed-kind pools are the operator's responsibility, not enforced.** Selection hands back
  whichever member frees first and the provider dispatches by that resource's own kind; a run with
  a `target_kind` constraint that binds an off-kind granted System fails at `runs.bind`'s existing
  `target_kind` check (the backstop). Pools are documented as "same-kind, interchangeable."
- `allocations.get`/`list` echo `requested_pool` (extending ADR-0180), so a queued/granted
  allocation's targeted pool is visible for recovery.

## Alternatives considered

- **First-class `Pool` entity/table.** Would carry pool metadata, membership, and lifecycle.
  Rejected — nothing in #561 needs pool-level state; a label on the resource row is sufficient and
  avoids a second source of truth and a new reconcile/CRUD surface.
- **`pool` narrows `kind` (allow both together).** More flexible (candidates matching both), but
  redundant for same-kind pools and doubles the selector-combination matrix; rejected for the
  exactly-one-selector model.
- **Enforce single-kind-per-pool at registration/reconcile.** A stronger invariant, but adds a
  cross-row validation pass and a new failure mode for a contract the catalog already trusts for
  by-kind selection; rejected — documented operator responsibility with the `runs.bind` backstop.
- **A pool-specific promotion sweep.** Unnecessary — the ADR-0069 sweep is already candidate-set
  agnostic; re-resolving from `requested_pool` reuses it whole.
