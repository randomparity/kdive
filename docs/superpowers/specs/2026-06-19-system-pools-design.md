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
of `{resource_id, pool, kind}`**. Exactly-one is already enforced at the payload by a
**discriminated union** — `payload.resource` is one of `ResourceById | ResourceByKind`
(`mcp/tool_payloads.py`); we add a third variant `ResourceByPool`, so a request structurally
carries exactly one selector and no new validation arm is needed at the payload. Rationale
(locked in design review): a pool is its own selection axis; a pool is assumed to group
interchangeable (same-kind) hosts, so `pool`+`kind` would be redundant and only adds
validation/test surface.

**Internal refactor — `kind` becomes optional (do not understate this).** The internal
`AdmissionRequestSpec.kind` is a **non-optional** `ResourceKind` today, populated with a default
even for a by-id request (`_spec_from_payload` sets `kind = ResourceByKind().kind`). Adding pool
makes the *selector* genuinely tri-state, so `AdmissionRequestSpec` gains `pool: str | None` and
`kind` becomes `ResourceKind | None`. Three sites in
`services/allocation/admission/request.py` assume `kind` is always meaningful and **each must
become selector-aware**:

- `object_id = … else spec.kind.value` (request.py:71) — derive the object id from whichever
  selector is set (`resource_id` → its str, `pool` → the pool name, `kind` → its value).
- `requested_kind = None if resource_id … else spec.kind` (request.py:108) — must persist
  `requested_kind` **only** for a by-kind request and `requested_pool` only for a by-pool request,
  so a queued pool row does not carry a bogus `requested_kind` that corrupts promotion
  re-resolution.
- `available_kinds` / `_no_resource_response` (request.py:88 and the handler's
  `_no_resource_response`) — the "available kinds: …" denial detail is kind-specific and is set
  **only** on a by-kind denial. A **pool** no-resource denial leaves `available_kinds` `None` and
  returns a **generic** detail (e.g. `no schedulable resource in pool {pool!r} is registered`)
  with **no pool enumeration**. We deliberately do **not** enumerate available pools: unlike the
  fixed global `ResourceKind` enum (`_registered_kinds`, ADR-0132), pool names are operator-chosen
  free-form strings on resources that may be **affinity-scoped** (`affinity_allowlist` /
  `owner_project`), so a `SELECT DISTINCT pool` would leak another project's private pool names
  across the tenant boundary. A pool name is operator config the agent already holds; the denial
  does not need to echo the catalog. (By-id and by-kind details are unchanged.)

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

For the host-agnostic callers that genuinely operate over the whole fleet, add
`all_remote_configs() -> list[RemoteLibvirtConfig]` (validates and returns every declared
instance). `remote_config_from_inventory()` and `_require_single_instance` /
`_resolve_instance` (the guards that exist purely because per-op selection was unwired) are
**deleted** — no caller is left resolving "an arbitrary single instance" (with N hosts that
silently hits the wrong host). Every site moves to `remote_config_for_resource(name)` (identity)
or `all_remote_configs()` (enumerate-all); see B.5.

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

### B.4 Console hosting (a process-wide loop that resolves per system)

`build_console_hosting` runs a single-leader loop that hosts **all** running systems and today
opens every console with one `remote_config`. With multiple remote hosts, the console collector
factory must resolve config **per system**: inside `factory(system_id)`, look up the system's
bound resource name and call `remote_config_for_resource(name)` to open that system's console on
its own host. The loop's process-wide singletons (leader lock, event loop, host pool) stay
bootstrap-resolved; only the per-console open moves to per-system resolution.

### B.5 Caller classification — every `remote_config_from_inventory` / `config_factory` site

The per-op DI seam is not the whole story: several callers have **no System** yet today depend on
"the one instance." Every site is classified below; the implementation plan must convert (b) and
(c), not just the lifecycle ops in (a). Once the singleton guards are removed,
`remote_config_from_inventory()` is **deleted** and replaced by either `remote_config_for_resource(name)`
(identity-bearing) or `all_remote_configs()` (enumerate-all) — no caller is left resolving "an
arbitrary single instance," which with N hosts would silently hit the wrong host.

- **(a) Per-op via System** — `lifecycle/{provisioning,connect,install,control,build_vm}.py`,
  `debug/introspect.py`, `retrieve/facade.py`, `staged_volumes.py`. Resolve by the bound
  resource name threaded from `System → Allocation → Resource` (B.2).
- **(b) Reconciler sweeps with a domain/handle but no System** —
  `transport_reset.py` (`RemoteLibvirtTransportResetter.reset(transport, transport_handle,
  domain_name)`, the ADR-0086 dead-worker gdbstub re-arm) and `reaping/connections.py` (port /
  connection reaping). These must resolve **per domain → System → Resource** (the reconciler maps
  `domain_name`/handle to its System) so a dead gdbstub on host B is reset on host B and the
  reaper does not leak ports on non-first hosts. A reaper with no specific domain enumerates
  **all** hosts via `all_remote_configs()`.
- **(c) Diagnostics / doctor probes** — `diagnostics/reachability.py`,
  `diagnostics/base_image_staging.py`, `diagnostics/contribution.py` (calls
  `remote_config_from_inventory()` directly today, line ~50), `gdbstub_acl`. A doctor describes
  the fleet, so each probe **fans out per declared instance** (`all_remote_configs()` → one
  result row per host) instead of reporting only one host's health. `resolve_base_image_staged_volume`
  becomes per-instance the same way.
- **(d) Genuinely process-wide** — `composition.build_console_hosting` bootstrap (B.4, the
  per-console open is per-system) and `discovery.py` (already enumerate-all). These keep a
  no-identity entry point, but it is `all_remote_configs()` / per-instance, never "the single
  instance."

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
  resource name; console factory resolves per system; **reconciler reset/reap (class b)** resolves
  per domain→resource and a domain-less reap enumerates all hosts; **diagnostics (class c)** fan
  out one result per declared instance; `all_remote_configs()` validates and returns every
  instance; a grep proves no remaining `remote_config_from_inventory` / `_require_single_instance`
  references.
- **Boundary/error paths:** empty pool string; pool + kind both set; pool naming a kind value;
  resource whose allocation has NULL resource_id (queued) never reaches a remote op.
- **Tenant isolation:** project A requesting an unknown pool gets a generic denial that does
  **not** echo project B's affinity-scoped pool names (no `SELECT DISTINCT pool` leak).

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
