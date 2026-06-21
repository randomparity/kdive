# Sub-issue B implementation plan — durable runtime add/remove/modify (#639)

> Derived from [the B spec](../specs/2026-06-20-durable-runtime-inventory-mutate-639.md),
> [ADR-0199](../../adr/0199-seed-once-runtime-authoritative-inventory.md), and
> [the M2.7 design](../../design/runtime-mutable-inventory.md). The milestone plan
> ([2026-06-20-runtime-mutable-inventory.md](2026-06-20-runtime-mutable-inventory.md)) locks the
> decomposition and acceptance; this plan is the per-task TDD breakdown for sub-issue **B** only.

**Scope:** the operator-facing tools that **write** A's `inventory_overrides` ledger. A (the ledger
table `inventory_overrides`, migration `0046`, the repository `kdive.inventory.overrides`, and the
reconcile-side honoring) is **already merged** — B adds **no** migration and **no** schema change.

**Migration number:** none. B is tools + tests only.

**Guardrails before every commit:** `just lint`, `just type` (whole tree), focused `just test`.
The full `just ci` is run once before the first push. Doc commits also run `just docs-links`,
`just docs-paths` (and `just check-mermaid` where mermaid is touched — none here). Zero warnings;
the doc-style word ban (no "critical/comprehensive/robust/significant/…") applies to commits,
comments, and docstrings.

**Cross-agent coordination:** sub-issue C (#640) edits the **same `tuning.py`**, adding
`export_systems_toml`. B touches only `set_host_capacity` (+ its imports and one new helper). Keep
B's `tuning.py` edits localized to the `set_host_capacity` function, its new helpers, and the import
block so the serialize-merge conflict is small. Note the exact `tuning.py` hunks in the PR body.

**Key foundation facts (verified against merged A):**

- Ledger repo `kdive.inventory.overrides`: `set_override(conn, identity, *, disposition, reason,
  actor)`, `clear_override(conn, identity) -> bool`, `lookup(conn, identity)`. Identity is
  `OverrideIdentity(source_kind, resource_kind, name)`. Enums: `InventorySourceKind.{RESOURCE,
  BUILD_HOST}`, `InventoryOverrideDisposition.{DETACHED, REMOVED}`, sentinel
  `BUILD_HOST_RESOURCE_KIND = "build-host"`. Helpers take an injected conn, take **no** lock, open
  **no** transaction — the caller owns both.
- `resource_identity_lock(conn, kind, name)` (`kdive.inventory.reconcile`) is the per-`(kind, name)`
  xact lock; `advisory_xact_lock(conn, LockScope.BUILD_HOST, name)` (`kdive.db.locks`) is the
  build-host equivalent. `pg_advisory_xact_lock` is re-entrant per session and releases on
  commit/rollback, so it must run inside an open `conn.transaction()`.
- `prune_or_cordon_removed_resource` / `prune_or_cordon_build_host` (`kdive.inventory.reconcile`)
  each open **their own** `conn.transaction()` and (for resources) re-take the identity lock — so
  B's tools **inline** the FK-safe disposition body rather than nest those helpers inside the tool's
  own transaction.
- `deregister_resource(pool, ctx, *, resource_id, force=False)` (`deregister.py`) today rejects
  config/discovery rows. `build_hosts.remove` is `remove_build_host(pool, ctx, *, name)`
  (`build_hosts/lifecycle.py`); `BuildHost` dataclass does **not** carry `managed_by`. `tuning.py`'s
  `set_host_capacity` `_update_host_cap` does a blind id-only UPDATE returning `rowcount==1`.
- A new MCP tool needs **three** registrations: its registrar `@app.tool`, an entry in
  `tests/mcp/core/test_tool_docs.py`, and a scope in `exposure.py` **`CLASSIFIED_TOOLS`** (the gated
  map — `inventory.clear_override` is `_PLAT_ADMIN`, sitting beside `build_hosts.remove: _PLAT_ADMIN`).
  **Not** `PUBLIC_TOOLS` — that frozenset is for intentionally-ungated open reads (an admin mutation
  there would be mislabeled as public). The completeness guard asserts
  `CLASSIFIED_TOOLS | PUBLIC_TOOLS == live registry`. The registrar is wired into `mcp/app.py` via a
  `_pool_only_plane_registrar` entry (it needs only pool + ctx, no provider resolver).

---

## Task B1 — `resources.deregister` accepts a config-owned remote-libvirt row

**Files:** `src/kdive/mcp/tools/ops/resources/deregister.py`;
`tests/mcp/ops/test_resources_mutation.py` (extend).

**Where it fits:** spec §1 — the headline remove path for a config-declared remote-libvirt host.

**Behavior:**

1. Add a `reason: str = ""` parameter (after `force`). The MCP registrar tool gains a `reason`
   argument (a non-secret audit string).
2. **Branch dispatch.** Today `_locked_runtime_row` SELECTs `... WHERE id=%s AND
   managed_by='runtime' FOR UPDATE`, so a config row misses and falls into `_classify_absent` (which
   returns `CONFLICT`). B1 must route a config remote-libvirt row to the new accept path **before**
   that rejection. Restructure so the tool first reads the row's `managed_by` + `kind` (unfiltered)
   to dispatch: `runtime` → existing path unchanged (no `reason`, no ledger); `config` +
   `remote_libvirt` → new config path below; `config`+other-kind / `discovery` → `CONFLICT`
   (existing `_classify_absent` message); truly absent → `not_found`. Keep the existing
   `_locked_runtime_row` runtime path **unchanged** for the runtime branch. The config path:
   when the row is `managed_by='config'` **and** `kind='remote_libvirt'`:
   - require a non-empty `reason` (strip-and-check) → else `config_error(resource_id, ...)`
     (`CONFIGURATION_ERROR`) **before** any row mutation.
   - run one `conn.transaction()`, take `resource_identity_lock(conn, ResourceKind.REMOTE_LIBVIRT,
     name)` once, then `SELECT id, managed_by FROM resources WHERE id = %s FOR UPDATE` (re-read under
     the lock).
   - compute the live-allocation count (`_LIVE` states) under the lock; if `live and not force` →
     `CONFLICT` envelope (unchanged shape), no ledger write.
   - inline the FK-safe `removed` disposition: if any allocation row (any state) references the row
     → `UPDATE resources SET cordoned = true, lease_expires_at = NULL WHERE id = %s`
     (disposition `cordoned`); else `DELETE FROM resources WHERE id = %s` (disposition `deleted`).
   - `set_override(conn, OverrideIdentity(RESOURCE, "remote_libvirt", name), disposition=REMOVED,
     reason=reason, actor=actor_for(ctx))`.
   - audit one `platform_audit_log` row (reuse `_audit_deregister`, scoped with the disposition).
   - the success envelope `disposition` is `deleted` | `cordoned` from which branch ran (resulting
     state, **not** a rowcount flag).
3. A `managed_by='config'` row of any other kind (`fault_inject`), and any `discovery` row, stay
   `CONFLICT` (existing `_classify_absent` message path — keep it for the rejected kinds).
4. A truly-absent id → `not_found` (unchanged).

**Tests (TDD, write first, confirm red):**
- config remote-libvirt, idle (no allocation history) + `reason` → `deleted`, one `removed` ledger
  entry, row gone.
- config remote-libvirt that held a (now terminal) allocation + `reason` → `cordoned`, `removed`
  ledger entry, `lease_expires_at` cleared, row still present.
- config remote-libvirt with a **live** allocation, no `force` → `CONFLICT`, no ledger entry, not
  cordoned. With `force=True` → `cordoned`, `removed` ledger entry.
- config remote-libvirt with empty/blank `reason` → `CONFIGURATION_ERROR`, no row change, no ledger.
- config **fault_inject** row → `CONFLICT`, no ledger.
- `discovery` local-libvirt row → `CONFLICT`, no ledger.
- runtime row deregister (existing tests) → still works, **no** ledger entry written.

**Acceptance:** the spec's first three success bullets + "discovery row still rejected" + "runtime
deregister unchanged" hold; a reconcile pass with the host still in the file does not re-create it
(covered by a B1/B5 integration-style test or deferred to the existing A reconcile suite — assert at
least the ledger entry exists so reconcile suppresses it).

**Rollback/cleanup:** none beyond the transaction (atomic). No migration.

---

## Task B2 — config-owned build-host remove writes `removed`

**Files:** `src/kdive/mcp/tools/ops/build_hosts/lifecycle.py`;
`tests/mcp/ops/` build-host lifecycle test (locate the existing `remove_build_host` test;
`rg -l remove_build_host tests/`).

**Where it fits:** spec §2 — the build-host analogue of B1.

**Behavior:**

1. Add an optional `reason: str = ""` parameter to `remove_build_host` and its registrar tool.
2. Keep `worker-local` rejection (CONFLICT) and absent-name (`not_found`) first.
3. SELECT the row's `managed_by` directly (the `BuildHost` dataclass lacks it):
   `SELECT id, managed_by FROM build_hosts WHERE name = %s` (or extend `get_by_name` usage with a
   direct managed_by read).
4. `runtime`-owned host → existing plain-delete path, **no** `reason` required, **no** ledger.
5. `config`-owned host → require non-empty `reason` (else `CONFIGURATION_ERROR`); run one
   `conn.transaction()` holding `advisory_xact_lock(conn, LockScope.BUILD_HOST, name)`; inline (or
   call within the same txn) the FK-safe disposition: `SELECT id FROM build_hosts WHERE id = %s FOR
   UPDATE`, check `build_host_leases` first — if leased → `UPDATE build_hosts SET enabled = false`
   (cordon), else `DELETE`; then `set_override(conn, OverrideIdentity(BUILD_HOST,
   BUILD_HOST_RESOURCE_KIND, name), disposition=REMOVED, reason=reason, actor=...)`; audit.
   - **Serialization (mirror the helper):** the parent `build_hosts` row must be SELECTed
     `FOR UPDATE` **before** the `build_host_leases` check (as written above). This is the exact lock
     `prune_or_cordon_build_host` relies on — it conflicts with the implicit `FOR KEY SHARE` a
     concurrent lease INSERT takes on the parent row, so a lease cannot land between the liveness
     check and the delete to hit `ON DELETE RESTRICT` mid-pass. Do **not** instead lock only the
     `build_host_leases` rows.
   - **Note:** `prune_or_cordon_build_host` opens its own transaction — so for the config path,
     **inline** the FOR UPDATE + lease-check + cordon/delete inside the tool's single transaction so
     the `set_override` is atomic with the row change (mirror the resource inline pattern).
6. Success envelope reports `removed` (deleted) or `cordoned` (disabled) disposition.

**Tests (TDD):**
- config build-host, idle + `reason` → deleted, `removed` ledger entry (PK uses the `build-host`
  sentinel + `build_host` source).
- config build-host with an in-flight lease + `reason` → `enabled=false` (cordoned), `removed`
  ledger entry, row present, no aborted pass.
- config build-host empty `reason` → `CONFIGURATION_ERROR`, no change, no ledger.
- runtime build-host remove → plain delete, no ledger (existing behavior).
- `worker-local` → CONFLICT regardless of reason.

**Acceptance:** the build-host success bullet holds; the FK `ON DELETE RESTRICT` never aborts a pass.

**Rollback/cleanup:** none beyond the transaction.

---

## Task B3 — `ops.set_host_capacity` detaches a config row

**Files:** `src/kdive/mcp/tools/ops/tuning.py` (localized to `set_host_capacity` + helpers + imports
— the C-coordination zone); `tests/mcp/ops/test_ops_tuning.py` (extend).

**Where it fits:** spec §3 — the in-place modify path that must survive reconcile.

**Behavior:**

1. In `set_host_capacity`, replace the blind `_update_host_cap` with a read-then-lock-then-write
   sequence in one `conn.transaction()`:
   - `SELECT id, kind, name, managed_by FROM resources WHERE id = %s FOR UPDATE`; missing →
     `CONFIGURATION_ERROR` (unchanged envelope).
   - take `resource_identity_lock(conn, ResourceKind(kind), name)`.
   - merge the cap (the existing `capabilities || jsonb_build_object(...)` UPDATE).
   - **only if** `managed_by == ManagedBy.CONFIG.value`: `set_override(conn,
     OverrideIdentity(RESOURCE, kind, name), disposition=DETACHED, reason="set_host_capacity",
     actor=actor_for(ctx))`. A `runtime`/`discovery` row writes no entry.
   - audit (existing `_audit_applied`).
2. Keep the `_parse_cap` / `_parse_resource_id` validation unchanged.

**Tests (TDD):**
- config host: set cap → `detached` ledger entry present; a reconcile pass with a differing file cap
  leaves the runtime cap intact (assert the cap value after a reconcile pass, reusing the A
  reconcile test harness, or assert the entry exists and reference A's detached-skip behavior).
- runtime host: set cap → **no** ledger entry.
- missing id → `CONFIGURATION_ERROR`.

**Acceptance:** the set_host_capacity success bullet holds.

**Rollback/cleanup:** none. **C-merge note:** only `set_host_capacity`, its helpers, and the import
block change; `export_*` (C) is untouched.

---

## Task B4 — `inventory.clear_override` tool (new; three registrations)

**Files (new):** `src/kdive/mcp/tools/ops/inventory_overrides.py` (handler + registrar) — or extend
an existing ops module; keep it out of `resources/` and `build_hosts/` since it spans both. Wire its
registrar into `mcp/app.py` via a `_pool_only_plane_registrar` entry. **Three registrations:** the
registrar `@app.tool`, `tests/mcp/core/test_tool_docs.py`, and `exposure.py` **`CLASSIFIED_TOOLS`**
with a `_PLAT_ADMIN` scope (**not** `PUBLIC_TOOLS` — this is a gated admin mutation).

**Where it fits:** spec §4 — the re-add path (clears a `removed`/`detached` entry).

**Behavior:**

1. Tool `inventory.clear_override(source_kind, resource_kind, name)` — `platform_admin`, mutating.
2. Validate inputs against the ledger enums **before** any DB read:
   - `source_kind` must parse to `InventorySourceKind` → else `CONFIGURATION_ERROR`.
   - for `BUILD_HOST`, `resource_kind` must equal `BUILD_HOST_RESOURCE_KIND` (`"build-host"`); for
     `RESOURCE`, `resource_kind` must parse to `ResourceKind`. An illegal pairing →
     `CONFIGURATION_ERROR`.
3. One `conn.transaction()` holding the matching per-identity lock (resource →
   `resource_identity_lock(ResourceKind(resource_kind), name)`; build-host →
   `advisory_xact_lock(LockScope.BUILD_HOST, name)`); call `clear_override(conn, identity)`.
   - returns `False` (no entry) → `not_found` (idempotent; second clear also `not_found`).
   - returns `True` → success; audit one `platform_audit_log` row.

**Tests (TDD):**
- removed config resource → clear → success; ledger entry gone; (optionally) a reconcile pass
  re-creates the config row.
- removed config build-host → clear → success; entry gone.
- clear with no entry → `not_found`; a second clear → `not_found`.
- unknown `source_kind` → `CONFIGURATION_ERROR`; build_host + a non-sentinel `resource_kind` →
  `CONFIGURATION_ERROR`.
- non-admin → `authorization_denied` (audited iff holds ≥1 platform role).
- the three-registration guards (`test_tool_docs`, `test_exposure`, `test_app`) pass.

**Acceptance:** the clear-override success bullet holds; the re-add path works with no file edit.

**Rollback/cleanup:** delete the new module + its three registrations if reverted.

---

## Task B5 — confirm a runtime-added host survives reconcile

**Files:** `tests/mcp/ops/test_resources_mutation.py` or a reconcile test
(`tests/.../test_reconcile_resources*` — locate with `rg -l reconcile_resources tests/`).

**Where it fits:** spec §5 — locks in the add criterion (behavior already exists from M2.6).

**Behavior:** test-only. Register a `register_remote_libvirt` host (`managed_by='runtime'`), assert
no `inventory_overrides` entry exists for its identity, run a reconcile pass (with the runtime host
**not** in the file), and assert the runtime row is **not** pruned (it survives because prune only
touches `managed_by='config'` rows).

**Acceptance:** the runtime-add survives bullet holds.

---

## Sequencing

```
B1 (deregister) ─┐
B2 (build-host) ─┼─ independent; any order
B3 (capacity)  ──┤
B4 (clear)     ──┘  (B4's reconcile-re-create test benefits from B1/B2 but does not depend on them)
B5 (runtime-add survives) ── independent, test-only
```

All five tasks are mostly independent (disjoint files except B1/B4 both touch `resources/`). Tightly
coupled enough (shared inline-disposition pattern, shared ledger repo) that this session implements
them directly in dependency-free order rather than fanning out parallel mutating subagents on one
working tree.

## Self-review

- **Spec coverage:** §1→B1, §2→B2, §3→B3, §4→B4, §5→B5. The disposition-idempotency, reason-threading,
  enum-validation, and txn/lock decisions from the hardened spec are carried into each task verbatim.
- **No migration / no schema change** (A owns the table) — called out so an implementer does not add
  a stray migration and collide with the version-walk assertions.
- **Three-registration rule** is named explicitly for B4 (the only new tool).
- **tuning.py C-coordination** is flagged in B3 so the serialize-merge conflict stays small.
- **No placeholders / no TBD.**
