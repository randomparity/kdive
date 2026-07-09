# ADR 0319 — Remove dead profile-requirements + BUILD_HOST inventory/lock vestiges

- **Status:** Accepted
- **Date:** 2026-07-09
- **Deciders:** kdive maintainers
- **Spec:** [`../superpowers/specs/2026-07-09-remove-dead-vestiges-1055.md`](../superpowers/specs/2026-07-09-remove-dead-vestiges-1055.md)
- **Follows:** [ADR-0316](0316-remove-server-build-lane.md) — the server-build-lane removal that
  killed the readers for both seams below but deferred their clean removal because each crosses a
  behavior/scope boundary (fixture data + an agent-facing tool parameter).

## Context

ADR-0316 deleted the server-build lane and all `.config` validation, and a follow-up
consolidation dropped `[[build_host]]` inventory, `BuildHostInstance`, the `build_hosts` table,
and the `reconcile/build_hosts` pass. Two seams were left behind as **inert** rather than removed,
because removing them touches artifacts outside a diff-only cleanup:

1. **Profile-requirements config-gating apparatus.** `components/requirements.py`
   (`ConfigRequirements`, `CmdlineRequirements`) survives only as data shapes so the fixture
   catalog still parses — its own module docstring admits "No code reads them for gating." The
   consuming chain is dead: `ProfileRequirements` / `RootfsRequirements` and the
   `ProfileCatalogEntry.requires` field are populated from fixture profile YAML but read by no code
   in `src/` (`FixtureCatalog.profile()` is called, but `.requires` is never read). A materialized
   `fixtures/local-libvirt/configs/console-ready.required.config` is referenced by nothing.

2. **BUILD_HOST inventory/lock vestige.** No `build_host` override row can ever be created
   (the inventory family, its reconcile pass, and its table are gone), and every internal caller of
   `InventorySourceKind` already passes `.RESOURCE`. Yet `InventorySourceKind.BUILD_HOST`,
   `BUILD_HOST_RESOURCE_KIND`, `LockScope.BUILD_HOST`, and the `BUILD_HOST` branches of
   `inventory.clear_override` remain. The `LockScope.BUILD_HOST` docstring was **repurposed** during
   the removal to describe an `inventory.clear_override` per-identity lock — a path that can never
   fire (the resource path locks on `LockScope.RESOURCE` via `resource_identity_lock`), so the
   docstring masks dead code rather than removing it.

Both seams are genuinely dead. Left in place they read as disguised-live code: an agent sees a
`source_kind` parameter that accepts `build_host`, a maintainer sees a `requires:` block that looks
load-bearing.

## Decision

Remove both seams.

**1 — profile-requirements.** Delete `components/requirements.py`. Drop the `requires` field from
`ProfileCatalogEntry`, which makes `ProfileRequirements` and `RootfsRequirements` dead; delete them
too. Strip the `requires:` block from the on-disk fixture profile YAML **and** the embedded
`_PROFILE_YAML` literal in `admin/default_fixtures.py`, and correct that module's docstring (it
still claims the provider "checks a built kernel against" this policy). Delete the orphaned
`fixtures/local-libvirt/configs/console-ready.required.config`. A `ProfileCatalogEntry` keeps only
`provider` / `name` / `arch`; `extra="forbid"` then rejects any stray `requires:` block, so the
fixture YAML and the model stay in lockstep. Update the parse tests.

**2 — BUILD_HOST vestige.** Narrow `InventorySourceKind` to a single `RESOURCE` member and drop the
`BUILD_HOST_RESOURCE_KIND` sentinel. Drop `LockScope.BUILD_HOST` and correct the `LockScope`
class docstring. In `inventory.clear_override`, **remove** the now single-valued `source_kind`
parameter from the agent-facing tool surface (wrapper + `Field`), the handler, and its helpers; the
tool becomes `clear_override(resource_kind, name)`, always building an
`OverrideIdentity(source_kind=RESOURCE, …)` internally. Delete the dead BUILD_HOST branches in
`_parse_override_identity` and `_override_identity_lock`. Update the ledger module docstring
(two-family / sentinel language) and every affected test in lockstep.

**No DB migration.** The `inventory_overrides` table keeps its `source_kind` column and PK
(migration 0046); it stores the constant `'resource'` and carries no `build_host` rows (none could
ever have been written). The column has no CHECK constraint to narrow, and advisory-lock keys derive
from the enum's string value at runtime and are never persisted, so no migration references the
removed `LockScope.BUILD_HOST`.

## Consequences

- `inventory.clear_override` is a **breaking agent-contract change**: callers drop the `source_kind`
  argument. This is a pre-release cleanup with no external consumers; the wrapper docstring and
  `Field` text (the agent-facing contract, per AGENTS.md) are updated in the same change.
- A stored fixture profile YAML that still carries a `requires:` block now fails `extra="forbid"`
  parse. The only shipped profile (`console-ready_x86_64`) is updated in lockstep; an operator's
  hand-authored profile carrying the block would need the block removed (accepted pre-release break;
  the block was never read).
- `InventorySourceKind` remains a (single-member) enum so the DB column↔value mapping stays explicit
  and the diff stays minimal; `lookup_many`, `set_override`, `serialize`, and the reconcile passes
  are unchanged.
- Net removal of a module, three models, a sentinel, an enum member, a lock scope, a tool
  parameter, and an orphaned fixture file — the surface no longer advertises capabilities that do
  not exist.

## Considered & rejected

- **Keep `source_kind`, restrict it to `'resource'`.** Matches the issue's "narrow the surface"
  wording but leaves a required parameter that can take exactly one value — dead flexibility that an
  agent must still supply. Rejected in favor of removing it (no speculative features).
- **Delete `InventorySourceKind` entirely, hardcode `'resource'` in the SQL.** Would touch
  `set_override` / `lookup` / `lookup_many` / `serialize` / two reconcile passes to swap the enum for
  a string literal — more churn than the vestige warrants, and loses the explicit column mapping.
  Rejected; narrow to one member instead.
- **Add a `source_kind = 'resource'` CHECK constraint (new migration).** Hardening against a row
  that no code can write. Out of scope for a dead-code removal and adds a migration the issue
  explicitly did not ask for. Rejected.
- **Keep `RootfsRequirements` (format/root_device/capabilities) as live profile metadata.** It is
  only reachable through the dead `requires` field and read by nothing; keeping it would preserve a
  second inert shape. Rejected — remove with the rest of the chain.
