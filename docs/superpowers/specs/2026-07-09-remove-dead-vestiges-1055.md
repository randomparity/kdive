# Spec — Remove dead profile-requirements + BUILD_HOST inventory/lock vestiges (#1055)

- **Issue:** [#1055](https://github.com/randomparity/kdive/issues/1055)
- **ADR:** [ADR-0319](../../adr/0319-remove-dead-profile-requirements-buildhost-vestiges.md)
- **Type:** refactor (dead-code removal), priority:low
- **Follows:** ADR-0316 (server-build-lane removal), which killed the readers for both seams.

## Problem

Two seams left inert after the server-build-lane removal read as disguised-live code. See ADR-0319
Context for the full inventory. In short: a config-gating apparatus (`ConfigRequirements` /
`CmdlineRequirements` / `ProfileRequirements` / `RootfsRequirements` and the
`ProfileCatalogEntry.requires` field, plus fixture `requires:` blocks and a materialized
`.required.config`) has no reader; and a `BUILD_HOST` inventory/lock family (`InventorySourceKind`
member, `BUILD_HOST_RESOURCE_KIND`, `LockScope.BUILD_HOST`, and the `inventory.clear_override`
`BUILD_HOST` branches) can never be exercised because the build-host inventory family is gone.

## Goal

Delete both seams so the source and the agent-facing surface no longer advertise capabilities that
do not exist, with no behavior change to any live path.

## Scope

### Part 1 — profile-requirements apparatus

Files:

- `src/kdive/components/requirements.py` — **delete** the module.
- `src/kdive/components/catalog.py` — remove `import` of the deleted module; delete
  `ProfileRequirements` and `RootfsRequirements`; remove the `requires` field from
  `ProfileCatalogEntry` (leaving `provider` / `name` / `arch`).
- `src/kdive/admin/default_fixtures.py` — strip the `requires:` block from the `_PROFILE_YAML`
  literal (leaving `provider` / `name` / `arch`); correct the module docstring, which still claims
  the installed bundle carries "the kernel-config/cmdline policy the local-libvirt provider checks a
  built kernel against."
- `fixtures/local-libvirt/profiles/console-ready_x86_64.yaml` — strip the `requires:` block.
- `fixtures/local-libvirt/configs/console-ready.required.config` — **delete** (orphaned; referenced
  by nothing).
- Tests: `tests/provider_components/test_catalog.py`, `tests/admin/test_default_fixtures.py`,
  `tests/mcp/catalog/test_fixtures_validate.py` — update any that construct or assert a `requires:`
  block or the `requires` field.

### Part 2 — BUILD_HOST inventory/lock vestige

Files:

- `src/kdive/inventory/overrides.py` — narrow `InventorySourceKind` to a single `RESOURCE` member;
  delete `BUILD_HOST_RESOURCE_KIND` (and remove it from `__all__`); update the module docstring's
  two-family / `build-host` sentinel language to the single resource family.
- `src/kdive/db/locks.py` — delete `LockScope.BUILD_HOST`; correct the `LockScope` class docstring
  paragraph that describes `BUILD_HOST` as "the `inventory.clear_override` per-identity lock" (the
  resource path locks on `LockScope.RESOURCE`).
- `src/kdive/mcp/tools/ops/inventory.py` — **remove** the `source_kind` parameter from the
  `inventory_clear_override` wrapper (and its `Field`) and the `clear_override` handler. Drop the
  `BUILD_HOST` branches in `_parse_override_identity` (now takes only `resource_kind`, `name`;
  validates `resource_kind` as a `ResourceKind`; builds an `OverrideIdentity` with
  `source_kind=InventorySourceKind.RESOURCE`) and `_override_identity_lock` (always
  `resource_identity_lock`). Also drop the now-constant `'resource'` `source_kind` from **every
  other emitter** in the handler so no agent-facing or audit row carries a field the caller cannot
  set: the success `ToolResponse` `data` payload (agent-facing output), the denial-audit `scope`
  f-string and `args` dict (the f-string references the removed parameter, so it must change
  regardless), and `_audit_clear`'s `scope` string and `args` dict. Each drops to
  `{resource_kind, name}`. Correct **every** docstring in this handler that describes the removed
  parameter or the build-host path — no lint/type/test/`docs-check` guardrail reads docstring prose,
  and this tool is absent from the generated `tools.md`, so a stale docstring survives silently. (The
  `@app.tool` wrapper docstring carries no `source_kind` token today — the wrapper's concrete
  removals are the `source_kind` parameter and its `Field`; keep its prose consistent with the new
  `(resource_kind, name)` signature.) The stale-prose emitters are the handler
  `clear_override` docstring (the "`(source_kind, resource_kind)` pairing" line, the "illegal kind
  pairing" phrasing, and the `source_kind` / `build-host` sentinel `Args`), and the
  `_parse_override_identity` ("Validate the ledger PK … pairing") and `_override_identity_lock`
  ("matching the override's family") helper docstrings.
- Tests: `tests/inventory/test_overrides.py`, `tests/mcp/ops/test_inventory_clear_override.py`,
  `tests/db/test_locks.py` — delete the `build_host` cases; update resource cases that pass
  `source_kind` to the tool.

### Explicitly out of scope

- No DB migration (see ADR-0319 "No DB migration"). The `inventory_overrides.source_kind` column, PK,
  and lack of CHECK are unchanged.
- No change to `set_override` / `lookup` / `lookup_many` / `inventory/serialize.py` / the reconcile
  passes / `mcp/tools/ops/tuning.py` / `resources/deregister.py` — all already use
  `InventorySourceKind.RESOURCE` and stay untouched.

## Acceptance criteria

1. `src/kdive/components/requirements.py` no longer exists; no `src/` or `tests/` reference imports
   `ConfigRequirements` / `CmdlineRequirements` / `ProfileRequirements` / `RootfsRequirements`.
2. `ProfileCatalogEntry` has no `requires` field; `load_fixture_catalog` still parses the shipped
   fixtures, and `console-ready_x86_64.yaml` (provider/name/arch only) validates under
   `extra="forbid"`.
3. `python -m kdive install-fixtures` (via `LOCAL_LIBVIRT_FIXTURES`) writes a profile YAML with no
   `requires:` block, and that written YAML re-parses through `load_fixture_catalog`.
4. `fixtures/local-libvirt/configs/console-ready.required.config` no longer exists.
5. `InventorySourceKind` has exactly one member (`RESOURCE`); `BUILD_HOST_RESOURCE_KIND` and
   `LockScope.BUILD_HOST` are gone; no `src/` reference names any of them.
6. `inventory.clear_override` takes `(resource_kind, name)` — no `source_kind` — and still: clears a
   `removed` resource override (success), returns `not_found` when none exists (idempotent), and
   returns `configuration_error` on an invalid `resource_kind`. Its wrapper docstring and `Field`
   text match the new signature, and no `source_kind` key appears in the success `data` payload, the
   denial-audit scope/args, or the `_audit_clear` scope/args. No `source_kind` / `build_host` /
   `build-host` token remains anywhere in `src/kdive/mcp/tools/ops/inventory.py` — including
   docstrings and `Args` blocks — falsifiable via
   `rg 'source_kind|build_host|build-host' src/kdive/mcp/tools/ops/inventory.py` returning zero hits.
7. The `db/locks.py` `LockScope` docstring no longer claims a `BUILD_HOST` scope exists or that it is
   the `inventory.clear_override` lock.
8. `just lint`, `just type` (whole tree), and `just test` all pass.

## Verification

- Grep guard (exact removed symbols — must return **zero** hits over `src/`):
  `rg 'ConfigRequirements|CmdlineRequirements|ProfileRequirements|RootfsRequirements|BUILD_HOST_RESOURCE_KIND|LockScope\.BUILD_HOST|InventorySourceKind\.BUILD_HOST|\.required\.config' src/`.
  A checkable equality, not a judgement call: every one of these names is being deleted, so any
  post-removal hit is a missed removal.
  - The generic word `build_host` / `build-host` deliberately is **not** in the guard: it has many
    live residuals that stay — `jobs.payloads.build_host_id`, the image-family `build-host`
    toolchain comments, `diagnostics/egress_probe.py`, and every historical
    `db/schema/*.sql` migration (`0027_build_hosts.sql` … `0062_drop_server_build_tables.sql`).
    Those are expected and out of scope.
- Targeted tests: `tests/provider_components/test_catalog.py`, `tests/admin/test_default_fixtures.py`,
  `tests/mcp/ops/test_inventory_clear_override.py`, `tests/inventory/test_overrides.py`,
  `tests/db/test_locks.py`.
- Full guardrail: `just lint && just type && just test`.

## Operational note — previously-installed fixtures

`load_fixture_catalog` validates each on-disk profile YAML under the new `extra="forbid"`
`ProfileCatalogEntry`. The source-tree default (`DEFAULT_FIXTURE_CATALOG_PATH`) is updated in this
change, so the default path is consistent. But an operator who set `KDIVE_FIXTURE_CATALOG_PATH` to a
directory populated **before** this change has a profile YAML that still carries the `requires:`
block; after upgrade its parse raises `ValidationError` → `CategorizedError(INFRASTRUCTURE_FAILURE)`
at catalog load. Two cases, with distinct remediation (documented, not automated — pre-release, no
external consumers):

- **Populated by `install-fixtures`** (the common case): the directory holds only `manifest.yaml`
  and `profiles/console-ready_x86_64.yaml` — `install_fixtures` writes exactly the keys of
  `LOCAL_LIBVIRT_FIXTURES` and never emits anything under `configs/`. The only stale artifact is the
  profile YAML's `requires:` block, so re-running `install-fixtures --force` rewrites it
  requires-free and fully resolves the failure. There is no `.required.config` in such a directory.
- **A hand-copied full source tree** (`fixtures/local-libvirt/` copied verbatim, `configs/`
  included): besides the profile YAML, this also carries the now-orphaned
  `configs/console-ready.required.config` (deleted from the source tree by AC4). Re-sync the tree
  or delete that file manually; nothing reads it either way.

Auto-pruning orphaned files from `install_fixtures --force` is out of scope for this cleanup.

## Rollback

Pure deletion on a feature branch; revert the branch. No migration, no data change, nothing to
un-apply. (An operator who already re-installed fixtures on the new format keeps a valid catalog;
the reverted code still parses a profile with no `requires:` block.)
