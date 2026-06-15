# ADR 0115 — Declarative cost-class coefficients in `systems.toml`

- **Status:** Proposed
- **Date:** 2026-06-15
- **Designed by:** [`../design/declarative-cost-class-coefficients.md`](../design/declarative-cost-class-coefficients.md)
- **Extends:** [ADR-0007](0007-metering-budgets-admission.md) §1 (the kcu cost model — `rate = coeff(cost_class) × (W_CPU·vcpus + W_MEM·memory_gb)`)
- **Composes with:** [ADR-0112](0112-systems-inventory-config.md) (systems.toml inventory + reconcile)
- **Deferred sibling:** [#429](https://github.com/randomparity/kdive/issues/429)

## Context

ADR-0007 §1 makes `coeff(cost_class)` the only per-class price input, resolved at admission from
`cost_class_coefficients` and **failing closed** when a row is absent. The `cost_class` *label* is
declarative — authored per host in `systems.toml` or `resources.register_*` — but the
*coefficient* has no declarative source: it is set only by hardcoded seed migrations
(`0002` `local`, `0032` `remote`) or the imperative `ops.set_cost_class_coeff` tool.

This asymmetry has two costs. There is no reviewable, reproducible base costing model: pricing
lives in migrations and runtime calls rather than in the file an operator version-controls. And
because `resources.cost_class` is an unconstrained `text` column, a host declared with any label
that lacks a coefficient is admitted into the catalog but denied **every** allocation with an
opaque `configuration_error{cost_class}` at allocation time — a wall the `0032` seed only
point-patched for `remote`.

Every other declarable knob already pairs a declarative baseline with a runtime override
(`concurrent_allocation_cap` in the file, `ops.set_host_capacity` at runtime). Coefficients lack
the declarative half.

## Decision

### 1. A `[[cost_class]]` table in `systems.toml`, reconciled into `cost_class_coefficients`

`InventoryDoc` gains `cost_class: list[CostClassEntry]` (`name: str`, `coeff: Decimal`). The
name/coeff rule (non-blank name; finite `coeff > 0`; parsed via `Decimal(str(value))`) is extracted
into **one neutral helper module** (e.g. `domain/cost_class_rules.py`) that both the inventory
validator and `ops.set_cost_class_coeff` call, so the two surfaces share code and cannot diverge.
Inventory must **not** import `mcp/tools/ops` for this (a core→tool layering inversion; inventory
imports nothing from `mcp/` today). The shared helper raises a neutral `ValueError`; each caller
maps it to its own error type — `InventoryError` at load for the file, `CONFIGURATION_ERROR` for
the tool. A duplicate name in the file is an `InventoryError`.

### 2. File-authoritative for declared classes; `ops` owns the rest

A dedicated `inventory/reconcile_coefficients.py` pass upserts each declared `(name, coeff)`
(`ON CONFLICT … DO UPDATE`), re-asserting the file value on every reconcile, including the
continuous reconciler loop. A class **named in the file** is file-owned: a runtime
`ops.set_cost_class_coeff` override on it is transient. A class **not** in the file is `ops`-owned
and never touched by reconcile. The migration-seeded `local`/`remote` (= 1.0) remain the
irreducible floor for the absent/empty-file case.

### 3. Upsert-only — never prune

Reconcile never deletes a coefficient. A class removed from the file simply stops being
re-asserted; its last value persists. This keeps the pass trivial and makes it impossible for
reconcile to strand a host or misprice in-flight work by deleting a coefficient still in use.

There is **no removal path today**: neither reconcile (this rule) nor the `ops` surface
(`ops.set_cost_class_coeff` only upserts; there is no unset) can delete a coefficient row, so
removing a `[[cost_class]]` block from the file is intentionally a no-op, not an effective delete.
This is a deliberate trade for safety; a coefficient-unset capability is out of scope (a possible
follow-up). An orphaned, unreferenced coefficient is harmless — nothing prices against a
`cost_class` no host carries.

### 4. Ordered before the resource pass

The coefficient pass runs **before `reconcile_resources` in every orchestrator that reconciles
resources** — both the background loop (`reconciler/inventory.py::run`) and the on-demand MCP tool
`ops.reconcile_systems` (which chains `reconcile_resources` directly, not via the loop). It must be
in both, via a shared ordered helper, or the on-demand path silently skips pricing. When the file
declares both a host and a matching `[[cost_class]]` block, the class is then priced in the same
reconcile run that creates the host — no unpriced-cost_class wall. (The images-only CLI
`reconcile_systems` reconciles no resources and is unchanged.)

**Scope of the guarantee.** The wall is closed for a config host **whose `cost_class` is priced** —
a matching `[[cost_class]]` block, or a seeded class (`local`/`remote`). It is *not* an unconditional
property of "any config-declared host": a `[[remote_libvirt]]`/`[[fault_inject]]` block may name a
`cost_class` for which no `[[cost_class]]` block (and no seed) exists, and that host hits the same
denial. Two cases this does not cover:

- **Host class with no matching coefficient block.** A config host declaring `cost_class = "premium"`
  with no `[[cost_class]] premium` block (and no seed) is unpriced and hits the wall. A *load-time*
  cross-check ("every host `cost_class` is priced") is not free: the inventory loader is pure and
  cannot see the DB seed floor (`local`/`remote` are not in the file), so it cannot distinguish
  "unpriced" from "priced by a seed". A *reconcile-time* warning is feasible (the pass has DB
  access and runs after the coefficient upsert, so it can see seeds), but is left as a possible
  follow-up, not solved here.
- **`resources.register_*` hosts.** The runtime registration path carries an operator-supplied
  `cost_class` and seeds **no** coefficient, so a novel class there hits the same denial. Price the
  class first — a reconciled `[[cost_class]]` block, or `ops.set_cost_class_coeff` before
  registering. A register-time preflight is out of scope here (a possible follow-up).

### 5. Drift is loud

When reconcile overwrites a coefficient whose DB value differs from the file (i.e. it is clobbering
a runtime override), it records a `ReconcileDiff` `warned` entry and an audit line — the one
behavior that *changes* a value is never silent. Detection is **atomic with the write**: the prior value
is taken under a row lock (`SELECT coeff … FOR UPDATE`, then upsert), since plain `… ON CONFLICT DO
UPDATE … RETURNING` yields the post-update row, not the prior `coeff`. So a concurrent
`ops.set_cost_class_coeff` cannot slip between a separate read and the clobber and be reverted
unlogged. Note the complementary case (§3): removing a `[[cost_class]]` block does **not** change
anything (upsert-only), so that no-op is silent by design and must not be mistaken for an effective
delete.

### 6. `ops.export_cost_classes` closes the loop

A read-only `PLATFORM_OPERATOR` tool serializes the live table to a deterministic `[[cost_class]]`
TOML fragment so a break-glass override can be captured back into the file (override → export →
commit → reconcile re-asserts). It returns text and does not write files; full-file regeneration
is #429.

**Which overrides this can capture.** The loop is reliable only for an **ops-owned** class — one
not yet in the file — whose override persists (reconcile never touches it), so a later export
returns it. It does **not** reliably capture an override on an **already-declared** class: per §2
that override is transient, and the continuous reconciler can re-assert the file value (seconds
later) before the operator runs the export, so the export would return the file value, not the
override. For a class already in the file the durable change is to **edit the file directly**, not
override-then-capture.

## Consequences

- The costing baseline becomes a reviewable, reproducible artifact in the inventory file.
- The unpriced-cost_class admission wall closes for a config host **whose `cost_class` is priced**
  (a matching `[[cost_class]]` block or a seed) — not unconditionally for any config-declared host;
  see §4 for the two uncovered cases.
- `ops.set_cost_class_coeff` keeps its role for ad-hoc/**undeclared** classes (durable, and
  capturable into the file via `ops.export_cost_classes`) and for break-glass. On a **declared**
  class its effect is transient and flagged (§5); it is **not** reliably capturable (§6), so the
  durable way to change a declared price is to edit the file.
- The file is authoritative for declared classes by design: a runtime override on one is not a
  durable change. That is the deliberate cost of a reproducible file.
- Budgets/quotas, the `W_CPU`/`W_MEM` weights, coefficient pruning, and whole-file regeneration
  are explicitly excluded; the last is tracked as #429.
