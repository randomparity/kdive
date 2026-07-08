# Remove the Server-Build Lane — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete all server/worker kernel-build code and all kernel-config validation, so
the external-upload lane (agent builds locally → uploads to S3 → kdive installs) is the only
lane.

**Architecture:** A staged deletion. `ty`, `ruff`, and pytest collection all run **whole-tree**,
so every commit must leave the tree importable and green. Tasks are therefore ordered so each
one removes a coherent slice — deleting a module only after (or in the same commit as) removing
every surviving reference to it. Tasks are larger than the usual "2–5 min" granularity because
the deletion is coupled; each still ends at an independently reviewable green commit.

**Tech Stack:** Python 3.14, `uv`, `just` recipes (`just lint`, `just type`, `just test`,
`just docs`, `just docs-check`), `ty` (strict, whole-tree), ruff, prek hooks.

**Design source:** `docs/superpowers/specs/2026-07-08-remove-server-build-lane-design.md`
(read it before starting — it holds the deletion inventory, the boundary-file surgery list,
and the preserved feature→symbol table).

## Global Constraints

- **Whole-tree green at every commit.** After each task run the full guardrail suite
  (`just lint && just type && just test && just docs-check`); do not commit red. `ty` fails the
  whole tree on a single dangling import, so a task that deletes a module must, in the same
  commit, remove every surviving importer of its symbols.
- **Stage explicit paths only** (`git add <path>` / `git rm <path>`); never `git add -A`.
- **Conventional Commits**, imperative subject ≤72 chars, one logical slice per commit, ending
  with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Regenerate generated docs** with `just docs` in any task that removes an MCP tool or changes
  a tool docstring/Field, and commit the regenerated `docs/guide/reference/*.md`.
- **No new server-build behavior.** This is pure removal; do not add features.
- **Never validate a `.config`.** Any code that inspects kernel config symbols is deleted, not
  relocated.
- **Sweep step is mandatory.** Every deletion task ends with
  `rg -n '<deleted-symbol-or-module>' src tests` returning nothing before committing.

## File Structure / cut map

The deletion is cut into six tasks. Known hard couplings (verified against the code) and where
they are resolved:

- `admission.py` — branches on `ServerBuildProfile`, imports `build_host_selection` +
  `db.build_hosts`. → externalized in Task 2, profile-flattened in Task 3.
- `components/validation.py` — imports `build_host_selection`. → Task 2.
- `reconciler/loop.py` — imports build-host fleet/repairs/prober. → Task 2.
- `jobs/handlers/runs/registrar.py`, `ports.py`, `mcp/worker_registration.py` — build-execution
  wiring. → Task 2.
- `mcp/tools/lifecycle/runs/registrar.py` `_RunsCreatePayload` union `ExternalBuildProfile |
  ServerBuildProfile`. → collapsed in Task 3.
- `services/runs/complete_build.py`, `mcp/tools/catalog/artifacts/uploads.py`,
  `build_artifacts/validation.py`, `components/requirements.py` — config-validation / profile
  plumbing. `ConfigRequirements`/`CmdlineRequirements` **data classes survive** (inert
  `FixtureManifest` fields); only the validator functions die. → Task 3.
- `mcp/tool_registration.py` (real hub) registers `build_configs` (→ Task 1) and `ops.build_hosts`
  (→ Task 2); `mcp/exposure.py` holds the RBAC keys for both (Task 1 + Task 2, regen RBAC matrix).
- `providers/core/runtime.py` `builder: Builder` required field + every `*/composition.py` builder
  construction. → structural surgery in Task 2.
- `diagnostics/kernel_src.py`, `providers/remote_libvirt/diagnostics/contribution.py`,
  `inventory/reconcile/{pipeline,build_hosts,overrides}.py`, `__main__.py` `_build_reconcile_config`
  — surviving importers of deleted build-host/db/telemetry symbols. → Task 2.
- `build_configs/**` importers: `admin/build_configs.py`, `inventory/reconcile/build_configs.py`
  (imported by `inventory/reconcile/pipeline.py`), `inventory/model.py`, `inventory/cli.py`. → Task 4.

---

### Task 1: Remove the MCP server-build tool surface

Deletes the agent-facing server-build tools. Self-contained: these modules define MCP handlers
that nothing else imports except the MCP registrar/tool-index. Leaves `runs.create` and its
`ServerBuildProfile` union intact (collapsed later).

**Files:**
- Delete: `src/kdive/mcp/tools/lifecycle/runs/server_build.py`, `.../composite.py` (the MCP
  tool), `.../validate_profile.py`, `.../profile_examples.py`,
  `src/kdive/mcp/tools/catalog/build_configs.py` (the `buildconfig.get/set/list` tools).
- Delete: `src/kdive/mcp/resources/_content/build-source-staging.md`.
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — drop the `_register_runs_build`,
  `_register_runs_build_install_boot`, `runs.validate_profile`, `runs.profile_examples`
  registrations and their imports; prune every docstring / `suggested_next_actions` that names
  `runs.build`, `runs.validate_profile`, `runs.profile_examples`, or `buildconfig.*`. Keep
  `runs.create`, `runs.complete_build`, `runs.bind/cancel/install/boot`.
- Modify: `src/kdive/mcp/tool_registration.py` (the real registration hub — there is no
  `catalog/registrar.py`) — remove the `build_configs` import (line ~23) and its
  `build_configs.register(...)` call (line ~157).
- Modify: `src/kdive/mcp/tool_index.py` — remove keyword entries for the deleted tools.
- Modify: `src/kdive/mcp/exposure.py` — drop the RBAC/visibility keys `buildconfig.set/delete/get/list`,
  `runs.profile_examples`, `runs.validate_profile` (keep `build_hosts.*` for Task 2). The generated
  RBAC tool matrix / reference index is derived from this; regen and reconcile
  `tests/scripts/test_gen_rbac_tool_matrix.py`.
- Delete tests: `tests/mcp/catalog/test_build_configs_tool.py`,
  `tests/mcp/lifecycle/test_runs_build*.py`, `tests/mcp/lifecycle/test_validate_profile*.py`,
  `tests/mcp/lifecycle/test_profile_examples*.py`,
  `tests/mcp/tools/lifecycle/runs/test_composite_tool.py` (and any other test that imports the
  deleted tool modules — find with `rg -l 'server_build|validate_profile|profile_examples|build_configs' tests/mcp`).

**Interfaces:**
- Consumes: nothing from later tasks.
- Produces: an MCP surface with no server-build tools; `runs.create` still accepts the
  `ExternalBuildProfile | ServerBuildProfile` union (Task 3 collapses it).

- [ ] **Step 1: Delete the tool modules and resource doc** (`git rm` the files above).
- [ ] **Step 2: Prune the registrars and tool_index.** Remove registrations, imports, and
  cross-referencing help text. Grep the docstrings you keep:
  `rg -n 'runs\.build|validate_profile|profile_examples|buildconfig' src/kdive/mcp` → only
  incidental mentions in kept tools' prose should remain; remove those too.
- [ ] **Step 3: Delete the orphaned tests.**
- [ ] **Step 4: Regenerate docs.** `just docs` — the MCP tool reference loses the removed tools;
  stage the regenerated `docs/guide/reference/*.md`.
- [ ] **Step 5: Guardrails.** `just lint && just type && just test && just docs-check`. Confirm
  `rg -n 'server_build|validate_profile\.py|profile_examples\.py|catalog/build_configs' src` is
  empty.
- [ ] **Step 6: Commit** — `feat(build)!: remove server-build MCP tools (runs.build, validate_profile, profile_examples, buildconfig)`.

---

### Task 2: Delete kernel-build execution + build-host fleet

The largest task. Removes everything that compiles kernels or manages build hosts, and
externalizes the create/admission path. After this, `runs.create` accepts a `ServerBuildProfile`
but nothing special-cases it (a transient dead-but-green state fixed in Task 3).

**Files — delete (build execution):**
- `src/kdive/jobs/handlers/runs/build.py`, `src/kdive/jobs/handlers/runs/composite.py`.
- `src/kdive/providers/shared/build_host/**` (entire package).
- `src/kdive/providers/local_libvirt/build.py`, `src/kdive/providers/remote_libvirt/build.py`.
- Remote-libvirt ephemeral build-VM: `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py`,
  `.../reaping/build_vm.py`, `.../guest/build_transport.py`, `.../diagnostics/buildhost_agent.py`.
- `src/kdive/providers/ports/build.py`, `src/kdive/providers/ports/build_transport.py`.
- `src/kdive/providers/fault_inject/build.py`, `src/kdive/providers/assembly/build_hosts.py`.
  **KEEP** `providers/shared/build_timeouts.py` (and its test) — despite the name it holds only a
  generic `SLOW_BUILD_TOOL_TIMEOUT_S` constant that the **surviving rootfs-image lane** imports
  (`local_libvirt/rootfs_build.py:70`, `remote_libvirt/rootfs_build.py:47`); it is not
  server-build-only.
- `src/kdive/observability/build_telemetry.py`, `src/kdive/domain/build_phase.py`.

**Files — delete (build-host fleet):**
- `src/kdive/reconciler/build_host_fleet.py`, `src/kdive/reconciler/repairs/build_hosts.py`.
- `src/kdive/services/runs/build_host_selection.py`.
- `src/kdive/diagnostics/buildhost_agent_check.py` (and siblings `buildhost_agent*.py`).
- `src/kdive/db/build_hosts.py`, `src/kdive/db/build_host_policy.py`,
  `src/kdive/db/buildhost_agent_probes.py`.
- `src/kdive/mcp/tools/ops/build_hosts/**`.
- `src/kdive/inventory/reconcile/build_hosts.py` (imports `db.build_hosts.BuildHostKind`; called
  by `inventory/reconcile/pipeline.py`).
- `src/kdive/diagnostics/kernel_src.py` **+ `src/kdive/diagnostics/local_kernel_src_check.py`** —
  warm-tree kernel-**source** diagnostics (server-build only: `kernel_src.py` imports
  `db.build_host_policy` + `db.build_hosts.get_by_id(WORKER_LOCAL_ID)`). Delete both. Their
  assembly site `diagnostics/service.py` and the `ops.diagnostics` opt-in are surgery (modify
  list); sweep `rg -n 'kernel_src|local_kernel_src|LocalKernelSrcCheck' src` to catch every site.

**Files — modify (surgery):**
- `src/kdive/jobs/handlers/runs/registrar.py` — drop the `JobKind.BUILD` /
  `JobKind.BUILD_INSTALL_BOOT` registrations and the `build_handler` / `composite_handler` /
  `BuildProfile` / `ServerBuildProfile` imports; keep install/boot/other registrations.
- `src/kdive/jobs/handlers/runs/ports.py` — remove `BuildHostTransportFactories` (from deleted
  `jobs.handlers.runs.build`) and `BuildPhaseRecorder` (from deleted `observability/build_telemetry`).
- `src/kdive/mcp/worker_registration.py` — remove the `BuildHostTransportFactories` (from deleted
  `providers.shared.build_host.dispatch`) and `BuildPhaseRecorder` wiring.
- `src/kdive/reconciler/loop.py` — strip: imports of `BuildHostProber`, `BuildHostTelemetry` /
  `read_build_host_snapshot`, `reconciler.repairs.build_hosts`; the
  `_reclaim_build_host_leases` / `_reap_orphan_build_vms` / `_probe_build_host_reachability`
  aliases; the `_refresh_build_host_snapshot` method; the `reclaimed_build_host_leases` /
  `build_host_states_changed` counters and their `__all__`/report entries.
- `src/kdive/services/runs/admission.py` — remove `_maybe_reject_incompatible_source` (the
  `isinstance(build_profile, ServerBuildProfile)` build-host compat check), the
  `from kdive.db.build_hosts import get_by_name`, the
  `from kdive.services.runs.build_host_selection import check_source_kind_compatibility`, and
  `is_git_source` imports/uses. Leave `is_external = isinstance(build_profile, ExternalBuildProfile)`
  for now (Task 3 simplifies it).
- `src/kdive/components/validation.py` — remove its `build_host_selection` import/use (find with
  `rg -n build_host_selection src/kdive/components/validation.py`; drop the dead check).
- **Provider runtime + composition (structural).** `src/kdive/providers/core/runtime.py` declares
  `builder: Builder` as a **required** field (import at line ~21, field at ~71); each provider's
  `composition.py` constructs a concrete builder (`local_libvirt/composition.py` →
  `LocalLibvirtBuild.from_env`, `fault_inject/composition.py` → `FaultInjectBuild(...)`,
  `remote_libvirt/composition.py` → `RemoteLibvirtBuild` + `BuildTransport` +
  `RemoteLibvirtBuildVmReaper`). Drop the `builder` field + import from `ProviderRuntime` and
  remove each provider's builder construction (and the remote build-VM reaper wiring). Sweep with
  `rg -n 'Builder|LocalLibvirtBuild|RemoteLibvirtBuild|FaultInjectBuild|ports\.build|assembly\.build_hosts|Build.*Reaper' src/kdive/providers`.
- `src/kdive/inventory/reconcile/pipeline.py` — remove the `reconcile_build_hosts` import (line ~18)
  and its `await reconcile_build_hosts(...)` call (~42). (The `reconcile_build_configs` call is
  removed in Task 4.)
- `src/kdive/inventory/reconcile/overrides.py` — remove the `db.build_hosts.BuildHostKind` import
  (~31) and the `EPHEMERAL_LIBVIRT` override branch (~213-217).
- `src/kdive/__main__.py` — in `_build_reconcile_config` (~614) drop the
  `reconciler.build_host_fleet.BuildHostTelemetry` import and its `ReconcileConfig` wiring, in
  lockstep with the `loop.py` counter/field removals.
- `src/kdive/providers/remote_libvirt/diagnostics/contribution.py` — remove the
  `diagnostics.buildhost_agent_check` import (~13), the sibling `buildhost_agent` probe use, the
  `_buildhost_agent_check` factory, and its registration in the diagnostics contribution (~100-111).
- `src/kdive/mcp/tool_registration.py` — drop the `ops.build_hosts` import (~50),
  `_register_ops_build_hosts_tools` (~203-206), and its entry in the registration list (~282).
- `src/kdive/mcp/exposure.py` — drop the `build_hosts.*` RBAC keys (regen the RBAC matrix).
- **`src/kdive/providers/assembly/composition.py` (structural — the transport-factory/prober hub).**
  Drop the `db.build_hosts.BuildHostKind` import (~19), the
  `providers.shared.build_host.dispatch`/`.reachability` imports (~44-45), and the three build-host
  methods `_build_host_transport_factory_maps`, `build_build_host_transport_factories` (~412), and
  `build_reconciler_build_host_prober` (~423). Then its two callers, in lockstep:
  `src/kdive/mcp/app.py:88` (drop the `transport_factories=composition.build_build_host_transport_factories()`
  arg to worker registration) and `src/kdive/__main__.py:633` (drop
  `build_host_prober=provider_composition.build_reconciler_build_host_prober()` from
  `ReconcileConfig` — matches the `:614` `BuildHostTelemetry` removal above).
- `src/kdive/mcp/tools/ops/diagnostics.py` — remove the `with_buildhost_agent` param and all its
  plumbing (`_audit_args`, `_audit_run`, service-factory threading) and the `_BUILDHOST_TOOL`
  constant, plus the `local_kernel_src` opt-in tied to the deleted `diagnostics/kernel_src.py`.
  Regenerate the tool reference (`just docs`).
- `src/kdive/diagnostics/service.py` — the diagnostics assembly site: drop the
  `import kdive.diagnostics.kernel_src` (~26) and the always-on `local_kernel_src` check assembled
  in `_build_host_checks()` (~337-341: `warm_tree_source_probe()`, `local_host_enabled_probe`),
  and the `buildhost_agent` check factory. These checks are removed, not relocated.

**Files — delete tests:** all of `tests/providers/build_host/**`,
`tests/providers/local_libvirt/test_build.py`, `tests/providers/remote_libvirt/build/**` +
`.../lifecycle/test_build_vm.py` + `.../guest/test_build_transport.py`,
`tests/providers/ports/test_build.py`, `tests/providers/test_build_common.py`,
`tests/providers/test_build_host_assembly.py`, `tests/jobs/handlers/test_build_handler*.py`,
`tests/jobs/handlers/test_runs_build.py`, `tests/reconciler/test_build_host_fleet.py`,
`tests/reconciler/test_build_hosts.py`, `tests/services/test_build_host_selection.py`,
`tests/db/test_build_hosts_*.py`, `tests/db/test_buildhost_agent_probes.py`,
`tests/diagnostics/test_buildhost_agent*.py`, `tests/mcp/ops/build_hosts/**`,
`tests/mcp/ops/test_build_hosts.py`, `tests/observability/test_build_telemetry.py`,
`tests/domain/test_build_phase.py`, `tests/guards/test_build_host_boundaries.py`,
`tests/adversarial/test_build_config_concurrency.py`, `tests/diagnostics/test_local_kernel_src.py`.
Also **adjust** (remove only the deleted-check cases, keep the rest): `tests/diagnostics/test_service.py`,
`tests/diagnostics/test_default_factory.py`, `tests/mcp/ops/test_diagnostics.py`. Do **not**
delete `tests/providers/shared/test_build_timeouts.py` (module kept).

**Interfaces:**
- Consumes: Task 1's reduced MCP surface.
- Produces: no build execution, no fleet; `create`/`admission` handle only external at runtime
  but `ServerBuildProfile` still exists in `profiles/build.py` and the MCP `runs.create` union.

- [ ] **Step 1: Surgery first.** Edit the six modify-files above to drop every reference to the
  soon-deleted symbols. This must precede deletion so intermediate greps are meaningful.
- [ ] **Step 2: Delete the execution + fleet modules and their tests** (`git rm`).
- [ ] **Step 3: Sweep.** `rg -n 'build_host|BuildHostKind|BuildPhaseRecorder|build_telemetry|BuildHostProber|BuildHostTelemetry|build_host_selection|kernel_src|reconcile_build_hosts|jobs.handlers.runs.build|handlers.runs.composite|providers.ports.build|assembly.build_hosts|LocalLibvirtBuild|RemoteLibvirtBuild|FaultInjectBuild|\.builder\b|buildhost_agent' src` must return only surviving *rootfs*-image build references (e.g. `rootfs_build.py`) and the inert `JobKind.BUILD*` enum members — nothing importing a deleted module or reading the removed `ProviderRuntime.builder`.
- [ ] **Step 4: Guardrails.** `just lint && just type && just test && just docs-check`. Expect
  the external-lane, install, boot, reconciler (non-fleet), and rootfs-image-build tests to stay
  green.
- [ ] **Step 5: Commit** — `feat(build)!: delete kernel-build execution and build-host fleet`.

---

### Task 3: Flatten BuildProfile to external-only; remove config validation

Collapses the profile model and removes every remaining reader of `ServerBuildProfile`,
`profile_requirements`, and kernel-config validation.

**Files — modify:**
- `src/kdive/profiles/build.py` — delete `ServerBuildProfile`, `GitKernelSource`/`GitSourceRef`/
  `is_git_source`/`git_source_of`, `ProfileRequirementsRef`, `MAX_CONFIG_FRAGMENTS`, the URI-scheme
  guards, and the `source` discriminator. Rename/keep a single flat `BuildProfile` (drop the
  `profile_requirements` field). `BuildProfile.parse` no longer dispatches on `source`. Keep
  `dump_build_profile`. This is the thin type Spec 2 will extend.
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — `_RunsCreatePayload.build_profile` typed as
  the flat `BuildProfile` (drop the union); prune the huge server-build docstring on the Field.
- `src/kdive/services/runs/admission.py` — remove the now-vacuous `ExternalBuildProfile`
  isinstance; every run is external. Simplify/remove the `is_external` flag as follows: keep it
  `True` (or drop the branch it guards) so the create response still chains into the upload loop.
- `src/kdive/mcp/tools/catalog/artifacts/uploads.py` — `_run_accepts_upload` gates on
  `isinstance(parsed, ExternalBuildProfile)`; change to the flat-`BuildProfile` check.
- `src/kdive/services/runs/complete_build.py` — delete `_external_config_requirements`, the
  `_external_build_profile` isinstance guard, and the `ConfigRequirements` / `load_fixture_catalog` /
  `profile.profile_requirements` plumbing through `CompleteBuildValidation`; call
  `validate_external_artifacts` with no `profile_requirements`.
- `src/kdive/build_artifacts/validation.py` — flip `effective_config` contract `requirement` from
  `'conditional'` to `'optional'`; rewrite its note; delete `_validate_effective_config`; drop the
  `profile_requirements` parameter from `validate_external_artifacts`; remove the
  `ConfigRequirements` / `validate_config_requirements` import.
- `src/kdive/components/requirements.py` — delete the **validator functions and helpers**:
  `validate_config_requirements`, `validate_cmdline_requirements` (dead code),
  `load_profile_config_requirements`, and `_parse_config` (if unused after those go). **Keep the
  `ConfigRequirements` and `CmdlineRequirements` data classes** — `FixtureManifest`
  (`components/catalog.py:42-43`) declares `config: ConfigRequirements` and `cmdline:
  CmdlineRequirements` fields, and a fixture (`fixtures/local-libvirt/profiles/console-ready_x86_64.yaml`)
  populates `config:`, so deleting the classes would dangle `catalog.py` and break fixture parse.
  The classes become inert data shapes (no code reads them for gating). **Do not touch**
  `services/runs/steps.py` `platform_owned_cmdline_token` — that is the real install/boot cmdline
  gate and stays. `components/validation.py` was already stripped of its `build_host_selection`
  use in Task 2.

**Files — modify tests:**
- `tests/profiles/test_build.py`, `tests/profiles/test_build_profile_source.py` — drop
  server/source cases; keep the flat-profile parse cases.
- Delete `tests/provider_components/test_requirements.py` (the sole caller of
  `validate_cmdline_requirements`) plus any `test_*` for the deleted validators.

**Files — create tests:**
- `tests/mcp/lifecycle/test_effective_config_unvalidated.py` — assert `create_run_upload` /
  `complete_build` accept an `effective_config` that the old gate would have rejected (e.g. a
  config missing a mount symbol) and the run completes. Proves "accepted but never inspected".

**Interfaces:**
- Consumes: Task 2's externalized create path.
- Produces: a flat `BuildProfile`, no config validation anywhere, `effective_config` optional +
  unvalidated. `ServerBuildProfile` and `ConfigRequirements` no longer exist.

- [ ] **Step 1: Write the failing test** `test_effective_config_unvalidated.py` (upload a
  deliberately "bad" config, expect success). Run it — it should FAIL today (old validation
  rejects) or error on import; that failure defines the work.
- [ ] **Step 2: Flatten `profiles/build.py`** and update the MCP `_RunsCreatePayload` union.
- [ ] **Step 3: Externalize `admission.py`, `uploads.py`, `complete_build.py`, `validation.py`**
  per the modify list; remove the config-validation plumbing.
- [ ] **Step 4: Prune `components/requirements.py`** to only what `FixtureManifest` references;
  delete its dead-code test.
- [ ] **Step 5: Sweep.**
  `rg -n 'ServerBuildProfile|profile_requirements|validate_config_requirements|validate_cmdline_requirements|load_profile_config_requirements|is_git_source|MAX_CONFIG_FRAGMENTS' src tests`
  must be empty. Note `ConfigRequirements` / `CmdlineRequirements` **intentionally survive** as
  inert `FixtureManifest` data shapes — do not sweep for them.
- [ ] **Step 6: Run the new test — it PASSES.** Then guardrails
  `just lint && just type && just test && just docs && just docs-check`.
- [ ] **Step 7: Commit** — `feat(build)!: flatten BuildProfile and drop all kernel-config validation`.

---

### Task 4: Delete the build-config catalog system

**Files — delete:** `src/kdive/build_configs/**` (`catalog.py`, `defaults.py`, `platform_config.py`,
`rules.py`, `seed.py`, `data/kdump.config`, `__init__.py`); `src/kdive/admin/build_configs.py`;
`src/kdive/inventory/reconcile/build_configs.py`; the `seed-build-configs` CLI subcommand
(`src/kdive/cli/__main__.py` or `src/kdive/__main__.py` registration + its handler).

**Files — modify (sweep remaining importers):**
- `src/kdive/inventory/reconcile/pipeline.py` — remove the `reconcile_build_configs` import
  (line ~17) and its `await reconcile_build_configs(conn, doc, store)` call (~43). **This is the
  actual importer** — the `reconcile_build_hosts` sibling was already removed here in Task 2.
- `src/kdive/inventory/model.py`, `src/kdive/inventory/cli.py`,
  `src/kdive/inventory/reconcile/__init__.py` — remove any build-config reconcile re-export /
  registration and any `build_configs` import (sweep `rg -n build_configs src/kdive/inventory`).
- Confirm `src/kdive/components/catalog.py` does **not** import `build_configs` (its
  `load_fixture_catalog` is local); adjust only if a real import exists.

**Files — delete tests:** `tests/build_configs/**`, `tests/admin/test_build_configs*.py`,
`tests/inventory/**test_build_config*`, `tests/cli/test_seed_build_configs*.py` (find with
`rg -l 'build_configs' tests`).

**Interfaces:**
- Consumes: Tasks 2 & 3 removed the last non-inventory importers.
- Produces: no build-config catalog, no `platform_config`, no kdump fragment.

- [ ] **Step 1: Sweep importers.** `rg -n 'build_configs|DEFAULT_CONFIG_REF|platform_required|kdump\.config' src` → resolve every hit to a delete or a surgery in the modify list.
- [ ] **Step 2: Delete the package, admin/inventory reconcile, and the CLI subcommand + tests.**
- [ ] **Step 3: Sweep.** `rg -n 'build_configs' src tests` empty.
- [ ] **Step 4: Guardrails** `just lint && just type && just test && just docs-check`.
- [ ] **Step 5: Commit** — `feat(build)!: delete the build-config catalog and kdump fragment`.

---

### Task 5: Drop the orphaned server-build tables

**Files — create:** the next-numbered migration under `src/kdive/db/schema/` (check the highest
existing number; use `NNNN_drop_server_build_tables.sql`).

```sql
-- Drop dependents BEFORE build_hosts (FKs REFERENCE build_hosts(id)).
DROP TABLE IF EXISTS build_config_catalog;
DROP TABLE IF EXISTS buildhost_agent_probe_guests;
DROP TABLE IF EXISTS build_host_leases;
DROP TABLE IF EXISTS build_hosts;
-- JobKind enum values BUILD, BUILD_INSTALL_BOOT are intentionally left in place:
-- Postgres cannot drop a value from an existing enum without recreating the type.
```

Do **not** drop `egress_probe_guests` (0022) — separate, surviving table.

**Interfaces:**
- Consumes: Tasks 2 & 4 deleted all code touching these tables.
- Produces: a clean schema with no server-build tables.

- [ ] **Step 1: Verify no code references the tables.**
  `rg -n 'build_config_catalog|buildhost_agent_probe_guests|build_host_leases|\bbuild_hosts\b' src` →
  empty (only the new migration file names them).
- [ ] **Step 2: Write the migration** (highest-number + 1).
- [ ] **Step 3: Apply + verify** against the live/test DB the repo uses for migration tests
  (`just test tests/db` or the migration harness); confirm it applies cleanly and the drop order
  does not trip an FK.
- [ ] **Step 4: Guardrails** `just lint && just type && just test`.
- [ ] **Step 5: Commit** — `feat(db)!: drop orphaned server-build tables`.

---

### Task 6: Residual sweep — docs, resources, and full-suite green

**Files — modify/delete:**
- `src/kdive/mcp/resources/_content/external-build-upload.md` — remove any reference to
  server-build / `runs.build` / config validation; keep it as the upload-lane guide (Spec 3 will
  extend it). Delete any other build resource docs the earlier tasks missed.
- Regenerate all generated docs: `just docs` (MCP reference, agent guides). Stage the diffs.
- `AGENTS.md` / `docs/**` narrative that describes the server-build lane — update to
  upload-only. Find with `rg -ln 'runs\.build|build_host|server.build|buildconfig' docs AGENTS.md`.
- Any straggler test that still imports a deleted symbol (final `rg` sweep across `tests`).

**Interfaces:**
- Consumes: all prior tasks.
- Produces: docs consistent with the upload-only platform; full suite green.

- [ ] **Step 1: Update narrative docs + resource content** to the upload-only model.
- [ ] **Step 2: Regenerate generated docs** `just docs`; review the diff.
- [ ] **Step 3: Full sweep.**
  `rg -n 'ServerBuildProfile|build_host|buildconfig|runs\.build\b|profile_examples|validate_profile|build_configs|BuildPhaseRecorder|platform_required' src tests docs`
  → only intentional survivors (rootfs-image build, the inert enum members, this plan/spec).
- [ ] **Step 4: Full guardrails** `just lint && just type && just test && just docs-check`.
- [ ] **Step 5: Commit** — `docs(build): update narrative + generated docs for upload-only lane`.

---

## Self-Review

- **Spec coverage:** every "deleted"/"surgically changed"/schema item in the spec maps to a task
  (Task 1 = MCP surface; Task 2 = execution+fleet+wiring; Task 3 = profile flatten + validation
  removal; Task 4 = build_configs; Task 5 = migration; Task 6 = docs/resources). The preserved
  feature→symbol table stays in the spec (not re-created in code) — satisfied by *not* having a
  task that recreates it.
- **Ordering:** each task deletes a module only after its importers are surgically cleared in the
  same commit; Task 3 depends on Task 2 having externalized `admission`; Task 4 depends on 2+3
  clearing the last non-inventory importers; Task 5 depends on all code deletion; Task 6 last.
- **Type consistency:** the flat `BuildProfile` name is introduced in Task 3 and consumed by the
  MCP union edit in the same task; no later task references `ServerBuildProfile`.
- **Green-at-each-commit** is the explicit exit gate of every task via the guardrail step + `rg`
  sweep.
