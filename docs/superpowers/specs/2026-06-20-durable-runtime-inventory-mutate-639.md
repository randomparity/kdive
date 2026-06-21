# Sub-issue B spec — durable runtime add/remove/modify for config-owned inventory (#639)

> Derived from [ADR-0199](../../adr/0199-seed-once-runtime-authoritative-inventory.md) and
> [the M2.7 design](../../design/runtime-mutable-inventory.md). The milestone plan
> ([2026-06-20-runtime-mutable-inventory.md](../plans/2026-06-20-runtime-mutable-inventory.md))
> locks the decomposition and acceptance; this spec scopes sub-issue **B** only. Sub-issues
> A/C/D are out of scope (A is merged; C/D are siblings/followers).

## Context

Sub-issue A shifted the inventory reconcile model to seed-once + DB-authoritative, mediated by the
`inventory_overrides` ledger (`(source_kind, resource_kind, name)` → `detached | removed`). The
reconcile pass already consults the ledger: a `removed` entry suppresses create and drives a
cordon-if-live / delete-once-idle of the still-declared row; a `detached` entry leaves the live
row's runtime-owned fields in place and never prunes it. **A added no operator-facing way to write
those entries** — every reconcile test in A hand-inserts them.

Sub-issue B delivers the operator-facing mutation tools that **write** the ledger, so an operator
can durably add/remove/modify config-declared inventory at runtime. The reconcile-side honoring is
already in place (A); B only writes entries and preserves the destructive/refuse-if-live contracts.

## Decision (implements ADR-0199; no new architectural decision)

Five changes, all reusing A's ledger repository (`kdive.inventory.overrides`) and the existing
per-identity locks. Each ledger write commits in the **same transaction** as its row change, under
the per-identity advisory lock, so a concurrent reconcile pass cannot interleave create/prune for
that identity.

1. **`resources.deregister` accepts a config-owned remote-libvirt row.** Today it operates only on
   `managed_by='runtime'` rows and rejects config/discovery rows with `conflict`. B extends it so a
   `managed_by='config'` **remote-libvirt** row is deregistrable, gated on a **non-empty `reason`**
   (a new required-when-config parameter). On accept it:
   - takes the per-identity `resource_identity_lock(conn, kind, name)`,
   - applies the FK-safe `removed` disposition (`prune_or_cordon_removed_resource`): cordon a row
     that ever held an allocation (live or terminal), hard-delete a never-allocated row,
   - writes `inventory_overrides[(resource, remote_libvirt, name)] = removed` with the `reason` and
     actor, in the same transaction,
   - audits one `platform_audit_log` row.

   The existing destructive gate is preserved: a row with **live** allocations is refused unless
   `force=True`, then cordoned (never auto-drained — ADR-0112). A `discovery`-owned (`local-libvirt`)
   row is **still rejected** (the ledger does not apply to hardware-probe-owned rows). A
   config-owned row of a kind other than `remote_libvirt` (i.e. `fault_inject`) is out of scope for
   this issue and stays rejected (the issue names remote-libvirt only).

   Runtime (`managed_by='runtime'`) deregister keeps its current behavior unchanged and writes **no**
   ledger entry (a runtime row is not config-declared, so there is nothing to suppress against the
   file).

2. **Config-owned build-host remove** (`build_hosts.remove`). Today it deletes only by name and
   rejects the protected `worker-local` seed; it does not consult `managed_by`. B extends it so a
   `managed_by='config'` host is removable: under the per-identity build-host lock it applies
   `prune_or_cordon_build_host` (cordon-if-leased via `enabled=false`, else delete — FK
   `ON DELETE RESTRICT` makes a blind delete of a leased host abort, so the lease is checked first)
   and writes `inventory_overrides[(build_host, build-host, name)] = removed`. A non-empty `reason`
   is required for a config-owned removal. A `runtime`-owned host keeps its current behavior (plain
   delete, no ledger). `worker-local` stays protected.

3. **`ops.set_host_capacity` detaches.** After the in-place `concurrent_allocation_cap` merge, if
   the target row is `managed_by='config'`, write `inventory_overrides[(resource, kind, name)] =
   detached` in the same transaction under the per-identity lock, so the runtime cap survives the
   next reconcile pass (which would otherwise re-assert the file value). A `runtime`/`discovery` row
   needs no entry (reconcile does not overwrite a runtime row's cap, and a discovery row is outside
   the ledger), so no entry is written for those. `detached` is intent-only; no `reason` is required
   (a capacity change is self-describing and already audited).

4. **Clear-override / re-enable path.** A new `resources.clear_override` tool (`platform_admin`,
   mutating) deletes the ledger entry for a config-declared identity, so the next no-entry reconcile
   pass re-asserts the file and the identity returns. This is the supported re-add path: the file
   still declares a `removed` host, and `register_*` rejects a config-owned name, so without this an
   operator who removed a config host at runtime could never get it back. The tool is keyed by the
   ledger identity `(source_kind, resource_kind, name)` so it can clear both a resource and a
   build-host override. It takes the per-identity lock, calls `clear_override`, and audits. Honors
   the **three-registration rule** (registrar + `test_tool_docs` + `exposure.py` PUBLIC_TOOLS).

5. **Confirm runtime-add survives.** A regression test asserting a `register_remote_libvirt` host
   (`managed_by='runtime'`) is created with no ledger entry and is **not** pruned across reconcile
   passes (it locks in the add criterion; the behavior is already in place from M2.6).

## Success criteria (falsifiable)

- **Config remote-libvirt deregister, idle:** a `config` remote-libvirt row with no allocation
  history, deregistered with a `reason` → row **deleted**, ledger has one `removed` entry for its
  identity, reconcile pass with the host still in the file does **not** re-create it.
- **Config remote-libvirt deregister, allocation-bearing:** a `config` remote-libvirt row that ever
  held an allocation → **cordoned** (not deleted), ledger `removed`, lease cleared. A row with a
  **live** allocation without `force=True` → `conflict` (refused), no ledger write, no cordon.
- **Missing reason:** a config-owned deregister/remove with an empty/blank `reason` →
  `configuration_error`, no row change, no ledger write.
- **Discovery row still rejected:** deregistering a `discovery`-owned `local-libvirt` row →
  `conflict`, unchanged.
- **Runtime deregister unchanged:** a `runtime` row deregister writes no ledger entry.
- **Build-host config remove:** a `config` build-host removed with a `reason` → deleted (idle) or
  cordoned (`enabled=false`, leased), ledger `removed`; a leased host is never blind-deleted (no
  aborted pass). `worker-local` stays refused.
- **set_host_capacity detaches:** setting the cap on a `config` host writes a `detached` entry; a
  following reconcile pass with a differing file cap does **not** overwrite the runtime cap. Setting
  the cap on a `runtime` host writes **no** entry.
- **Clear-override re-enables:** remove a config host → `clear_override` → the next reconcile pass
  re-creates the config row, with no `systems.toml` edit. Clearing a non-existent override →
  `not_found` (idempotent-safe: a second clear reports nothing cleared).
- **Runtime-add survives:** a runtime-registered remote-libvirt host has no ledger entry and is not
  pruned across passes.
- **Concurrency:** a deregister/remove ledger write and a concurrent reconcile create/prune for the
  same identity serialize on the per-identity lock (the write and the row change commit atomically;
  the reconcile pass either sees the entry or does not, never a half-applied state).

## Failure modes & edges

- **Empty `reason`** → `configuration_error` before any DB write (fail fast).
- **Absent id/name** → `not_found` (deregister by UUID; build-host/clear-override by name).
- **Config row of an out-of-scope kind** (`fault_inject`) passed to deregister → stays `conflict`
  (issue scopes config-deregister to remote-libvirt; fault-inject config removal is not requested).
- **Clear-override of an identity with no entry** → `not_found` (nothing to clear), no side effect.
- **Double-remove** (deregister an already-`removed` identity) → idempotent: `set_override` upserts,
  the row is already gone/cordoned, the call reports the current disposition.
- **Secret refs** never enter audit rows or envelopes (existing invariant; B adds no secret
  surface — only a `reason` string and identity).

## Out of scope

- The export tool (`ops.export_systems_toml`, sub-issue C) and ConfigMap writeback (D).
- Config-owned **fault-inject** deregister (issue names remote-libvirt only).
- Reworking discovery-owned rows or `local-libvirt` registration.
- Auto-export on mutation.
