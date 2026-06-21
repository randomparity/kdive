# Design — Runtime-mutable inventory (M2.7)

- **Status:** Proposed
- **Date:** 2026-06-20
- **Formal decision:** [ADR-0199](../adr/0199-seed-once-runtime-authoritative-inventory.md)
- **Refines:** [ADR-0021](../adr/0021-reconciler-loop-drift-repair.md) (drift repair re-asserts the file every pass), [ADR-0112](../adr/0112-systems-inventory-config.md) (systems.toml inventory + reconcile, refuse-if-live prune)
- **Extends:** [ADR-0115](../adr/0115-declarative-cost-class-coefficients.md) §6 (`ops.export_cost_classes` — the focused cost-class half of the export)
- **Epic:** [#429](https://github.com/randomparity/kdive/issues/429)

## Problem

Two operator needs converge on one model.

1. **Durable runtime inventory mutation.** Operators must add and remove systems
   (`remote_libvirt` resources) and build-hosts at runtime — schedulable without a
   cluster restart, a ConfigMap re-apply, or a hand-edited database. Today there is
   no such path for a **config-declared** host: removing the prod host `ub24-big`
   required editing `systems.toml`, re-applying the `kdive-systems` ConfigMap, and
   waiting for reconcile to prune.

2. **Persist running state back to the file.** Transient runtime overrides are lost
   on the next reconcile pass because the file is authoritative. `ops.set_host_capacity`
   is clobbered back to the file's `concurrent_allocation_cap` every pass;
   `ops.export_cost_classes` (ADR-0115 §6) solved this for the cost-class table alone.
   The general case — export the **full** running inventory back to `systems.toml` —
   is unsolved.

### Why config-declared inventory is not runtime-mutable today

The runtime add/remove **tools already exist**, but they cannot durably change
config-declared inventory, because reconcile re-asserts the file every pass:

- Reconcile **re-creates** a config-owned `resources` / `build_hosts` row deleted
  out-of-band, and **prunes or cordons** a host removed from the file
  (`reconciler/inventory.py`, `inventory/reconcile_resources.py::_prune_departed`).
  This is ADR-0021 drift repair, *not* gated on the file hash.
- `resources.deregister` operates **only on `managed_by='runtime'`** rows and rejects
  config/discovery-owned ones (its docstring: *"a config resource is removed by editing
  `systems.toml`."*).
- `build_hosts.remove` works, but the next reconcile pass re-creates a config-owned host.
- `resources.register_remote_libvirt` / `build_hosts.register_*` add `managed_by='runtime'`
  rows that live alongside — not integrated with — the config-owned set; any divergence
  is transient (reverts on restart because nothing exports it back to the file).

Net: the only durable way to add/remove a config-declared system is to edit the file
and re-apply the ConfigMap — a config edit, not a runtime operation.

## Decision (summary)

Shift the reconcile model from **drift-repair-from-file** to **seed-once-then-DB-authoritative**,
mediated by a small per-identity **inventory-override ledger**. `systems.toml` seeds initial
state at startup and continues to repair genuine drift for identities the operator has *not*
runtime-mutated; once an operator mutates a config-declared identity at runtime, a ledger entry
makes that mutation authoritative so reconcile does not clobber it. A full-inventory export tool
writes the running state back to `systems.toml`, after which the operator's committed file makes
the ledger entries no-ops. The formal decision is [ADR-0199](../adr/0199-seed-once-runtime-authoritative-inventory.md);
the lossless cost-class half stays [ADR-0115](../adr/0115-declarative-cost-class-coefficients.md) §6.

## The provenance model: the inventory-override ledger

A new table records, per inventory identity, an operator's intent to **override** the file:

```
inventory_overrides
  source_kind   text     -- 'resource' | 'build_host'  (the inventory family)
  resource_kind text     -- the resource `kind` ('remote-libvirt' | 'fault-inject') for a
                          --   resource; the sentinel 'build-host' for a build host
  name          text     -- the instance name, ADR-0112
  disposition   text     -- 'detached' | 'removed'
  reason        text     -- operator-supplied audit reason
  actor         text     -- principal that set the override
  created_at    timestamptz
  PRIMARY KEY (source_kind, resource_kind, name)
```

The PK includes `resource_kind` so it matches the real inventory identity: the `resources`
table is unique on `(kind, name)` (`resources_kind_name_key`), and both `remote-libvirt` and
`fault-inject` are config-owned in that table, so a name can legitimately repeat across kinds.
Build-host names are globally unique (their own `UNIQUE` constraint), so they use the fixed
sentinel `resource_kind = 'build-host'`.

Two dispositions:

- **`detached`** — "runtime owns the live row; ignore the file's *values* for it." Set when an
  operator modifies a config-declared row in place (e.g. `ops.set_host_capacity`). The row keeps
  `managed_by='config'` (so export still emits it) but reconcile no longer overwrites its
  runtime-owned fields from the file.
- **`removed`** — "suppress this identity; do not re-create it." Set when an operator removes a
  config-declared host. The live row is deleted (if idle) or cordoned (if live, per ADR-0112);
  reconcile skips re-creating the identity while the ledger entry stands, **and deletes a
  still-present cordoned row once it becomes idle** (see the reconcile table — this delete is
  driven by the ledger, not by file-departure, because the file still declares the identity).

Reconcile consults the ledger before acting on each declared / departed identity:

| Ledger state for identity | Reconcile behavior | Drift-repair preserved? |
|---|---|---|
| **no entry** | exactly today: create on appear, repair a deleted row, prune/cordon on departure | ✅ unchanged |
| **`detached`** | if the row exists, leave it (do **not** overwrite runtime-owned fields, never prune); if the row was hand-deleted, **GC the entry** and let the no-entry path re-assert from the file | partial — see note below |
| **`removed`** | do **not** create; cordon a live row (never auto-drain), then **delete it once idle** | ✅ — a genuine corrupted row was never the target |

This is the precise answer to the issue's hard part: *"reconcile can no longer treat
file-absence as prune or DB-absence as re-create for runtime-mutated rows, without losing
drift repair's original benefit."* An identity with **no ledger entry** still gets full
drift repair; only the explicitly-overridden ones are exempted.

**"Delete once idle" is FK-safe, not literal.** `allocations.resource_id` is
`NOT NULL REFERENCES resources(id)` with no `ON DELETE`, and allocation rows are retained for
accounting after they go terminal, so a resource that **ever** held an allocation cannot be
row-deleted (the same constraint `resources.deregister` documents). The `removed` reconcile delete
therefore deletes only a **never-allocated** row and **cordons** a row with any allocation history
(live or terminal) — the cordon is the durable suppression, the ledger entry keeps it suppressed,
and an operator export later drops the entry. A build host has no such retained FK
(`build_host_leases` rows are deleted on release), so its `removed` row is deleted once it holds no
in-flight lease.

**`detached` is value-loss-safe only while its row lives.** The ledger is intent-only — it
carries no field values. So a `detached` override protects the live row's runtime values *in
place*; it cannot reconstruct them if the row is hand-deleted. Rather than resurrect the row
with stale file values while the entry still claims an override is in force, reconcile **GCs a
`detached` entry whose row no longer exists** and re-asserts the file on the next pass (no
entry). The operator sees the override reverted (e.g. the capacity is back to the file value)
and re-applies it. This keeps drift repair (the row returns) without silently misrepresenting a
lost override as live. A value-carrying ledger that could restore the runtime value is a possible
future refinement, deliberately out of scope here.

### Lifecycle and convergence

```
file declares X ──reconcile──> live row X (managed_by=config)
   │
   ├─ operator set_host_capacity(X) ──> ledger[X]=detached ; reconcile stops clobbering cap
   ├─ operator deregister(X, reason) ──> ledger[X]=removed ; row deleted/cordoned ; reconcile won't recreate
   │                                        └─ operator re-enables X ──> clear-override(X) clears ledger[X]
   │                                                 └─ reconcile (no entry) re-asserts file ──> live row X
   │
   └─ operator export_systems_toml ──> file now matches live state
            └─ operator commits file + re-applies ConfigMap
                     └─ reconcile: file no longer declares removed X (or declares the detached cap)
                              └─ ledger entries are now no-ops; a GC step clears settled entries
```

**Clearing an override is an explicit operation, not only a side effect of export.** An operator
who removed X at runtime but later wants it back faces a trap if the only clearing rule is
"identity left the file": the file still declares X, reconcile keeps suppressing it, and
`resources.register_*` rejects the config-owned name. So sub-issue B provides a **clear-override**
path (the inverse of the runtime remove — e.g. a `force`/`re_enable` flag on the registration tool
for a `removed` config name, or a dedicated clear tool) that deletes the ledger entry; the next
no-entry pass re-asserts the file and X returns. This is the supported re-add path; it does not
require editing `systems.toml`.

Beyond that explicit clear, ledger entries are **not** permanent: once the file is exported and
re-applied so it agrees with live state, an entry is redundant and reconcile GCs it — a `removed`
entry whose identity is no longer declared in the file, a `detached` entry whose file values now
equal the live row (or whose row was hand-deleted, per the note above). This keeps the ledger
bounded and makes the export the natural "commit my runtime changes" step.

## Reconcile-model shift (refines ADR-0021/0112)

ADR-0021 establishes that the reconciler is a drift-repair loop and ADR-0112 that the file is the
source of truth for config-owned inventory rows. ADR-0199 narrows both: the file is the source of
truth **for identities with no override ledger entry**. The reconcile inventory pass
(`reconcile_resources`, the build-host equivalent) gains a ledger lookup under the existing
session-scoped `inventory-reconcile` lock, so the ledger read and the create/prune decision are
atomic with respect to a concurrent `resources.register_*` (which already takes the per-identity
lock). No other reconciler duty (orphan teardown, leaked-domain reap, zombie sweep, debug-session
detach) changes — this is strictly the inventory pass.

## Sub-milestones

The epic decomposes into four issues. **A gates B and C; C gates D.** B and C may proceed in
parallel after A.

### A — Reconcile-model shift + override ledger *(foundation)*

- Migration adding `inventory_overrides` (forward-only, additive; ADR-0112 schema style; PK
  `(source_kind, resource_kind, name)`).
- Repository helpers: set/clear/lookup overrides; the GC step.
- Reconcile inventory pass consults the ledger: `detached` → skip field overwrite (GC the entry if
  its row was deleted); `removed` → skip create, cordon-if-live, **delete the cordoned row once
  idle**; then GC settled entries.
- ADR-0199 is implemented here.
- **Acceptance:** with a hand-inserted `removed` entry, a declared host is not re-created across
  passes, and a cordoned live row is deleted after its allocations drain; with a `detached` entry,
  a file capacity change does not overwrite the runtime cap, and a hand-deleted `detached` row is
  re-asserted from the file with the entry GC'd; an identity with no entry is still fully
  drift-repaired (regression test against ADR-0021).

### B — Durable runtime add/remove/modify for config-owned inventory

- `resources.deregister` accepts `managed_by='config'` remote-libvirt rows: requires a `reason`,
  preserves the refuse-if-live contract (cordon if live, delete if idle), and writes
  `ledger[X]=removed`.
- The build-host removal tool gains the same config-owned path + `removed` ledger write.
- `ops.set_host_capacity` (and any other in-place config-row modifier) writes `ledger[X]=detached`
  so the override sticks across passes.
- A **clear-override** path re-enables a `removed` config-declared identity (the inverse of the
  runtime remove): it clears the ledger entry so the next no-entry pass re-asserts the file. Without
  it an operator cannot re-add a config-declared host they removed, since the file still declares it
  and `register_*` rejects the config-owned name.
- A runtime-**added** host is already `managed_by='runtime'` and already survives reconcile; B only
  needs to confirm and test this, plus ensure C's export captures it.
- **Acceptance:** the issue's add/remove criteria — add a remote-libvirt host at runtime, schedulable
  without restart, not pruned by reconcile; remove a system/build-host at runtime, stays removed
  across passes without editing `systems.toml` or the DB; removing a host with live
  allocations/leases is refused or cordoned (ADR-0112); a removed config-declared host can be
  re-enabled via the clear-override path without a file edit.

### C — Full inventory export (`ops.export_systems_toml`)

- A read-only `PLATFORM_OPERATOR` tool serializing the live `image_catalog`, `resources`,
  `build_hosts`, and `cost_class_coefficients` into one declarative `systems.toml` document
  (text output, deterministic ordering). Reuses ADR-0115's cost-class serializer for the
  `[[cost_class]]` blocks.
- **First task is a field-by-field persistence audit.** The `resources` row persists only
  `kind, name, host_uri, cost_class, pool, managed_by` plus the sizing/cap fields in the
  `capabilities` jsonb. The remote_libvirt **connection and debug** fields — `gdb_addr`,
  `gdbstub_range`, the three TLS secret refs (`client_cert_ref`/`client_key_ref`/`ca_cert_ref`),
  `base_image`, `shapes` — are **not** in the DB; they are read straight from the file by
  `providers/remote_libvirt/{transport,config}.py`. The export cannot recover them and emits them
  as operator-supplied placeholders (see the lossy-field policy). The audit must confirm, per
  field, which path applies before C is implemented.
- Honors the ledger: a `removed` identity is omitted; a `detached` identity is emitted with its
  live (runtime) values.
- **Acceptance:** the export faithfully round-trips images, build_hosts, cost_classes, and the
  identity/economic/sizing fields of resources (export → parse → equal DB state for those fields);
  it is byte-deterministic for a given DB state. A `remote_libvirt` block is emitted as a
  **skeleton** whose operator-supplied connection/debug fields are placeholders; a fresh start
  reproduces the live inventory **after the operator completes those placeholders** (the export
  alone does not, because the file-only required fields are not in the DB). The acceptance test
  asserts the completed file reproduces the DB state, and that the skeleton names every
  placeholder field.

### D — Persist export back to the ConfigMap / mounted file

- Write the exported document to the live source the app reads (`KDIVE_SYSTEMS_TOML`):
  either patch the `kdive-systems` ConfigMap via the Kubernetes API (needs an RBAC Role granting
  `patch` on that one ConfigMap) **or** write a PVC-backed mounted file. This is a deployment-shape
  concern not exercisable by local CI; it ships last and behind an explicit operator opt-in.
- **Acceptance:** an operator-invoked writeback updates the source the reconciler re-reads, and a
  pod restart reproduces the live inventory from it. Verified on a real cluster; local tests cover
  the serializer + the writeback adapter seam with a fake.

## Lossy round-trip policy (C)

The `resources` / `build_hosts` rows do not carry every `systems.toml` field. ADR-0112's model
keeps the remote_libvirt connection/debug config file-only — it is consumed directly from the file
by the provider, never persisted to the `resources` row. So for a `remote_libvirt` block the export
recovers identity/economics/sizing from the DB and emits the rest as **operator-supplied
placeholders**:

| File field | Carried in `resources`? | Export policy |
|---|---|---|
| `name`, `cost_class`, `pool`, `uri` (→`host_uri`), `vcpus`, `memory_mb`, `concurrent_allocation_cap` | yes (columns / `capabilities` jsonb) | emit from DB (faithful round-trip) |
| `gdb_addr` | no | placeholder + comment; operator supplies |
| `gdbstub_range` | no (only allocated ports live in the port registry) | placeholder + comment; operator supplies |
| secret refs (`client_cert_ref`, `client_key_ref`, `ca_cert_ref`) | no — neither material nor the ref *name* is stored | placeholder + comment; operator supplies (never the material) |
| `base_image` (link to an `[[image]]` name) | no (read from file by the provider) | placeholder + comment; operator supplies |
| `shapes` | no | placeholder `[]` + comment; operator supplies if used |
| build-host `base_image_volume` | yes (`build_hosts.base_image_volume`) | emit as stored |
| comments / `[campaign.*]` knobs | no | not reconstructable; header comment states the export is values-only |

The export's header comment states plainly that a `remote_libvirt` block is a **skeleton**: the
fields above marked "no" are not in the DB and must be completed by the operator before the file
parses (they are required fields, so an unedited skeleton will *not* load). Secret *material* is
never emitted. A re-import of the **completed** file must parse and reproduce the same DB state for
every DB-carried field (round-trip test); the skeleton itself is validated by asserting it names
every placeholder field. Images, build_hosts, and cost_classes round-trip with no operator step.

## Concurrency

All ledger writes (B) and reconcile ledger reads (A) serialize through the existing locks:
the per-identity `resource_identity_lock` (already held by `register_*` and prune) and the
session-scoped `inventory-reconcile` lock (held for a whole reconcile pass). A ledger write in a
mutation tool takes the per-identity lock so it cannot interleave with a reconcile pass's
create/prune for the same identity. The export (C) is a read; it takes no inventory lock and
tolerates a concurrent mutation (it snapshots whatever committed state it reads).

## Testing

- **A:** unit tests on the ledger repository; reconcile tests asserting each ledger state's
  behavior; a regression test that a *no-entry* identity is still drift-repaired (guards the
  ADR-0021 benefit).
- **B:** handler tests for config-owned deregister (idle→delete+`removed`, live→cordon+`removed`,
  refuse-without-reason), `set_host_capacity` detaching, and an adversarial test interleaving a
  mutation with a reconcile pass under the locks.
- **C:** serializer round-trip property test (export → parse → equal DB state for non-lossy
  fields); determinism test; ledger-honoring test (`removed` omitted, `detached` uses live values).
- **D:** serializer/writeback seam tested with a fake adapter; the real ConfigMap path is an
  operator runbook step, not CI.

## Out of scope

- Reworking discovery-owned (`local-libvirt`) rows — they remain hardware-probe-owned; the ledger
  applies to config-owned resources and build-hosts only.
- A general three-way GitOps merge engine. The ledger is a targeted override record, not a full
  desired/last-applied/live reconciler.
- Auto-export on every mutation. Export stays an explicit operator action (the "commit my runtime
  changes" step).

## Considered & rejected

- **In-place `detached` flag only (no ledger).** A boolean on the row handles modify but cannot
  durably express a **removal** while the file still declares the host — the deleted row has
  nowhere to hold the flag, so reconcile re-creates it. Rejected: insufficient for the remove
  criterion.
- **Three-way last-applied snapshot (GitOps merge).** Track the last-applied file and diff
  desired/last-applied/live. A host removed at runtime while still declared in the file looks like
  a *new* file declaration → re-created; a suppression record (a tombstone) is needed anyway.
  Rejected: most machinery, still needs the ledger's core idea.
- **Re-label `managed_by` config→runtime on mutation.** Flipping ownership loses the fact that the
  file still declares the identity, so export can no longer faithfully re-emit it and a later file
  edit silently stops applying. Rejected: conflates ownership with override intent.
- **Auto-write the ConfigMap on every runtime mutation.** Couples every tool call to a Kubernetes
  API write and an RBAC dependency, and makes a mutation fail when the cluster API is unavailable.
  Rejected: export stays explicit; D is opt-in.
