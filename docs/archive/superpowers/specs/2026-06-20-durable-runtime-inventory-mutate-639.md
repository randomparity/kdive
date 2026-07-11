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
   (a new required-when-config parameter).

   **Transaction/lock structure (atomic, one acquisition).** The config path runs in a single
   `conn.transaction()` and takes `resource_identity_lock(conn, REMOTE_LIBVIRT, name)` **once**, then
   does everything under that one lock so the live-count gate and the row change cannot be raced by a
   reconcile pass or a concurrent allocation. It does **not** call `prune_or_cordon_removed_resource`
   (that helper opens its **own** `conn.transaction()` and re-takes the identity lock — nesting it
   inside the tool's transaction would create a savepoint and a redundant re-lock, and the live-count
   gate must sit inside the same lock to avoid a TOCTOU). Instead the tool **inlines** the FK-safe
   `removed` disposition, mirroring `prune_or_cordon_removed_resource`'s body:
   - `SELECT id, managed_by, name FROM resources WHERE id = %s FOR UPDATE` (the row must be
     `managed_by='config'`, `kind='remote_libvirt'`; the lock key needs the `name`, so resolve
     `name` from this row, then take the lock — i.e. read kind+name first, acquire the lock, re-read
     `FOR UPDATE` under it),
   - compute the live-allocation count under the lock; if live and not `force` → `conflict` (refused,
     **no** ledger write, **no** cordon),
   - apply the disposition: a row with **any** allocation row (live or terminal) is **cordoned**
     (`cordoned=true, lease_expires_at=NULL`); a never-allocated row is **hard-deleted**,
   - write `inventory_overrides[(resource, remote_libvirt, name)] = removed` with the `reason` and
     actor, in the same transaction,
   - audit one `platform_audit_log` row.

   The success envelope's `disposition` reports the row's **resulting state** (`deleted` |
   `cordoned`), derived from which branch ran — **not** from a `rowcount`-vs-`NOT cordoned` flag — so
   re-removing an already-cordoned row still reports `cordoned` (see edge below).

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
   `managed_by='config'` host is removable. `build_hosts.remove` gains an **optional `reason`**
   parameter; it is **required only when the resolved row is `managed_by='config'`** (a
   config-owned removal with an empty/blank `reason` → `configuration_error`). A `runtime`-owned host
   keeps its current behavior (plain delete, **no** `reason` needed, **no** ledger). `worker-local`
   stays protected (CONFLICT) regardless of ownership.

   The config path runs in one transaction holding the build-host identity lock
   (`advisory_xact_lock(conn, LockScope.BUILD_HOST, name)`), reads the row's `managed_by` (the
   `BuildHost` dataclass does not carry `managed_by`, so the tool SELECTs it directly), applies the
   FK-safe disposition (cordon-if-leased via `enabled=false`, else delete — `build_host_leases` FKs
   `build_hosts(id) ON DELETE RESTRICT`, so the lease is checked **first** under `FOR UPDATE` and a
   leased host is never blind-deleted), and writes
   `inventory_overrides[(build_host, BUILD_HOST_RESOURCE_KIND, name)] = removed` in the same
   transaction. The ledger PK uses `source_kind='build_host'` and the sentinel
   `resource_kind=BUILD_HOST_RESOURCE_KIND` (`"build-host"`), matching `reconcile_build_hosts`.
   Unlike resources, a build host carries no retained-accounting FK (leases are deleted on release),
   so the existing `prune_or_cordon_build_host` contract is FK-safe for the `removed` path; the tool
   may reuse it or inline its body, but the lease check, the delete/cordon, and the `set_override`
   must commit atomically under the one identity lock.

3. **`ops.set_host_capacity` detaches.** The current `_update_host_cap` does a blind
   `UPDATE resources SET capabilities = ... WHERE id = %s` and returns only `rowcount==1` — it never
   reads `managed_by`, `kind`, or `name`. But the detach write needs `kind` + `name` for the
   identity-lock key and the ledger PK, and `managed_by` to decide whether to detach at all. So
   `set_host_capacity` must, in one transaction: **first** `SELECT id, kind, name, managed_by FROM
   resources WHERE id = %s FOR UPDATE`, then take `resource_identity_lock(conn, kind, name)`, then do
   the cap merge, then — **only if `managed_by='config'`** — write
   `inventory_overrides[(resource, kind, name)] = detached`. The lock wraps both the cap UPDATE and
   the `set_override`, so a concurrent reconcile pass cannot re-assert the file cap between them. A
   `runtime`/`discovery` row writes **no** entry (reconcile does not overwrite a runtime row's cap,
   and a discovery row is outside the ledger). A missing id → `configuration_error` (unchanged).
   `detached` is intent-only; no `reason` is required (a capacity change is self-describing and
   already audited).

4. **Clear-override / re-enable path.** A new `inventory.clear_override` tool (`platform_admin`,
   mutating) deletes the ledger entry for a config-declared identity, so the next no-entry reconcile
   pass re-asserts the file and the identity returns. This is the supported re-add path: the file
   still declares a `removed` host, and `register_*` rejects a config-owned name, so without this an
   operator who removed a config host at runtime could never get it back.

   It clears **both** a resource and a build-host override, so it is keyed by the ledger identity
   `(source_kind, resource_kind, name)`. It lives under a neutral `inventory.*` namespace rather than
   `resources.*`/`build_hosts.*` precisely because it spans both families (a build-host operator
   should not have to look under `resources.*`). The three inputs are **validated against the
   ledger enums**: `source_kind` must parse to `InventorySourceKind` and `resource_kind` must be
   consistent with it — for `source_kind='build_host'` the only legal `resource_kind` is the
   `BUILD_HOST_RESOURCE_KIND` sentinel (`"build-host"`); for `source_kind='resource'` it must be a
   valid `ResourceKind`. An unknown `source_kind`, or an illegal `(source_kind, resource_kind)`
   pairing, → `configuration_error` before any DB read. The tool runs in one transaction, takes the
   matching per-identity lock (resource → `resource_identity_lock(kind, name)`; build-host →
   `advisory_xact_lock(BUILD_HOST, name)`), calls `clear_override`, and audits. Clearing an identity
   with **no** entry → `not_found` (idempotent: a second clear also reports `not_found`, no side
   effect). Honors the **three-registration rule** (registrar + `test_tool_docs` + `exposure.py`
   PUBLIC_TOOLS).

5. **Confirm runtime-add survives.** A regression test asserting a `register_remote_libvirt` host
   (`managed_by='runtime'`) is created with no ledger entry and is **not** pruned across reconcile
   passes (it locks in the add criterion; the behavior is already in place from M2.6).

## Success criteria (falsifiable)

- **Config remote-libvirt deregister, idle:** a `config` remote-libvirt row with no allocation
  history, deregistered with a `reason` → row **deleted**, ledger has one `removed` entry for its
  identity, reconcile pass with the host still in the file does **not** re-create it.
- **Config remote-libvirt deregister, allocation-bearing:** a `config` remote-libvirt row that ever
  held an allocation → **cordoned** (not deleted), ledger `removed`, lease cleared. The envelope
  `disposition` reflects the row's **resulting state** (`cordoned`), so an already-cordoned row
  re-removed still reports `cordoned` (the assertion is on final row state + ledger entry, not on a
  per-call rowcount flag). A row with a **live** allocation without `force=True` → `conflict`
  (refused), no ledger write, no cordon.
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
- **Clear-override re-enables:** remove a config host → `inventory.clear_override` → the next
  reconcile pass re-creates the config row, with no `systems.toml` edit. Clearing a non-existent
  override → `not_found` (idempotent-safe: a second clear also reports `not_found`). An unknown
  `source_kind` or an illegal `(source_kind, resource_kind)` pairing → `configuration_error`.
- **Runtime-add survives:** a runtime-registered remote-libvirt host has no ledger entry and is not
  pruned across passes.
- **Concurrency:** a deregister/remove ledger write and a concurrent reconcile create/prune for the
  same identity serialize on the per-identity lock (the write and the row change commit atomically;
  the reconcile pass either sees the entry or does not, never a half-applied state).

## Failure modes & edges

- **Empty `reason`** → `configuration_error` before any DB write (fail fast).
- **Absent id/name** → `not_found` (deregister by UUID; build-host remove by name; clear-override by
  the `(source_kind, resource_kind, name)` identity).
- **Config row of an out-of-scope kind** (`fault_inject`) passed to deregister → stays `conflict`
  (issue scopes config-deregister to remote-libvirt; fault-inject config removal is not requested).
- **Clear-override of an identity with no entry** → `not_found` (nothing to clear), no side effect.
- **Double-remove** (deregister an already-`removed` identity) → idempotent: `set_override` upserts
  the entry (re-stamping `reason`/`actor`, keeping the original `created_at`), the row is already
  gone (`not_found` if hard-deleted) or already cordoned (envelope `disposition='cordoned'` from the
  row's resulting state). Tests assert on final row state + the single ledger entry, never on a
  `rowcount`-derived flag.
- **Secret refs** never enter audit rows or envelopes (existing invariant; B adds no secret
  surface — only a `reason` string and identity).

## Out of scope

- The export tool (`ops.export_systems_toml`, sub-issue C) and ConfigMap writeback (D).
- Config-owned **fault-inject** deregister (issue names remote-libvirt only).
- Reworking discovery-owned rows or `local-libvirt` registration.
- Auto-export on mutation.
