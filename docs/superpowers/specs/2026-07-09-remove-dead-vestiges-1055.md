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
  `inventory_clear_override` wrapper (and its `Field`), the `clear_override` handler, and the
  denial-audit args; drop the `BUILD_HOST` branches in `_parse_override_identity` (now takes only
  `resource_kind`, `name`; validates `resource_kind` as a `ResourceKind`; builds an
  `OverrideIdentity` with `source_kind=InventorySourceKind.RESOURCE`) and `_override_identity_lock`
  (always `resource_identity_lock`). Update the wrapper docstring to `clear_override(resource_kind,
  name)`.
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
   text match the new signature.
7. The `db/locks.py` `LockScope` docstring no longer claims a `BUILD_HOST` scope exists or that it is
   the `inventory.clear_override` lock.
8. `just lint`, `just type` (whole tree), and `just test` all pass.

## Verification

- Grep guard: `rg 'BUILD_HOST|build_host|ConfigRequirements|CmdlineRequirements|ProfileRequirements|RootfsRequirements|\.required\.config'`
  over `src/` returns only unrelated hits (e.g. `jobs.payloads.build_host_id`, the live
  `kernel_config` package, `build-host` toolchain image comments) — none of the removed symbols.
- Targeted tests: `tests/provider_components/test_catalog.py`, `tests/admin/test_default_fixtures.py`,
  `tests/mcp/ops/test_inventory_clear_override.py`, `tests/inventory/test_overrides.py`,
  `tests/db/test_locks.py`.
- Full guardrail: `just lint && just type && just test`.

## Rollback

Pure deletion on a feature branch; revert the branch. No migration, no data change, nothing to
un-apply.
