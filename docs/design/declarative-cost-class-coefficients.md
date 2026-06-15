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
- **Validation shares one rule module with `ops` so the two surfaces cannot diverge.** Extract the
  two rules into a neutral helper module (e.g. `domain/cost_class_rules.py`) that **both**
  `mcp/tools/ops/tuning.py` and the inventory validator import — *not* `inventory/` importing
  `mcp/tools/ops` (inventory imports nothing from `mcp/` today; that core→tool edge is a layering
  inversion). `ops.set_cost_class_coeff` is refactored to call the shared helper too, so the rule
  has a single home:
  - `name` — non-blank.
  - `coeff` — finite and `> 0`, parsed via `Decimal(str(value))` so a TOML float does not introduce
    binary-float drift.
  - Duplicate `name` within the file → `InventoryError` (mirrors the existing instance-name
    uniqueness check in `InventoryDoc`).
  - **Error surface:** the shared helper raises a neutral `ValueError`; the inventory validator
    maps it to `InventoryError` (§6) and `ops` maps it to its `CategorizedError`
    (`CONFIGURATION_ERROR`) — each layer keeps its own error type, the *rule* is shared.

### 2. Reconcile pass

A new single-purpose module `inventory/reconcile_coefficients.py`, run **before
`reconcile_resources` in every orchestrator that reconciles resources**. There are **two** such
orchestrators today, and both chain `reconcile_resources` independently — the pass must be added to
both or the on-demand path silently skips pricing:

- the background reconciler loop, `reconciler/inventory.py::run` (`reconcile_images` →
  `reconcile_resources` → `reconcile_build_hosts`);
- the on-demand MCP tool `ops.reconcile_systems` (`mcp/tools/ops/reconcile_systems.py`, which calls
  `reconcile_images`/`reconcile_resources`/`reconcile_build_hosts` directly, **not** via
  `InventoryReconcilePass.run`).

To keep the two in lockstep (and stop a future third caller from reintroducing the gap), extract
the ordered chain into one shared helper both call. The images-only CLI `reconcile_systems`
(`inventory/reconcile_cli.py`) does not reconcile resources and needs no change.

- Upserts each declared `(name, coeff)`:
  `INSERT INTO cost_class_coefficients (cost_class, coeff) VALUES (%s, %s)
   ON CONFLICT (cost_class) DO UPDATE SET coeff = EXCLUDED.coeff` — **file-authoritative**: a
  declared class is re-asserted to the file value on every pass (including the continuous
  reconciler loop).
- Running before the resource pass means a same-file host that declares **both**
  `cost_class = "premium"` **and** a matching `[[cost_class]] premium` block is **priced before its
  row is created**, so admission cannot hit the unpriced-cost_class wall. This is the Finding-1
  dissolution: the coefficient is in place in the same reconcile run that creates the host. The
  guarantee is contingent on the matching block existing — a host whose `cost_class` has no
  `[[cost_class]]` block and no seed is still unpriced (see Consequences scope note).
- **Upsert-only — never deletes.** A class removed from the file simply stops being re-asserted;
  its last value persists. There is **no removal path today** — reconcile never prunes and the
  `ops` surface only upserts (`ops.set_cost_class_coeff`, no unset) — so removing a `[[cost_class]]`
  block is intentionally a no-op, not an effective delete; a coefficient-unset capability is out of
  scope. Undeclared classes (the migration-seeded floor and any ad-hoc `ops`-set class) are left
  untouched. An orphaned, unreferenced coefficient is harmless (nothing prices against a
  `cost_class` no host carries).

### 3. Loud drift flagging

Drift detection is **atomic with the write**, so a concurrent `ops.set_cost_class_coeff` cannot
slip between a read and the clobber and be reverted unlogged. Each row is handled in one
transaction that takes the prior value under a row lock — `SELECT coeff … FOR UPDATE`, then the
upsert (or a `FOR UPDATE` CTE feeding the insert). (Plain `INSERT … ON CONFLICT DO UPDATE …
RETURNING` returns the *post*-update row, not the prior `coeff`, so it cannot supply the "was Y"
on its own — the locked read is required.) When the prior value differs from the file value, the
pass records a `warned` entry in
the `ReconcileDiff` **and** writes an audit line (`coefficient 'X' re-asserted from file: was Y,
now Z`). So the one behavior that *changes* a value — reconcile clobbering a runtime override on a
*declared* class — is never silent, even under a concurrent `ops` write. An idempotent re-run
(file == DB) produces no drift entry and no audit noise. (The complementary no-op — removing a
`[[cost_class]]` block, which changes nothing per §2 — is silent by design and must not be read as
an effective delete.)

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
  reconcile re-asserts from the file). This reliably captures an override only on an **ops-owned**
  class (one not yet in the file), whose value persists; an override on an **already-declared**
  class is transient (§4) and the continuous reconciler can clobber it back to the file value
  before the export runs, so for those the durable change is to **edit the file directly**.
- It **returns text; it does not write files** (writing stays the operator's/agent's job). A
  `--path`/write variant and full-file regeneration are out of scope (→ #429).

### 6. Error handling

- Invalid `name`/`coeff` in the file → `InventoryError` at load (the inventory validator maps the
  shared rule module's neutral `ValueError`; see §1). Consistent with all other inventory
  validation, the whole reconcile of that file aborts (fail fast, clear message).
- Coefficient parsing uses `Decimal(str(value))`, the shared rule the `ops` tool also calls.

## Testing

- **Model:** parses `[[cost_class]]`; rejects blank `name`, `coeff ≤ 0`, non-finite `coeff`,
  duplicate `name`.
- **Reconcile:** upserts declared coeffs; the file value overrides an existing row; drift is
  flagged in the diff and audited; undeclared / `ops`-set / migration-floor classes are
  untouched; removal does **not** delete; re-run is idempotent (no drift).
- **Finding-1 regression (both orchestrators):** declare a host with a custom `cost_class` plus its
  `[[cost_class]]`; reconcile via **each** resource-reconciling path — the background loop **and**
  `ops.reconcile_systems` — and assert the coefficient row exists, ordering held (coeff before the
  host), and `allocations.request` is admitted (no `configuration_error{cost_class}`). The
  on-demand path is the one that silently skipped pricing before §2's fix, so it must be pinned.
- **Export tool:** returns deterministic TOML for the current table; enforces the
  `PLATFORM_OPERATOR` gate; round-trips (export → parse → reconcile → identical table).
- **Floor:** an absent file leaves `local`/`remote` *priced* — the seed rows survive, so
  `resolve_coeff` succeeds for them (grantability also needs a host/budget/quota and is covered by
  the admission tests, not this one).
- **Drift under concurrency:** a coefficient upsert racing a concurrent `ops.set_cost_class_coeff`
  on the same class still emits the `warned`/audit drift record (atomic detection, §3).

## Components and their boundaries

| Unit | Does | Depends on |
|------|------|-----------|
| `CostClassEntry` / `InventoryDoc.cost_class` | Parse + validate the `[[cost_class]]` declarations | Pydantic, `domain/cost_class_rules` (shared with `ops`) |
| `domain/cost_class_rules` | The one name/coeff rule, shared by the inventory validator and `ops` | — (neutral; raises `ValueError`) |
| `inventory/reconcile_coefficients.py` | Upsert declared coeffs file-authoritatively; flag drift; never delete | `cost_class_coefficients` table, `ReconcileDiff`, audit |
| coefficient-before-resources ordering helper | Shared by the loop and `ops.reconcile_systems` so both price before creating hosts | `reconcile_coefficients`, `reconcile_resources` |
| `ops.export_cost_classes` | Serialize the live table to `[[cost_class]]` TOML | `cost_class_coefficients`, platform auth |

## Consequences

- Operators get a reproducible, reviewable base costing model; pricing leaves buried SQL/runtime
  calls for the inventory file.
- The unpriced-cost_class admission wall closes for a config host **whose `cost_class` is priced**
  (a matching `[[cost_class]]` block, or a seed) — not unconditionally for any config-declared host
  (Finding 1). **Two uncovered cases:** (a) a config host declaring a `cost_class` with no matching
  `[[cost_class]]` block and no seed is still unpriced and hits the wall — a *load-time* host↔class
  cross-check is non-trivial because the pure loader can't see the DB seed floor, though a
  *reconcile-time* warning is feasible (the pass has DB access); left as a possible follow-up;
  (b) a host created via `resources.register_*` carries an operator-supplied `cost_class`
  and seeds no coefficient, so a novel class there hits the wall unless priced first (a reconciled
  `[[cost_class]]` block, or `ops.set_cost_class_coeff`). A register-time preflight is a possible
  follow-up, out of scope here.
- `ops.set_cost_class_coeff` keeps its role for ad-hoc/**undeclared** classes (durable, and
  capturable into the file via `ops.export_cost_classes`) and for break-glass. On a **declared**
  class its effect is transient and flagged (§3); it is **not** reliably capturable (§5), so the
  durable way to change a declared price is to edit the file.
- The file is authoritative for declared classes: a runtime override on one is not a durable
  change — the deliberate cost of a reproducible file.
- The full-fidelity "regenerate the whole `systems.toml`" capability (which would also capture
  `ops.set_host_capacity` overrides) is tracked separately as #429.
