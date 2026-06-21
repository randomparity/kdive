# Runtime-mutable inventory (M2.7) — Milestone Implementation Plan

> **For agentic workers:** This is a **milestone** plan. It decomposes epic #429 into four
> sub-issues (A/B/C/D), each of which gets its **own** spec→plan→implementation `/work-issue`
> cycle (repo convention, `AGENTS.md`). This document locks the decomposition, file map,
> sequencing, cross-cutting constraints, and per-sub-issue task breakdown + acceptance — enough
> to file each sub-issue and pick it up later. Per-function TDD step detail lives in each
> sub-issue's own plan.

**Goal:** Make config-declared inventory (`remote_libvirt` resources, `build_hosts`) durably
mutable at runtime — add/remove/modify that survives reconcile — and export the running inventory
back to `systems.toml`.

**Architecture:** Shift the inventory reconcile pass from drift-repair-from-file (ADR-0021) to
seed-once + DB-authoritative, mediated by a per-identity `inventory_overrides` ledger
(`detached | removed`). The file still drift-repairs every identity with **no** ledger entry;
runtime mutations write a ledger entry that reconcile honors. A full-inventory export tool persists
running state back to the file. Formalized in [ADR-0199](../../adr/0199-seed-once-runtime-authoritative-inventory.md);
full design in [the spec](../../design/runtime-mutable-inventory.md).

**Tech Stack:** Python 3.14, `uv`, Postgres (psycopg async), FastMCP tools over injected pool +
`RequestContext`, forward-only SQL migrations under `src/kdive/db/schema/`, pytest + testcontainers.

## Global Constraints

- Migrations are **forward-only, additive** (`src/kdive/db/schema/NNNN_*.sql`); the next free
  number is assigned at implementation time (parallel-agent collision zone — do not pre-pick here).
- Every tool returns a `ToolResponse` (`mcp/responses.py`) with the most specific `ErrorCategory`
  on failure; never invent error strings (`domain/errors.py`).
- A new tool needs **three registrations** (its registrar, `tests/.../test_tool_docs`, and
  `exposure.py` PUBLIC_TOOLS / `_VIEWER`) or the full suite fails outside touched dirs.
- Inventory mutations serialize through the existing `resource_identity_lock` (per `(kind, name)`)
  and the session-scoped `inventory-reconcile` lock (`db/locks.py`); a ledger write takes the
  per-identity lock in the same transaction as the row change.
- Destructive ops keep the ADR-0125 gate (capability + RBAC role + profile opt-in) and the
  ADR-0112 refuse-if-live contract (cordon, never auto-drain).
- Guardrails before every commit: `just lint`, `just type` (whole tree), focused `just test`;
  doc changes also `just check-mermaid`, `just docs-paths`, `just adr-status-check`. Doc-style
  word ban (no "critical/comprehensive/robust/…") applies to ADRs, specs, commits, comments.
- ADRs stay **Proposed** until the milestone ships; flip to Accepted at milestone close.

## File map

| Path | Responsibility | Sub-issue |
|---|---|---|
| `src/kdive/db/schema/NNNN_inventory_overrides.sql` | `inventory_overrides` table (PK `(source_kind, resource_kind, name)`) | A |
| `src/kdive/inventory/overrides.py` (new) | ledger repository: set/clear/lookup/GC helpers | A |
| `src/kdive/inventory/reconcile_resources.py` | consult ledger in create/prune; removed-idle-delete; detached skip + GC | A |
| `src/kdive/reconciler/inventory.py` | drive the GC step in the pass | A |
| `src/kdive/mcp/tools/ops/resources/deregister.py` | accept config-owned remote-libvirt; write `removed` | B |
| `src/kdive/mcp/tools/ops/resources/register.py` | clear-override / re-enable path for a `removed` config name | B |
| `src/kdive/mcp/tools/ops/build_hosts/lifecycle.py` | config-owned build-host remove + `removed` write | B |
| `src/kdive/mcp/tools/ops/tuning.py` | `set_host_capacity` writes `detached`; new `export_systems_toml` | B (capacity), C (export) |
| `src/kdive/inventory/serialize.py` (new) | model→TOML serializer (reuses ADR-0115 cost-class serializer) | C |
| `src/kdive/inventory/writeback.py` (new) + adapter seam | ConfigMap/PVC writeback adapter (+ fake for tests) | D |

---

## Sub-issue A — Reconcile-model shift + override ledger *(foundation; gates B, C)*

**Where it fits:** the load-bearing change. Implements ADR-0199. Nothing else can land first.

**Tasks (each its own test cycle):**

1. **Migration + ledger schema.** Add `inventory_overrides` (`source_kind`, `resource_kind`,
   `name`, `disposition` CHECK in (`detached`,`removed`), `reason`, `actor`, `created_at`;
   PK `(source_kind, resource_kind, name)`). Update the three migration-version-list assertions
   (memory: `test_0042_backfills`-style tests assert the version list). Test: migration applies,
   CHECK rejects an unknown disposition, schema-enum⊆SQL guard.
2. **Ledger repository** (`overrides.py`): `set_override`, `clear_override`, `lookup(kind, name)`,
   `gc_settled(doc, conn)`. Pure-ish helpers over an injected conn; unit-tested directly.
3. **Reconcile consults the ledger** in `reconcile_resources.py` (and the build-host equivalent):
   `removed` → skip create, cordon-if-live, **delete cordoned row once idle**; `detached` → leave
   live row / GC entry if row absent; no-entry → unchanged. Hold the existing locks.
4. **GC step** wired into the pass (`reconciler/inventory.py`): drop a `removed` entry whose
   identity left the file, a `detached` entry whose file values equal the live row or whose row is
   gone.

**Acceptance:**
- Hand-inserted `removed` entry → declared host not re-created across passes; a cordoned live row
  is deleted after its allocations drain.
- Hand-inserted `detached` entry → a file capacity change does not overwrite the runtime cap; a
  hand-deleted `detached` row is re-asserted from the file and the entry is GC'd.
- **Regression (guards ADR-0021):** an identity with **no** entry is still fully drift-repaired
  (re-created when hand-deleted, pruned when it leaves the file).
- `just type` whole-tree + the adversarial concurrency suite green.

## Sub-issue B — Durable runtime add/remove/modify *(depends on A)*

**Where it fits:** the operator-facing tools that write ledger entries; delivers the issue's
add/remove acceptance criteria.

**Tasks:**

1. **`resources.deregister` accepts config-owned remote-libvirt:** require a non-empty `reason`,
   keep the gate + refuse-if-live (cordon if live, delete if idle), write `ledger=removed` in the
   same txn under the per-identity lock. Test: idle→delete+removed; live→cordon+removed;
   missing-reason→`configuration_error`; discovery-owned still rejected.
2. **Build-host config-owned remove** (`build_hosts/lifecycle.py`): same pattern + `removed` write;
   refuse if it holds a live build lease (FK `ON DELETE RESTRICT`).
3. **`set_host_capacity` detaches** (`tuning.py`): write `ledger=detached` so the cap sticks across
   passes. Test: set cap, run a reconcile pass with a differing file value, assert the runtime cap
   survives.
4. **Clear-override / re-enable path** (`register.py` or a dedicated tool): clear a `removed` entry
   for a config-declared identity so the next no-entry pass re-asserts the file. Test: remove →
   clear-override → reconcile re-creates the config row; no `systems.toml` edit required. Honor the
   three-registration rule if a new tool is added.
5. **Confirm runtime-add survives** (already `managed_by='runtime'`): a test asserting a
   `register_remote_libvirt` host is not pruned across passes (locks in the add criterion).

**Acceptance:** add a remote-libvirt host at runtime → schedulable without restart, not pruned;
remove a system/build-host at runtime → stays removed across passes without editing the file or DB;
remove with live allocations/leases → refused or cordoned (ADR-0112); a removed config host
re-enabled via clear-override without a file edit.

## Sub-issue C — Full inventory export `ops.export_systems_toml` *(depends on A)*

**Where it fits:** the original "persist running → file" ask, generalized past ADR-0115's
cost-class fragment.

**Tasks:**

1. **Field-persistence audit (do this first).** Confirm, per field, what `resources`/`build_hosts`
   persist vs. what the provider reads from the file. Known from the spec: `gdb_addr`,
   `gdbstub_range`, the three TLS secret refs, `base_image`, `shapes` are **not** in the DB →
   operator-supplied placeholders. Record findings in the sub-issue's plan.
2. **Serializer** (`serialize.py`): live `image_catalog` + `resources` + `build_hosts` +
   `cost_class_coefficients` → one deterministic `systems.toml` document. Reuse ADR-0115's
   cost-class serializer for `[[cost_class]]`. Emit a header comment marking the values-only
   snapshot and naming the skeleton placeholders.
3. **`ops.export_systems_toml` tool** (`tuning.py`, read-only `PLATFORM_OPERATOR`, text output):
   honor the ledger (`removed` omitted, `detached` uses live values). Three-registration rule.

**Acceptance:** images/build_hosts/cost_classes and the identity/economic/sizing fields of
resources round-trip (export → parse → equal DB state); byte-deterministic for a given DB state; a
`remote_libvirt` block is a skeleton naming every operator-supplied placeholder; a fresh start
reproduces the live inventory **after** the operator completes the placeholders (round-trip test on
the completed file).

## Sub-issue D — ConfigMap / file writeback *(depends on C; deployment-shape, opt-in)*

**Where it fits:** turns the export text into a persisted source the reconciler re-reads; ships
last, behind explicit operator opt-in; not exercisable by local CI.

**Tasks:**

1. **Writeback adapter seam** (`writeback.py`): a port with two implementations — patch the
   `kdive-systems` ConfigMap via the k8s API (RBAC Role granting `patch` on that one ConfigMap), or
   write the PVC-backed mounted file (`KDIVE_SYSTEMS_TOML`). A fake adapter for tests.
2. **Wire the export tool** to optionally persist via the adapter behind an opt-in flag.
3. **Operator runbook** step for the real ConfigMap path + the RBAC manifest.

**Acceptance:** an operator-invoked writeback updates the source the reconciler re-reads, and a pod
restart reproduces the live inventory from it (verified on a real cluster). Local tests cover the
serializer + the writeback seam with the fake.

---

## Sequencing

```
A (foundation) ──┬──> B (tools)
                 └──> C (export) ──> D (writeback)
```

A merges first. B and C may proceed in parallel after A (disjoint files: B in `resources/` +
`build_hosts/` + `tuning.py::set_host_capacity`; C in `serialize.py` + `tuning.py::export_*` — note
the shared `tuning.py`, so serialize B's and C's `tuning.py` edits if run concurrently). D follows C.

## Self-review

- **Spec coverage:** ledger model → A; durable add/remove/modify + re-enable → B; full export +
  lossy policy → C; ConfigMap writeback → D. Drift-repair regression, removed-idle-delete,
  detached-GC, PK identity, clear-override path, skeleton export — each maps to a named task and
  acceptance bullet above. No spec section is unmapped.
- **Placeholder scan:** no TBD/TODO; migration number deliberately deferred (collision zone), called
  out explicitly rather than left vague.
- **Consistency:** disposition names (`detached`/`removed`), ledger PK `(source_kind, resource_kind,
  name)`, and tool names (`export_systems_toml`, `set_host_capacity`, `deregister`) match the spec
  and ADR verbatim.
