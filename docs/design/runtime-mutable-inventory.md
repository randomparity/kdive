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
  name          text     -- the (kind,name) instance identity, ADR-0112
  resource_kind text     -- e.g. 'remote-libvirt' for a resource; NULL for build_host
  disposition   text     -- 'detached' | 'removed'
  reason        text     -- operator-supplied audit reason
  actor         text     -- principal that set the override
  created_at    timestamptz
  PRIMARY KEY (source_kind, name)
```

Two dispositions:

- **`detached`** — "runtime owns the live row; ignore the file's *values* for it." Set when an
  operator modifies a config-declared row in place (e.g. `ops.set_host_capacity`). The row keeps
  `managed_by='config'` (so export still emits it) but reconcile no longer overwrites its
  runtime-owned fields from the file.
- **`removed`** — "suppress this identity; do not re-create it." Set when an operator removes a
  config-declared host. The live row is deleted (if idle) or cordoned (if live, per ADR-0112);
  reconcile skips re-creating the identity while the ledger entry stands.

Reconcile consults the ledger before acting on each declared / departed identity:

| Ledger state for identity | Reconcile behavior | Drift-repair preserved? |
|---|---|---|
| **no entry** | exactly today: create on appear, repair a deleted row, prune/cordon on departure | ✅ unchanged |
| **`detached`** | ensure the row exists (repair a *corrupted* row's identity) but do **not** overwrite runtime-owned fields; never prune | ✅ identity repair kept; value authority ceded |
| **`removed`** | do **not** create; if a live row exists, cordon (never auto-drain) | ✅ — a genuine corrupted row was never the target |

This is the precise answer to the issue's hard part: *"reconcile can no longer treat
file-absence as prune or DB-absence as re-create for runtime-mutated rows, without losing
drift repair's original benefit."* An identity with **no ledger entry** still gets full
drift repair; only the explicitly-overridden ones are exempted, and `detached` still repairs a
missing *row* (identity) while ceding *field* authority.

### Lifecycle and convergence

```
file declares X ──reconcile──> live row X (managed_by=config)
   │
   ├─ operator set_host_capacity(X) ──> ledger[X]=detached ; reconcile stops clobbering cap
   ├─ operator deregister(X, reason) ──> ledger[X]=removed ; row deleted/cordoned ; reconcile won't recreate
   │
   └─ operator export_systems_toml ──> file now matches live state
            └─ operator commits file + re-applies ConfigMap
                     └─ reconcile: file no longer declares removed X (or declares the detached cap)
                              └─ ledger entries are now no-ops; a GC step clears settled entries
```

Ledger entries are **not** permanent: once the file is exported and re-applied so that the file
agrees with live state, the entry is redundant. Reconcile GCs a `removed` entry whose identity is
no longer declared in the file, and a `detached` entry whose file values now equal the live row.
This keeps the ledger bounded and makes the export the natural "commit my runtime changes" step.

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

- Migration adding `inventory_overrides` (forward-only, additive; ADR-0112 schema style).
- Repository helpers: set/clear/lookup overrides; the GC step.
- Reconcile inventory pass consults the ledger (`detached` → skip field overwrite; `removed` →
  skip create + cordon-if-live) and GCs settled entries.
- ADR-0199 is implemented here.
- **Acceptance:** with a hand-inserted `removed` entry, a declared host is not re-created across
  passes; with a `detached` entry, a file capacity change does not overwrite the runtime cap; an
  identity with no entry is still fully drift-repaired (regression test against ADR-0021).

### B — Durable runtime add/remove/modify for config-owned inventory

- `resources.deregister` accepts `managed_by='config'` remote-libvirt rows: requires a `reason`,
  preserves the refuse-if-live contract (cordon if live, delete if idle), and writes
  `ledger[X]=removed`.
- The build-host removal tool gains the same config-owned path + `removed` ledger write.
- `ops.set_host_capacity` (and any other in-place config-row modifier) writes `ledger[X]=detached`
  so the override sticks across passes.
- A runtime-**added** host is already `managed_by='runtime'` and already survives reconcile; B only
  needs to confirm and test this, plus ensure C's export captures it.
- **Acceptance:** the issue's add/remove criteria — add a remote-libvirt host at runtime, schedulable
  without restart, not pruned by reconcile; remove a system/build-host at runtime, stays removed
  across passes without editing `systems.toml` or the DB; removing a host with live
  allocations/leases is refused or cordoned (ADR-0112).

### C — Full inventory export (`ops.export_systems_toml`)

- A read-only `PLATFORM_OPERATOR` tool serializing the live `image_catalog`, `resources`,
  `build_hosts`, and `cost_class_coefficients` into one declarative `systems.toml` document
  (text output, deterministic ordering). Reuses ADR-0115's cost-class serializer for the
  `[[cost_class]]` blocks.
- Honors the ledger: a `removed` identity is omitted; a `detached` identity is emitted with its
  live (runtime) values.
- **Lossy-field policy** (below): fields the DB does not carry are emitted as explicit
  placeholders with a header comment, never silently dropped.
- **Acceptance:** a fresh start from the exported file reproduces the live inventory (modulo the
  documented lossy fields); the export is byte-deterministic for a given DB state.

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
keeps several file-only:

| File field | Carried in DB? | Export policy |
|---|---|---|
| secret refs (`client_cert_ref`, `client_key_ref`, `ca_cert_ref`) | name only, never material | emit the stored ref *name*; never the material |
| `gdbstub_range` | no (only individual allocated ports live in the port registry) | emit a placeholder + comment; operator fills the range |
| `shapes` | not a DB column today | emit `[]` + comment if absent |
| `base_image` (FK to an `[[image]]` name) | yes (resolvable) | emit the resolved name |
| build-host `base_image_volume` | yes (`build_hosts.base_image_volume`) | emit as stored |
| comments / `[campaign.*]` knobs | no | not reconstructable; header comment states the export is values-only |

The export's header comment states plainly that it is a values snapshot: secret *material*,
free-form comments, and any field marked above are not reconstructed, and an operator must review
placeholders before committing. A re-import of the exported file must parse and reproduce the same
DB state for every non-placeholder field (round-trip test).

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
