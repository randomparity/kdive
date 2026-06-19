# Design: System pools — first-available allocation across interchangeable resources (#561)

- **Date:** 2026-06-19
- **Issue:** [#561](https://github.com/randomparity/kdive/issues/561) (status:needs-design)
- **Related:** [#395](https://github.com/randomparity/kdive/issues/395) (remote-libvirt per-op
  resource selection — folded into this work)
- **ADRs:** ADR-0186 (pool selection axis), ADR-0187 (remote-libvirt de-singletoning)

## Problem

Today an agent acquires capacity by naming either an exact host (`resource_id`) or a provider
`kind`. Naming an exact host means the agent must read `systems.list`/`resources.availability`,
pick a host, and race other agents for it — every agent naively picks the first matching host,
so they collide and serialize on the same row while other identical hosts sit idle. Naming a
`kind` already spreads load (the scheduler picks first-available across the kind), but `kind` is
too coarse: an operator cannot carve a kind's hosts into named groups (e.g. "the three big
remote boxes" vs. "the small CI box") and let an agent target one group without hand-picking a
member.

The motivating case (issue comment): three already-provisioned, interchangeable remote-libvirt
hosts (`ub26-big`, `fed44-big`, `rock10-big`) cannot even be *registered* together — the
remote-libvirt provider is hard-singleton: the inventory parser rejects more than one
`[[remote_libvirt]]` block, and the per-op connection resolver fails closed on more than one
instance because **the per-op call path carries no resource identity** and cannot pick which
host to talk to. So pooling remote hosts is blocked on first threading
allocation → resource → instance identity through the per-op path (#395).

## Goals

1. An agent can request "the first available system from pool *P*" without naming a host. The
   repo tool — which sees all resources at all times — does the selection, spreading allocation
   across the pool.
2. Operators can group interchangeable hosts into named pools declaratively (in `systems.toml`)
   or via the existing imperative registration path.
3. The three remote-libvirt hosts above can be registered and pooled: the per-op path resolves
   the connection identity of the *granted* host, not "the only" host.

## Non-goals

- No priority, preemption, future-window booking, or backfill — those remain the full-scheduler
  ADR series (ADR-0069 already deferred them). Pools reuse the existing work-conserving FIFO
  sweep unchanged.
- No new first-class `Pool` entity/table. A pool is a label on resources (the column already
  exists). YAGNI: nothing in the issue needs pool-level metadata, quotas, or lifecycle.
- No enforcement that a pool's members share one provider kind. "Identically configured" is the
  operator's contract; the catalog still carries each resource's own kind for dispatch, and
  downstream `runs.bind` `target_kind` checks still apply (see Risks).

## Background: what already exists (and is reused verbatim)

- **`resources.pool`** — a NOT NULL string column on every resource row
  (`domain/catalog/resources.py`). Set today to a per-kind constant by discovery
  (`pool="remote-libvirt"` etc.) or to `'default'` by the inventory reconcile
  (`inventory/reconcile_resources.py`). It carries **no selection semantics** today.
- **First-available selection** — `services/allocation/admission/placement.py`
  `_schedulable_candidates` already resolves a candidate set by `kind`
  (`SELECT * FROM resources WHERE kind=%s AND status='available' AND NOT cordoned ORDER BY
  created_at, id`) and layers affinity (`_affinity_ok`) + PCIe matching on top. A by-id request
  returns the single host.
- **Work-conserving FIFO promotion** — `services/allocation/promotion.py` re-runs selection from
  persisted request inputs and per-host promotes the oldest *placeable* queued request
  (ADR-0069). Queued rows persist `requested_kind` / `requested_resource_id` /
  `requested_pcie_specs` (migration 0016) and rest in `REQUESTED` with `resource_id` NULL.
- **Resource ↔ inventory identity** — the reconcile (ADR-0112) keys config-owned resources on
  `(kind, name)`, writing `resource.name = [[remote_libvirt]].name`
  (`inventory/reconcile_resources.py`). So a remote resource row's `name` *is* its inventory
  instance name — the natural per-op resolution key.
- **A per-op config DI seam** — ~16 remote-libvirt modules (provisioning, connect, install,
  control, build_vm, debug/introspect, retrieve, reaping, transport_reset, diagnostics) take
  `config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_inventory`. This is the
  single seam the de-singletoning changes.

## Part A — Pool selection axis (ADR-0186)

### A.1 Selector model

`allocations.request` gains an optional `pool: str`. The target selector becomes **exactly one
of `{resource_id, pool, kind}`**; supplying zero or more than one is a `configuration_error`
returned before admission (today the payload already requires exactly one of `resource_id` /
`kind`, so this extends the existing mutual-exclusion check with a third arm). Rationale (locked
in design review): a pool is its own selection axis; a pool is assumed to group interchangeable
(same-kind) hosts, so `pool`+`kind` would be redundant and only adds validation/test surface.

### A.2 Candidate resolution

`placement.PlacementRequest` gains `pool: str | None`. `_schedulable_candidates` gains a `pool`
branch that mirrors the kind branch exactly:

```sql
SELECT * FROM resources
WHERE pool = %s AND status = 'available' AND NOT cordoned
ORDER BY created_at, id
```

Then the same affinity filter and (if PCIe specs present) the same PCIe narrowing apply. The
by-id and by-kind branches are unchanged. Resolution precedence inside `_schedulable_candidates`
is: explicit `resource_id` first, then `pool`, then `kind` (only one is ever non-None by A.1).

### A.3 Admission + queue

- `services/allocation/admission/request.py` threads `pool` from the payload into the
  `AdmissionRequestSpec` and into the `PlacementRequest`.
- A pool request that finds **zero resources in the pool** (the pool name matches no row, or all
  members are permanently ineligible) is a **configuration** denial — symmetric with a by-kind
  request whose kind has no configured resource (the pool name is operator config, and waiting
  will not conjure a host). A pool request that finds members but none currently *fit* (all busy
  / over capacity) is a **capacity** denial → with `on_capacity=queue` it rests in `REQUESTED`,
  exactly like a by-kind capacity denial.

  Decision detail: "zero resources in the pool" is evaluated against the catalog **ignoring
  transient availability** (status/cordon), so a pool whose every member is momentarily
  `cordoned` still *queues* (capacity) rather than hard-denying (configuration) — cordon is
  transient, an unknown pool name is not. This mirrors the kind path, where a kind with rows but
  no free capacity queues.

- Queued rows persist the pool in a new nullable `requested_pool` column (migration 0045),
  mirroring `requested_kind`. The promotion sweep re-resolves candidates from `requested_pool`
  via the same `PlacementRequest` path, so a freed pool member is filled on the next reconciler
  pass with no pool-specific sweep code.

### A.4 Declaring pools

- **Declarative (systems.toml):** the inventory instance models (`RemoteLibvirtInstance`,
  `FaultInjectInstance`, `LocalLibvirtInstance`) gain an optional `pool: str` field. The
  reconcile writes it to `resources.pool` (replacing the hardcoded `'default'` on insert, and
  overlaying it on update so a pool re-assignment propagates). Absent → `'default'`.
- **Imperative:** `resources.register_remote_libvirt` (and siblings) already accept a `pool`
  argument path via the resource row; confirm it is settable there. (If the current registrar
  hardcodes a pool, expose the optional `pool` field — additive.)

### A.5 Observability

`allocations.get`/`list` already echo the requested selector (ADR-0180). Extend the recovery
context to echo `requested_pool` when set, so an agent can see which pool a queued/granted
allocation targeted. `resources.availability` / `systems.list` group output is out of scope
(the pool column is already on the row; richer pool-rollup reporting can be a follow-up).

## Part B — Remote-libvirt de-singletoning (ADR-0187, closes #395)

### B.1 Per-resource config resolution

Add `remote_config_for_resource(resource_name: str) -> RemoteLibvirtConfig` to
`providers/remote_libvirt/config.py`: it loads the `[[remote_libvirt]]` instances and selects the
one whose `name == resource_name` (instead of requiring exactly one). Zero matches →
`CONFIGURATION_ERROR` naming the missing instance; the existing per-instance validation
(`validate_remote_uri`, `_parse_gdbstub_range`) is applied to the selected instance unchanged.

`remote_config_from_inventory()` (no identity) is retained **only** for the genuinely
host-agnostic callers that enumerate or operate process-wide — discovery (enumerates all),
and the console-hosting bootstrap (see B.4). `_require_single_instance` / the singleton guards
that exist purely because per-op selection was unwired are removed.

### B.2 Threading resource identity to the op

Per-op modules change their seam from `config_factory: Callable[[], RemoteLibvirtConfig]` to
resolve by the resource the op targets. The op already operates on a `System`, and
`System → Allocation → Resource` is a persisted chain (`system.allocation_id`,
`allocation.resource_id`). The worker job handler that dispatches a remote-libvirt op resolves
the bound resource's `name` and passes it to the provider entry point, which constructs the
config via `remote_config_for_resource(name)`. The DI seam becomes
`config_factory: Callable[[str], RemoteLibvirtConfig] = remote_config_for_resource` (the
parameter is the resource name), keeping every module unit-testable with an injected fake.

Build-VM / ephemeral-build paths key off the `BuildHost` identity rather than a System; they
resolve config by the build host's resource name the same way.

### B.3 Relax the inventory singleton guard

`inventory/model.py` `_check_remote_libvirt_singleton` is removed (or relaxed to a uniqueness
check on instance `name`, which the reconcile already requires for `(kind, name)` identity).
Discovery enumerates all declared instances and registers one resource per instance (it is
already bind-only/non-creating in Phase 2; the reconcile is the sole creator and already
iterates `doc.remote_libvirt`, so multi-instance creation works once the parser admits it).

### B.4 Console hosting (the one genuinely process-wide caller)

`build_console_hosting` runs a single-leader loop that hosts **all** running systems and today
opens every console with one `remote_config`. With multiple remote hosts, the console collector
factory must resolve config **per system**: inside `factory(system_id)`, look up the system's
bound resource name and call `remote_config_for_resource(name)` to open that system's console on
its own host. This is the one place that moves from "resolve once at bootstrap" to "resolve per
system."

## Data flow (pool request, happy path)

```
agent → allocations.request(pool="big-remote", on_capacity=queue)
      → request.py: AdmissionRequestSpec(pool="big-remote")
      → placement.resolve_placement_candidates(PlacementRequest(pool=...))
          SELECT … WHERE pool='big-remote' AND available AND NOT cordoned ORDER BY created_at,id
          → affinity filter → PCIe narrow
      → admit() per candidate (PROJECT→RESOURCE lock, check-then-debit)
          fit  → grant: stamp resource_id, REQUESTED→GRANTED, ledger reserve, lease
          none → on_capacity=queue: persist requested_pool, rest in REQUESTED
reconciler sweep → promote_pending(): re-resolve from requested_pool → first freed member grants
worker op on the granted System → resource.name → remote_config_for_resource(name) → that host
```

## Testing strategy

- **Placement (unit):** pool branch returns members ordered FIFO; respects status/cordon;
  affinity excludes disallowed scoped members; PCIe narrowing applies; unknown pool → empty set.
- **Admission (unit/service):** exactly-one-selector validation (0, 2, 3 selectors → config
  error); unknown pool name → configuration denial; pool with busy members + `on_capacity=queue`
  → REQUESTED with `requested_pool` persisted; all-cordoned pool → queues, not config-denies.
- **Promotion (service/adversarial):** queued pool request promoted to the first freed member;
  two queued pool requests race a single freed slot under the PROJECT→RESOURCE lock (no
  double-grant) — extend the existing `tests/adversarial` promotion races.
- **Migration 0045:** `requested_pool` nullable; CHECK (if any) allows NULL for non-pool
  requests; migration-version-list assertions updated (memory: there are multiple such
  assertions, incl. a "backfills" test — grep them).
- **Reconcile (unit):** instance `pool` field writes `resources.pool` on insert and overlays on
  update; absent → `'default'`.
- **Remote config (unit):** `remote_config_for_resource` selects by name from N instances;
  unknown name → config error; validation still fires on the selected instance.
- **De-singletoning (unit/integration):** parser accepts N `[[remote_libvirt]]` blocks; reconcile
  creates N rows; a per-op module with two instances resolves the correct host by injected
  resource name; console factory resolves per system.
- **Boundary/error paths:** empty pool string; pool + kind both set; pool naming a kind value;
  resource whose allocation has NULL resource_id (queued) never reaches a remote op.

## Open risks / decisions

- **Mixed-kind pool (accepted).** Nothing forbids an operator putting a local and a remote host
  in one pool. Selection would then hand back whichever frees first, and the provider dispatches
  by the resource's own kind — correct, but a run with a `target_kind` constraint could then fail
  to bind to a granted off-kind System. We document pools as "same-kind, interchangeable" and let
  the existing `runs.bind target_kind` check be the backstop. Not enforced (design decision).
- **`requested_pool` vs. `requested_kind` coexistence.** A queued row carries exactly one
  non-NULL target column among `requested_resource_id` / `requested_kind` / `requested_pool`. The
  migration adds a CHECK (or the promotion reader tolerates) consistent with the existing
  resource_id-nullable CHECK style (ADR-0069). Decide in the plan whether to add a 3-way XOR
  CHECK or keep it a service-layer invariant (lean: service-layer + a light CHECK, matching how
  0016 handled requested_kind without a XOR CHECK).
- **Console per-system resolution cost.** Resolving config per system parses `systems.toml` per
  console open. Acceptable (console opens are infrequent and the doc is small); if it shows up,
  cache the parsed doc per sweep. Noted, not pre-optimized.
