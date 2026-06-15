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

`InventoryDoc` gains `cost_class: list[CostClassEntry]` (`name: str`, `coeff: Decimal`). Validation
reuses the rules `ops.set_cost_class_coeff` already enforces — non-blank name, finite `coeff > 0`,
parsed via `Decimal(str(value))` — so the declarative and imperative surfaces cannot diverge.
A duplicate name in the file is an `InventoryError`.

### 2. File-authoritative for declared classes; `ops` owns the rest

A dedicated `inventory/reconcile_coefficients.py` pass upserts each declared `(name, coeff)`
(`ON CONFLICT … DO UPDATE`), re-asserting the file value on every reconcile, including the
continuous reconciler loop. A class **named in the file** is file-owned: a runtime
`ops.set_cost_class_coeff` override on it is transient. A class **not** in the file is `ops`-owned
and never touched by reconcile. The migration-seeded `local`/`remote` (= 1.0) remain the
irreducible floor for the absent/empty-file case.

### 3. Upsert-only — never prune

Reconcile never deletes a coefficient. A class removed from the file simply stops being
re-asserted (last value persists); deliberate removal is an explicit `ops` act. This keeps the
pass trivial and makes it impossible for reconcile to strand a host or misprice in-flight work by
deleting a coefficient still in use.

### 4. Ordered before the resource pass

The coefficient pass runs in the reconciler loop's inventory pass (`reconciler/inventory.py::run`),
**before** `reconcile_resources`, so a config host's class is priced in the same reconcile run that
creates the host. The unpriced-cost_class admission wall therefore cannot occur for any
config-declared host.

### 5. Drift is loud

When reconcile overwrites a coefficient whose DB value differs from the file (i.e. it is clobbering
a runtime override), it records a `ReconcileDiff` `warned` entry and an audit line. The only
surprising behavior in the model is never silent.

### 6. `ops.export_cost_classes` closes the loop

A read-only `PLATFORM_OPERATOR` tool serializes the live table to a deterministic `[[cost_class]]`
TOML fragment so a break-glass override can be captured back into the file (override → export →
commit → reconcile re-asserts). It returns text and does not write files; full-file regeneration
is #429.

## Consequences

- The costing baseline becomes a reviewable, reproducible artifact in the inventory file.
- The unpriced-cost_class admission wall closes for every config-declared host.
- `ops.set_cost_class_coeff` keeps its role for ad-hoc/undeclared classes and break-glass; its
  transient effect on a *declared* class is explicit and flagged, and capturable via
  `ops.export_cost_classes`.
- A runtime override on a declared class is durable only once captured into the file — the
  deliberate cost of keeping the file authoritative.
- Budgets/quotas, the `W_CPU`/`W_MEM` weights, coefficient pruning, and whole-file regeneration
  are explicitly excluded; the last is tracked as #429.
