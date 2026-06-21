# Sub-issue A implementation plan — override ledger + reconcile-model shift (#638)

> Derived from [ADR-0199](../../adr/0199-seed-once-runtime-authoritative-inventory.md) and
> [the M2.7 design](../../design/runtime-mutable-inventory.md). The milestone plan
> ([2026-06-20-runtime-mutable-inventory.md](2026-06-20-runtime-mutable-inventory.md)) locks the
> decomposition and acceptance; this plan is the per-task TDD breakdown for sub-issue **A** only.
> Sub-issues B/C/D are out of scope here.

**Scope:** the foundation change. Add the `inventory_overrides` ledger table, a repository over it,
and make the reconcile inventory pass consult the ledger so runtime mutations win over the file
**without losing drift repair** for identities with no ledger entry. No operator-facing tools land
here (those are B); A is exercised by hand-inserted ledger rows in tests.

**Migration number:** the current head is `0045_allocation_requested_pool.sql`, so this lands as
**`0046_inventory_overrides.sql`** (forward-only, additive — ADR-0015).

**Guardrails before every commit:** `just lint`, `just type` (whole tree), focused `just test`.
The plan/doc commit also runs `just check-mermaid`, `just docs-paths`, `just adr-status-check`.
Zero warnings; doc-style word ban applies (no "critical/comprehensive/robust/significant/…").

---

## Task A1 — Migration + ledger schema

**Files:** `src/kdive/db/schema/0046_inventory_overrides.sql` (new);
`tests/db/test_migrate.py` (version-list + new CHECK guards).

Add `inventory_overrides`:

| column | type | notes |
|---|---|---|
| `source_kind` | text | `'resource'` \| `'build_host'` — the inventory family |
| `resource_kind` | text | the resource `kind` for a resource; sentinel `'build-host'` for a build host |
| `name` | text | the instance name (ADR-0112) |
| `disposition` | text | CHECK in (`'detached'`, `'removed'`) |
| `reason` | text | operator-supplied audit reason |
| `actor` | text | principal that set the override |
| `created_at` | timestamptz | `NOT NULL DEFAULT now()` |

PK `(source_kind, resource_kind, name)`. The PK matches the real inventory identity: `resources`
is unique on `(kind, name)`, and both `remote-libvirt` and `fault-inject` are config-owned there, so
a name can repeat across kinds; build-host names are globally unique, hence the fixed
`resource_kind = 'build-host'` sentinel.

The `disposition` CHECK is named `inventory_overrides_disposition_check` and mirrors a Python
`InventoryOverrideDisposition(StrEnum)` (`detached`, `removed`) — same SQL-CHECK⊆enum contract the
other lifecycle CHECKs follow (`CHECK_ENUMS` in `test_migrate.py`).

**TDD:**
1. Failing test: `test_migration_0046_creates_inventory_overrides_table` asserts the columns and PK
   `(source_kind, resource_kind, name)`.
2. Add `("inventory_overrides_disposition_check", InventoryOverrideDisposition)` to `CHECK_ENUMS`
   (drives the existing parametrized enum⊆SQL guard) and add a bidirectional
   `test_inventory_overrides_disposition_check_admits_exactly_the_enum` (the SQL⊆enum direction the
   `CHECK_ENUMS` parametrize cannot catch — mirrors the 0044 component-uploads test).
3. Failing test: `test_inventory_overrides_disposition_check_rejects_unknown` — an `INSERT` with
   `disposition='bogus'` raises `CheckViolation`.
4. **Update every migration-version-list assertion** to append `"0046"`:
   - `test_rerun_is_a_noop` (the `first == [...]` list)
   - `test_advisory_lock_serializes_migrators` (the post-lock applied list)
   - `test_0042_backfills_target_kind_from_resource_kind` (`assert applied == [...]` tail).
   Discover them with `rg -n 'through_004|schema_migrations|0045' tests/db/`; do not assume a fixed
   set.

**Acceptance:** migration applies; CHECK rejects an unknown disposition; the enum⊆SQL guard and its
bidirectional twin pass; the version-walk assertions pass.

---

## Task A2 — Ledger repository (`src/kdive/inventory/overrides.py`, new)

Pure-ish helpers over an injected `AsyncConnection`/cursor. A small frozen
`InventoryOverride` dataclass carries a looked-up row. An `OverrideIdentity` value
(`source_kind`, `resource_kind`, `name`) keys every call so the PK is expressed once.

- `InventoryOverrideDisposition(StrEnum)` — `DETACHED = "detached"`, `REMOVED = "removed"`. Lives
  here (the inventory family owns it); `test_migrate.py` imports it for `CHECK_ENUMS`.
- `async def set_override(conn, identity, *, disposition, reason, actor) -> None` — upsert
  (`ON CONFLICT (source_kind, resource_kind, name) DO UPDATE`) so re-setting an override for the
  same identity replaces the disposition/reason/actor (a remove-then-re-remove is idempotent).
- `async def clear_override(conn, identity) -> bool` — delete; returns whether a row was removed.
- `async def lookup(conn, identity) -> InventoryOverride | None` — fetch one.
- `async def lookup_many(conn, source_kind) -> dict[(resource_kind, name), InventoryOverride]` —
  bulk fetch for a reconcile pass (one query, not N), keyed for in-loop lookup.
- `async def gc_settled(...)` — see A4; lives here as the deletion primitive, driven from the pass.

The repo does **not** open its own transactions or take locks — the caller (a mutation tool in B, or
the reconcile pass in A) owns the transaction and the per-identity lock so the ledger write and the
row change commit atomically (ADR-0199 concurrency).

**TDD:** unit tests against a real pooled conn (testcontainers): set→lookup round-trip; set is
idempotent-on-conflict (second set replaces, no duplicate-key error); clear returns True then False;
`lookup_many` returns only the requested `source_kind` keyed correctly; lookup of an absent identity
is `None`.

**Acceptance:** every repository helper has a direct unit test covering its happy path and its empty
/ absent / conflict edge.

---

## Task A3 — Reconcile consults the ledger (`reconcile_resources.py` + `reconcile_build_hosts.py`)

The inventory pass already holds `inventory_pass_lock` (session-scoped) for the whole pass and takes
`resource_identity_lock` per identity in create/prune. A3 adds, **inside those locks**, a ledger
read that gates create and prune:

**Resource pass (`_create_config_resources` / `_prune_departed`):**
- Load `lookup_many(conn, 'resource')` once at the top of the pass (under the pass lock).
- **`removed`** for a declared identity → **skip create** (do not upsert the row from the file).
  In the prune sweep, a `removed` identity whose row still exists is **cordoned if live, deleted once
  idle** — reuse `prune_or_cordon_resource`, but drive it from the ledger, because the file still
  declares the identity so the file-departure branch never reaches it.
  **Concrete change required:** `_prune_departed` (`reconcile_resources.py:454-455`) currently does
  `if identity in declared: continue` — a `removed` identity *is* in `declared`, so it is skipped
  before reaching `prune_or_cordon_resource` and the delete-when-idle acceptance silently never
  fires. Change the guard to keep iterating a `removed` identity:
  `if identity in declared and removed.get(identity) is None: continue` (where `removed` is the
  subset of the ledger lookup with `disposition='removed'`). The create side (`_create_config_resources`)
  must `continue` past a `removed` identity before the upsert so it is not re-created.
- **`detached`** for a declared identity → keep the row's **existence/identity** but **do not
  overwrite its runtime-owned fields** (`cost_class`, the `capabilities` sizing/cap, `pool`,
  `host_uri`) from the file. Concretely, in `_upsert_config_resource` (reconcile_resources.py:210-268)
  the **field-overwrite branch is `_update_config_resource`** (the one that writes host_uri/cost_class/
  caps/pool) — for a `detached` row, **skip that call**. The **existence/adoption branch**
  (`_insert_config_resource` when the row is absent, `_adopt_config_resource` when `managed_by`/lease
  ownership is wrong) still runs so a `managed_by` flip is still repaired. If the row is **absent**
  (hand-deleted), the existence branch would re-insert it at file values, which resurrects stale
  values under a still-active override — instead, for a `detached`-and-absent identity **skip the
  insert and let A4 GC the entry** (see A4, which owns this case); the next no-entry pass re-asserts
  the file. Never prune a `detached` identity.
- **no entry** → today's behavior unchanged (create on appear, repair a deleted row, prune/cordon on
  departure). This is the ADR-0021 drift-repair path and must stay byte-for-byte equivalent for a
  no-entry identity.

**Build-host pass (`reconcile_build_hosts.py`):** the same three branches keyed on
`lookup_many(conn, 'build_host')` with the sentinel `resource_kind='build-host'`. `removed` →
skip upsert, cordon-if-live (`enabled=false`) / delete-once-idle via `prune_or_cordon_build_host`;
`detached` → leave the live row, GC if the row is gone; no entry → unchanged.

**Design note — minimal blast radius.** The `detached` "skip field overwrite" is implemented by
*not calling* the upsert's field-update branch when the identity is `detached` and a row exists; the
existence/adoption branch still runs so a `managed_by` flip or a hand-deleted row is still repaired
into existence (then, for a hand-deleted row, GC'd per the note above). Keep the no-entry path
untouched so the regression test holds.

**TDD (each its own failing-first test, in `tests/inventory/`):**
1. `removed` entry + declared host → host **not (re)created** across two passes.
2. `removed` entry + a live (allocation-backed) row → row **cordoned**, not deleted; after the
   allocation goes terminal, a later pass **deletes** the cordoned row.
3. `detached` entry + a file `concurrent_allocation_cap` that differs from the live row → the live
   runtime cap **survives** the pass (file value not written).
4. `detached` entry whose row was hand-deleted → A3 **skips the re-insert** (does not resurrect
   stale values); the **A4 GC** (which owns the detached-absent-row case) drops the entry, and the
   following no-entry pass re-asserts the file (row returns at the file values). This is a
   two-pass behavior: pass *N* sees absent-row+detached and GCs the entry; pass *N+1* (no entry)
   re-creates the row. The test asserts both passes.
5. **Regression (guards ADR-0021):** an identity with **no** entry is still fully drift-repaired —
   re-created when its row is hand-deleted; pruned when it leaves the file. (Two assertions, or two
   tests.)
6. Build-host equivalents of (1), (3), (5) at minimum.

**Acceptance:** all of the above pass; `just type` whole-tree green; the existing
`tests/inventory/` and `tests/adversarial/` inventory tests stay green (no regression to the
no-entry path).

---

## Task A4 — GC step wired into the pass (`reconciler/inventory.py` + the GC primitive in `overrides.py`)

A settled ledger entry is redundant and must be dropped so the ledger stays bounded:

- a **`removed`** entry whose identity is **no longer declared** in the file (the operator exported
  + re-applied, so the file-departure prune now owns the removal);
- a **`detached`** entry whose **file values equal the live row** — "equal" means the exact field set
  the resource upsert change-detects: `host_uri`, `cost_class`, the merged `capabilities` (the
  sizing/cap keys), and `pool` (for a build host: `kind`, `base_image_volume`, `workspace_root`,
  `max_concurrent`). When all of those already match, the override is a no-op and is dropped;
- a **`detached`** entry whose **row no longer exists** (the hand-deleted case). **A4 owns this case
  exclusively** — A3 only skips the re-insert; A4 deletes the entry. Implement it as the *first* GC
  branch (absent row → drop), so the "file values equal live row" comparison is never run against a
  missing row.

The GC runs as a step in the inventory pass, **after** the resource and build-host sub-passes, under
the same pass lock. It reads the parsed `InventoryDoc` (to know what the file declares and its
values) and the live rows, and deletes the settled entries via `clear_override`.

**Placement:** add the GC call to `reconcile_all` (`reconcile_pipeline.py`) as the final step, or to
the resource/build-host passes' tails — decide during implementation by which keeps the doc + live
state already in hand. The pipeline is the natural seam because it already holds `doc` and sequences
the sub-passes; A4's GC is `reconcile_overrides_gc(conn, doc)` called last in `reconcile_all`.

**TDD:**
1. `removed` entry + identity absent from the file → entry **GC'd** after a pass (and the normal
   file-departure prune handles the row).
2. `detached` entry + file values now equal to the live row → entry **GC'd**; the row is untouched.
3. `detached` entry + a still-divergent file value → entry **retained** (not GC'd).
4. (covered by A3.4) `detached` entry + absent row → GC'd.

**Acceptance:** the four GC cases hold; a steady-state pass with no settled entries is a no-op (no
spurious deletes); the adversarial inventory suite stays green.

---

## Sequencing & rollback

A1 → A2 → A3 → A4 (A3 depends on A2's `lookup_many`; A4 depends on A2's `clear_override` and A3's
behavior). Each task is one logical commit. Rollback is `git revert` of the commit(s); the migration
is additive (an empty `inventory_overrides` table is inert — no reconcile reads change behavior until
a ledger row exists), so a forward revert of A3/A4 leaves the table harmlessly present.

## Self-review

- **Spec coverage:** ledger table (A1) → A2 repo → A3 reconcile branches (no-entry / detached /
  removed) → A4 GC. The three ADR-0199 reconcile states and the GC rules each map to a named task and
  a named test. The ADR-0021 drift-repair regression is an explicit A3 test.
- **Concurrency:** every ledger read in A3/A4 runs under the existing `inventory_pass_lock`; no new
  lock is introduced; B's writes (out of scope) will take `resource_identity_lock` in the same txn.
- **No new tool:** A adds no MCP tool, so the three-registration rule does not apply here (it applies
  to B and C).
