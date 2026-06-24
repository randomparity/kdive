# buildconfig.delete + buildconfig.list (operator-published fragments)

- **Issue:** #751 (part of #746, Part 2 black-box review follow-up)
- **ADR:** [0231](../adr/0231-buildconfig-delete-list.md)
- **Status:** Accepted (ADR-0231)

## Problem

`buildconfig.set` publishes an operator kdump fragment and `buildconfig.get` reads one
by name, but the MCP surface has no way to **list** the catalog or **remove** an
operator-published fragment. A throwaway operator fragment can be overwritten in place
(`upsert_operator_build_config`'s `ON CONFLICT DO UPDATE`) but never deleted, leaving
catalog residue. An agent also cannot enumerate which fragments exist or what their
source/provenance is.

This is an asymmetry with the sibling catalogs: `images.delete`/`images.list` and
`shapes.delete`/`shapes.list` both exist.

## Provenance model (background, ADR-0119 / ADR-0122)

`build_config_catalog` rows carry a `source` column with three values, written by three
distinct upsert paths:

- `seed` — packaged default, written by `seed_build_configs` on migrate.
- `operator` — published at runtime via `buildconfig.set`.
- `config` — declared in `systems.toml`, file-authoritative; re-asserted every reconcile
  pass (`upsert_config_build_config`).

The reconcile pass (ADR-0122) treats `systems.toml` as the source of truth for `config`
rows: a `config`-sourced row deleted out from under the file would simply be re-created on
the next reconcile cycle, so deleting it via the MCP surface would be a no-op the operator
would find surprising. A `seed` row is the packaged baseline and is likewise not an
operator's to remove at runtime.

## Requirements

### `buildconfig.list`

- Returns every fragment in the catalog as a collection envelope, sorted by `name`. An
  empty catalog returns an empty collection (`ok`, zero items), not an error. The catalog is
  bounded (a handful of curated fragments), so the list is unpaginated — the `shapes.list`
  precedent, not the `images.list` keyset pagination.
- Per-row data exposes `name`, `sha256`, `source`, and `description` — enough for an
  operator/agent to see what exists and which rows are operator-owned (and therefore
  deletable).
- Does **not** return the fragment bytes (that is `buildconfig.get`'s job; the list is a
  catalog index, mirroring `images.list`/`shapes.list` which return identity + state, not
  payload).
- Auth: authenticated caller only, no project RBAC — the catalog is shared, non-sensitive
  infra (same gate as `buildconfig.get`, `images.list`, `shapes.list`). Goes in
  `PUBLIC_TOOLS`.

### `buildconfig.delete`

- Removes an **operator**-sourced fragment by name: deletes only `WHERE source =
  'operator'`.
- Refuses a `seed` or `config` row with a clear, structured `configuration_error`
  (`data.reason = "not_operator_source"`, `data.source = <actual source>`). The handler
  must distinguish "row exists but is seed/config" from "no such row".
- A name with no matching row is a `configuration_error` (`data.reason = "not_found"`).
- Auth: `platform_admin`, audited — mirrors `buildconfig.set`'s gate exactly. A non-admin
  caller is denied with `authorization_denied`, and the denial is audited iff the caller
  holds ≥1 platform role (the over-reach accountability rule). Goes in `_TOOL_SCOPES` as
  `_PLAT_ADMIN`.
- On success: removes the catalog row, writes a success audit row, returns a `deleted`
  envelope. The object-store bytes are **not** deleted (see ADR rejected alternatives).
- **Concurrency:** the handler acquires `advisory_xact_lock(conn, LockScope.BUILD_CONFIG,
  name)` inside its transaction — the same per-name lock `buildconfig.set` and the seed take
  — so the delete and its provenance-for-reason read are serialized against a concurrent
  `set`/`seed`/`config` write on the same name. Without it, a delete interleaving with a
  committing `set` could report a stale `source` (or `not_found`) in the refusal reason.

## Data layer

Two new queries in `build_configs/catalog.py`:

- `list_build_configs(conn) -> list[BuildConfigEntry]` — `SELECT ... ORDER BY name`
  (select-all, no `WHERE`). Reuses `parse_build_config_row` and `BuildConfigEntry`.
- `delete_operator_build_config(conn, name) -> DeleteOutcome` — a single statement that
  deletes `WHERE name = %(name)s AND source = 'operator'` and reports back enough to
  distinguish the three outcomes (deleted / refused-non-operator / not-found) **without a
  read-then-delete race**. Implemented as a `DELETE ... RETURNING` plus a provenance read
  inside the caller's transaction, or a single SQL that returns the row's source when the
  delete does not fire. The chosen shape returns a small result object the tool maps to an
  envelope.

## Acceptance criteria

1. `buildconfig.list` returns operator + seed + config rows, each with `name`, `sha256`,
   `source`, `description`; sorted by name; bytes not included.
2. `buildconfig.delete` on an operator row removes it; a subsequent `buildconfig.get`
   reports the name as not found and `list` no longer includes it.
3. `buildconfig.delete` on a `seed` row is refused with `configuration_error` and
   `data.reason = "not_operator_source"`; the row is **still present** afterward.
4. `buildconfig.delete` on a `config` row is refused the same way.
5. `buildconfig.delete` on an unknown name is `configuration_error` with `data.reason =
   "not_found"`; no audit row is written (nothing changed).
6. `buildconfig.delete` by a non-`platform_admin` caller is `authorization_denied`; a
   denial audit row is written when the caller holds a platform role; no row is removed.
7. A successful delete writes exactly one `buildconfig.delete` platform-audit row.

## Out of scope

- No DB migration (queries only; `source` column and table already exist).
- No CLI command (`buildconfig.*` has no CLI surface today).
- No object-store byte deletion on fragment delete.
