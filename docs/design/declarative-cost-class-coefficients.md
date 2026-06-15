# Design — Declarative cost-class coefficients in `systems.toml`

- **Status:** Proposed
- **Date:** 2026-06-15
- **Formal decision:** [ADR-0115](../adr/0115-declarative-cost-class-coefficients.md)
- **Extends:** [ADR-0007](../adr/0007-metering-budgets-admission.md) §1 (the kcu cost model), [ADR-0112](../adr/0112-systems-inventory-config.md) (systems.toml inventory + reconcile)
- **Deferred sibling:** [#429](https://github.com/randomparity/kdive/issues/429) (full systems.toml regeneration)

## Problem

A host's price is set by its `cost_class` label and that label's coefficient in the
`cost_class_coefficients` table. The label is **declarative** — authored per host in
`systems.toml` (`inst.cost_class`) or via `resources.register_*`. The coefficient is **not**:
the only ways a coefficient enters the database are (a) hardcoded `INSERT`s in seed migrations
(`0002` → `local`, `0032` → `remote`) and (b) the imperative `ops.set_cost_class_coeff` MCP tool.

There is no declarative, version-controlled source for the costing model. To stand up a
deployment with any pricing beyond the two baked-in defaults, an operator must issue a hand
sequence of `ops.set_cost_class_coeff` calls — there is no "edit a file, reconcile, done" path
like the rest of the fleet has. Two concrete consequences:

1. **No reproducible baseline.** Pricing is not in the file an operator version-controls and
   reviews; it lives in migrations and runtime calls.
2. **The unpriced-cost_class admission wall.** `resources.cost_class` is a free `text` column
   with no FK; reconcile/register accept any label. Admission resolves the coefficient
   fail-closed (`resolve_coeff`), so a host whose `cost_class` has no coefficient is denied every
   allocation with an opaque `configuration_error{cost_class}` at allocation time — the same
   class of wall the `0032` `remote` seed only point-patched.

Every other declarable knob already follows declare-in-file + runtime-override (e.g.
`concurrent_allocation_cap` in `systems.toml`, overridable via `ops.set_host_capacity`).
Coefficients are missing the declarative half.

## Goal

Give coefficients the declarative half: a `[[cost_class]]` table in `systems.toml`, reconciled
into `cost_class_coefficients`, so the costing baseline is a reviewable, reproducible artifact —
and so a host and its price land together in one reconcile, dissolving the admission wall for
config-declared hosts.

## Scope

In scope: cost-class coefficients only.

Out of scope (YAGNI): per-project budgets/quotas; the global `W_CPU`/`W_MEM` weights;
coefficient pruning; full-file regeneration (→ #429); file-writing from the export tool.

## Design

### 1. Data model

A new array-of-tables in the v2 inventory document:

```toml
[[cost_class]]
name  = "remote"
coeff = 2.5
```

- New `CostClassEntry` Pydantic model in `inventory/model.py`: `name: str`, `coeff: Decimal`.
- `InventoryDoc` gains `cost_class: list[CostClassEntry] = Field(default_factory=list)`.
- **Validation reuses the existing `ops` rules so the two surfaces cannot diverge:**
  - `name` — non-blank (the rule `tuning._validate_cost_class` enforces).
  - `coeff` — finite and `> 0` (the rule `tuning._parse_positive_coeff` enforces). Parsed via
    `Decimal(str(value))` so a TOML float does not introduce binary-float drift.
  - Duplicate `name` within the file → `InventoryError` (mirrors the existing instance-name
    uniqueness check in `InventoryDoc`).

### 2. Reconcile pass

A new single-purpose module `inventory/reconcile_coefficients.py`, invoked from the reconciler
loop's inventory pass (`reconciler/inventory.py::run`, which today chains
`reconcile_images` → `reconcile_resources` → `reconcile_build_hosts`), **ordered before
`reconcile_resources`**. That loop is where config resources are reconciled, so coefficients live
beside them; the CLI `reconcile_systems` is images-only today and is left unchanged.

- Upserts each declared `(name, coeff)`:
  `INSERT INTO cost_class_coefficients (cost_class, coeff) VALUES (%s, %s)
   ON CONFLICT (cost_class) DO UPDATE SET coeff = EXCLUDED.coeff` — **file-authoritative**: a
  declared class is re-asserted to the file value on every pass (including the continuous
  reconciler loop).
- Running before the resource pass means a same-file host
  (`cost_class = "premium"` + a `[[cost_class]]` for `premium`) is **priced before its row is
  created**, so admission cannot hit the unpriced-cost_class wall for anything declared in the
  file. This is the Finding-1 dissolution: the coefficient is in place in the same reconcile run
  that creates the host.
- **Upsert-only — never deletes.** A class removed from the file simply stops being re-asserted;
  its last value persists. Removal is a deliberate `ops` act, never a silent reconcile side
  effect. Undeclared classes (the migration-seeded floor and any ad-hoc `ops`-set class) are
  left untouched.

### 3. Loud drift flagging

Before each upsert the pass reads the existing `coeff`. If it differs from the file value the
pass records a `warned` entry in the `ReconcileDiff` **and** writes an audit line
(`coefficient 'X' re-asserted from file: was Y, now Z`). This makes the one genuinely surprising
behavior — reconcile clobbering a runtime `ops.set_cost_class_coeff` override on a *declared*
class — visible rather than silent. An idempotent re-run (file == DB) produces no drift entry and
no audit noise.

### 4. Authority model

- **File-declared class** → file owns it; an `ops.set_cost_class_coeff` override on it is
  transient (re-asserted next pass, surfaced as drift). The durable way to change a declared
  price is to edit the file.
- **Undeclared class** → `ops`-owned; durable; reconcile never touches it.
- **Migration seeds** (`local`/`remote` = 1.0) → the irreducible floor for the absent/empty-file
  case (`systems.toml` is gitignored; "absent file = quiet no-op"). The file overrides a seed if
  it declares that class.

### 5. Capture tool

New MCP tool `ops.export_cost_classes` (`PLATFORM_OPERATOR`, `readOnlyHint`):

- Reads the live `cost_class_coefficients` table and returns it as a deterministic, name-sorted
  `[[cost_class]]` TOML fragment in the response envelope.
- The operator/agent pastes the fragment into `systems.toml` to make a break-glass override
  durable — closing the file-authoritative loop (override at runtime → export → commit →
  reconcile re-asserts from the file).
- It **returns text; it does not write files** (writing stays the operator's/agent's job). A
  `--path`/write variant and full-file regeneration are out of scope (→ #429).

### 6. Error handling

- Invalid `name`/`coeff` in the file → `InventoryError` at load. Consistent with all other
  inventory validation, the whole reconcile of that file aborts (fail fast, clear message).
- Coefficient parsing uses `Decimal(str(value))`, the same path the `ops` tool already trusts.

## Testing

- **Model:** parses `[[cost_class]]`; rejects blank `name`, `coeff ≤ 0`, non-finite `coeff`,
  duplicate `name`.
- **Reconcile:** upserts declared coeffs; the file value overrides an existing row; drift is
  flagged in the diff and audited; undeclared / `ops`-set / migration-floor classes are
  untouched; removal does **not** delete; re-run is idempotent (no drift).
- **Finding-1 regression:** declare a host with a custom `cost_class` plus its `[[cost_class]]`,
  reconcile once, assert `allocations.request` is admitted (no `configuration_error{cost_class}`).
- **Export tool:** returns deterministic TOML for the current table; enforces the
  `PLATFORM_OPERATOR` gate; round-trips (export → parse → reconcile → identical table).
- **Floor:** an absent file leaves `local`/`remote` priced and grantable.

## Components and their boundaries

| Unit | Does | Depends on |
|------|------|-----------|
| `CostClassEntry` / `InventoryDoc.cost_class` | Parse + validate the `[[cost_class]]` declarations | Pydantic, the shared name/coeff rules |
| `inventory/reconcile_coefficients.py` | Upsert declared coeffs file-authoritatively; flag drift; never delete | `cost_class_coefficients` table, `ReconcileDiff`, audit |
| reconcile orchestrator | Run the coefficient pass **before** resources | the two passes |
| `ops.export_cost_classes` | Serialize the live table to `[[cost_class]]` TOML | `cost_class_coefficients`, platform auth |

## Consequences

- Operators get a reproducible, reviewable base costing model; pricing leaves buried SQL/runtime
  calls for the inventory file.
- The unpriced-cost_class admission wall closes for every config-declared host (Finding 1).
- `ops.set_cost_class_coeff` keeps its role for ad-hoc/undeclared classes and break-glass, with
  the transient-on-declared-class behavior now explicit and flagged.
- A runtime override on a declared class is not durable until captured into the file via
  `ops.export_cost_classes` — a deliberate trade for keeping the file authoritative.
- The full-fidelity "regenerate the whole `systems.toml`" capability (which would also capture
  `ops.set_host_capacity` overrides) is tracked separately as #429.
