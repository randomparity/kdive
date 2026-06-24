# ADR 0231 — buildconfig.delete + buildconfig.list for operator-published fragments

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-23
- **Deciders:** KDIVE maintainers

## Context

The build-config catalog (ADR-0096) holds named kernel-config fragments. ADR-0119 added
the operator write-path (`buildconfig.set`, `source='operator'`) and ADR-0122 made
`systems.toml` file-authoritative (`source='config'`, re-asserted every reconcile pass),
alongside the packaged `source='seed'` baseline. The MCP surface exposes only
`buildconfig.get` (read one by name) and `buildconfig.set` (publish/replace one).

Two gaps remain (#751, black-box review #746): an operator-published fragment can be
overwritten but never **removed**, leaving catalog residue, and there is no way to
**enumerate** the catalog. The sibling catalogs already close this asymmetry —
`images.delete`/`images.list` and `shapes.delete`/`shapes.list` both exist. The data layer
has neither a select-all nor a delete: `_SELECT` is single-name and the only mutators are
the three `upsert_*` functions.

## Decision

We will add `buildconfig.list` and `buildconfig.delete` MCP tools, mirroring the
`shapes.list`/`shapes.delete` catalog precedent, backed by two new queries in
`build_configs/catalog.py`.

- `buildconfig.list` returns every catalog row as a sorted collection of identity +
  provenance (`name`, `sha256`, `source`, `description`) — **not** the fragment bytes.
  Auth is authenticated-only (no project RBAC), the same gate as `buildconfig.get` and
  `images.list`/`shapes.list`; it joins `PUBLIC_TOOLS`.
- `buildconfig.delete` removes a fragment **only when `source='operator'`**. It refuses a
  `seed` or `config` row with a `configuration_error` carrying `data.reason =
  "not_operator_source"` and `data.source`, and treats a missing name as
  `configuration_error` with `data.reason = "not_found"`. Auth is `platform_admin` and
  audited, mirroring `buildconfig.set` exactly (denial audited iff the caller holds ≥1
  platform role; only a successful removal writes a success audit row). It joins
  `_TOOL_SCOPES` as `_PLAT_ADMIN`.
- The data-layer `delete_operator_build_config` deletes `WHERE name = … AND source =
  'operator'` and, in the same transaction, reads the row's provenance so the handler can
  distinguish refused-non-operator from not-found without a check-then-act race.

## Consequences

- Operators gain full lifecycle (list/get/set/delete) over operator fragments, symmetric
  with images and shapes.
- The `source='operator'` delete predicate enforces ADR-0122 at the data layer: a `config`
  row cannot be deleted out from under its authoritative `systems.toml` declaration (the
  reconcile pass would re-create it anyway), and the packaged `seed` baseline is protected.
  The refusal is explicit and structured, not a silent no-op.
- No migration: the `source` column and `build_config_catalog` table already exist; this is
  query + tool + exposure-map work only.
- New obligations: two new tool registrations triaged in `mcp/exposure.py`
  (`buildconfig.delete` → classified `_PLAT_ADMIN`; `buildconfig.list` → `PUBLIC_TOOLS`),
  which the completeness guard (`tests/mcp/core/test_app.py`) requires.
- The fragment's object-store bytes are intentionally left in place on delete (see below).

## Alternatives considered

- **Hard-delete any row regardless of source.** Rejected: a `config` row is re-asserted by
  the next reconcile pass, so deleting it is a confusing no-op-with-residue, and a `seed`
  baseline is not an operator's to remove at runtime. The `source='operator'` predicate is
  the ADR-0122 invariant made enforceable.
- **Silently ignore a seed/config delete (return success).** Rejected: the acceptance
  criterion requires a *clear reason*; a silent success hides that nothing happened.
- **Also delete the object-store bytes on fragment delete.** Rejected for this change: the
  catalog row is the index of record and `buildconfig.get`/resolution go through the row, so
  an orphaned object is unreferenced and harmless; deleting bytes adds a cross-store
  transaction and a failure mode (PUT-succeeded/row-gone vs. row-gone/DELETE-failed) for no
  correctness gain. Object lifecycle/GC is a separate concern, matching how `set` replaces
  bytes by writing a new object without reclaiming the prior one.
- **A read-then-delete in the handler.** Rejected: two statements open a race where a
  concurrent `set` flips `source` between the read and the delete. A single delete scoped to
  `source='operator'` plus an in-transaction provenance read keeps the decision atomic.
- **Return the bytes in `buildconfig.list`.** Rejected: the list is a catalog index;
  `images.list`/`shapes.list` return identity + state, not payload, and `buildconfig.get`
  already serves bytes by name. Inlining every fragment's bytes bloats the list response.
