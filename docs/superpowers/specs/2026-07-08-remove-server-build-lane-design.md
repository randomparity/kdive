# Remove the server-build lane — design (Spec 1 of 3)

- **Status:** Draft (approved in brainstorming 2026-07-08)
- **Date:** 2026-07-08
- **Scope:** Spec 1 of a three-spec redesign of kernel build & config handling.
  Specs 2 and 3 are out of scope here (see [Relationship to the other specs](#relationship-to-the-other-specs)).

## Context

kdive currently offers two ways to get a kernel onto a VM:

1. **Server-build lane** — the worker checks out a kernel tree, merges a `.config`
   fragment (`make defconfig` → `merge_config.sh -m` → `make olddefconfig`),
   validates the result against hard-coded requirements, compiles the kernel on a
   build host (worker-local or ephemeral build VM), and publishes the artifacts.
2. **External-upload lane** — the agent builds the kernel *locally*, uploads the
   artifacts to S3, and kdive installs them.

The server-build lane has grown into a large, tangled subsystem: a whole
`providers/shared/build_host/**` package, per-provider builders, an ephemeral
build-VM lifecycle, a build-host fleet with leases and agent probes, a build-config
catalog with fragment composition and multi-gate `.config` validation, and the MCP
surface to drive all of it. It is hard for a human *and* an agent to navigate, and
its config-validation gates actively fight the agent (a composed config that drops a
symbol is rejected mid-build).

The external-upload lane already exists, is self-sufficient, and is even the
documented default. Install and boot are **source-agnostic** — they consume
`run.kernel_ref` / `BuildStepResult` regardless of who produced the artifacts.

## Requirements addressed

From the redesign requirements, Spec 1 delivers:

> **R1.** The worker no longer has *any* responsibility for building kernel
> images/modules. Building is entirely the agent's job, done locally, with artifacts
> uploaded to S3 for install on the VM. All worker build code is deleted.

and the cross-cutting rule that also lands here:

> **No validation.** kdive will not, in any way, validate the kernel config settings
> the agent uses.

Image metadata + config offer (R2) and debug-feature advertisement + gating (R3) are
**Specs 2 and 3**.

## Goal

Delete all server/worker kernel-build code and all kernel-config validation. The
external-upload lane becomes the **only** lane. kdive never compiles a kernel and
never inspects or validates a `.config`.

**Ships:** `runs.create` → `artifacts.expected_uploads` → `artifacts.create_run_upload`
→ `runs.complete_build` → `runs.install` → `runs.boot`, working end-to-end, on a much
smaller surface.

## Decisions (settled in brainstorming)

1. **Flat `BuildProfile`, no discriminator.** `ServerBuildProfile` and the
   `source` field are deleted. There is one build-profile type. Re-adding
   server-build later reintroduces the field — accepted tradeoff for a clean surface
   now.
2. **No `profile_requirements`.** Its only job is validating the uploaded config,
   which the no-validation rule forbids. The field and its plumbing are removed.
   `BuildProfile` becomes a thin (near-empty) type, **kept** as the home Spec 2 will
   extend (image selection).
3. **`effective_config` stays an accepted upload, but is no longer validated.**
   Spec 3 will *read* it (advisory) to gate features; Spec 1 stops *checking* it.
4. **Full delete of `build_configs/**`** (catalog, fragments, `platform_config`,
   `buildconfig.*` tools). The feature→symbol knowledge is preserved in this document
   (see [Preserved knowledge](#preserved-knowledge-for-spec-3)) so no orphaned data
   files remain in the tree.
5. **Delete `runs.validate_profile` and `runs.profile_examples`.** They exist to help
   choose/validate a *server*-build profile; the external lane is guided by
   `artifacts.expected_uploads`.
6. **Delete the `runs.build_install_boot` composite.** Granular
   `runs.complete_build` → `runs.install` → `runs.boot` only. A composite can be
   re-added later if it is actually wanted.
7. **Drop migration for orphaned schema.** A new forward migration drops the dead
   server-build tables. Postgres cannot remove a value from an existing enum without
   recreating the type, so the `JobKind` values `BUILD` / `BUILD_INSTALL_BOOT` are
   left **inert with a comment** rather than recreating the enum.

## What is deleted (subtraction)

Grouped by subsystem. Paths are representative anchors, not exhaustive — the plan
enumerates every file.

- **Build execution:** `providers/shared/build_host/**` (entire package: orchestration,
  pipeline, execution, dispatch, clone_recipe, common, patches, sandbox, reachability,
  configuration, transports, workspaces, publishing); `providers/local_libvirt/build.py`;
  `providers/remote_libvirt/build.py` + ephemeral build-VM lifecycle / reaping /
  guest build-transport / buildhost-agent diagnostics; `providers/ports/build*.py`;
  `providers/fault_inject/build.py`; `providers/assembly/build_hosts.py`;
  `providers/shared/build_timeouts.py`.
- **Build jobs / telemetry:** `jobs/handlers/runs/build.py`; `domain/build_phase.py`;
  `observability/build_telemetry.py`.
- **Build-host fleet:** `db/build_hosts.py`, `db/build_host_policy.py`,
  `db/buildhost_agent_probes.py`; `reconciler/build_host_fleet.py` +
  `reconciler/repairs/build_hosts.py`; `services/runs/build_host_selection.py`;
  `diagnostics/buildhost_agent*.py`; MCP `ops/build_hosts/**`.
- **Config system:** `build_configs/**` (`catalog`, `defaults`, `platform_config`,
  `rules`, `seed`, `data/kdump.config`); MCP `buildconfig.get/set/list`;
  `admin/build_configs.py`; `inventory/reconcile/build_configs.py`; CLI
  `seed-build-configs`.
- **MCP tools:** `runs.build`, `runs.build_install_boot`, `runs.validate_profile`,
  `runs.profile_examples`; resource `build-source-staging.md`.
- **Upload-config validation:** `_validate_effective_config` (the config check inside
  `validate_external_artifacts`).
- **Tests:** all server-build test directories (~20).

### Explicitly kept (rootfs-image building, not kernel building)

`build-fs` CLI, `providers/local_libvirt/rootfs_build.py`,
`providers/remote_libvirt/rootfs_build.py`, `images/planes/**`,
`jobs/handlers/image_build.py`, and the image catalog. These build the guest OS
image, which both lanes need and Spec 2 extends.

## What is surgically changed (boundary files)

- **`profiles/build.py`** — collapse to one flat `BuildProfile`: delete
  `ServerBuildProfile`, `GitKernelSource`/`GitSourceRef`/`is_git_source`/`git_source_of`,
  `ProfileRequirementsRef`, `MAX_CONFIG_FRAGMENTS`, and the URI-scheme guards.
  `BuildProfile.parse` no longer dispatches on `source`.
- **`mcp/tools/lifecycle/runs/registrar.py` + `create.py`** — `runs.create` takes the
  flat profile; drop the server-build tool registrations and every docstring /
  `suggested_next_actions` that cross-references `runs.build`.
- **`services/runs/admission.py`** — remove the `is_external` / source branch; every
  run is the upload lane.
- **`build_artifacts/validation.py`** — keep `EXTERNAL_BUILD_CONTRACTS` and the
  kernel / vmlinux / initrd checks; make `effective_config` an always-optional,
  **unvalidated** accepted artifact.
- **`services/runs/steps.py`** — keep `BuildStepResult` / finalize (shared with
  install/boot); drop server-build-only helpers.
- **`jobs/payloads.py`, `domain/operations/jobs.py`** — remove `BuildPayload` /
  `BuildInstallBootPayload`; leave the two `JobKind` enum values inert with a comment.
- **`mcp/tool_index.py`** — prune keywords for the deleted tools.
- **`components/requirements.py` + fixture profile YAML (`fixtures/local-libvirt/profiles/*.yaml`)**
  — **INSPECT, do not blanket-delete.** The `ConfigRequirements` validator and
  `load_profile_config_requirements` are build-only and go. But `CmdlineRequirements`
  and `validate_cmdline_requirements` gate the **protected cmdline prefixes**
  (`nokaslr`, the `crashkernel` token) at `runs.install`/`runs.boot` — a *source-agnostic*
  path that must survive. The plan traces which half of this module and the fixture
  `requires.{config,rootfs,cmdline}` blocks are consumed by install/boot before removing
  anything.

## Schema change

One new forward migration:

```sql
DROP TABLE IF EXISTS build_config_catalog;
DROP TABLE IF EXISTS build_host_leases;
DROP TABLE IF EXISTS build_hosts;
DROP TABLE IF EXISTS buildhost_agent_probes;
-- plus ephemeral-build-host columns/rows
-- JobKind enum values BUILD, BUILD_INSTALL_BOOT are left in place:
-- Postgres cannot drop a value from an existing enum without recreating the type.
```

The migration is destructive but touches only server-build infrastructure state.

## Preserved knowledge (for Spec 3)

Captured here before `build_configs/**` is deleted, so Spec 3 can re-advertise these
as per-feature manifests without re-discovering them. Source of record:
`data/kdump.config`, `platform_config.py`, `kdump_support.py`, fixture profile YAML.

| Feature | Required `CONFIG_*` |
|---|---|
| rootfs mount (bootability) | `SQUASHFS=y`, `SQUASHFS_ZSTD=y`, `OVERLAY_FS=y`, `BLK_DEV_LOOP=y`, `XFS_FS=y` (+`XFS_POSIX_ACL=y`) |
| kdump / crash capture | `KEXEC=y`, `KEXEC_CORE=y`, `KEXEC_FILE=y`, `CRASH_DUMP=y`, `VMCORE_INFO=y`, `PROC_VMCORE=y`, `FW_CFG_SYSFS=y`, `RELOCATABLE=y`, `RANDOMIZE_BASE=y` |
| in-kernel config readback | `IKCONFIG=y`, `IKCONFIG_PROC=y` |
| debuginfo (symbols) | `DEBUG_INFO=y`, `DEBUG_INFO_DWARF5=y` (or `DWARF4` / `BTF`), `DEBUG_KERNEL=y` |
| sysrq diagnostics | `MAGIC_SYSRQ=y` |
| KASAN (advertised in docs) | `KASAN=y`, `KASAN_INLINE=y` |
| serial console (per-profile) | `SERIAL_8250_CONSOLE=y`, `VIRTIO_BLK=y`, `VIRTIO_PCI=y` |

> Why the old rootfs-mount *guard* can simply die: Spec 2 hands the agent the image's
> own working `/boot/config-*`, which by definition can already mount that image.
> Bootability is delivered by offering a known-good starting config, not by validating.

## Non-goals (later specs)

- **Spec 2:** capture the image's default kernel version; extract and offer its
  `/boot/config-*` for the agent to own.
- **Spec 3:** advertise each debug feature's required `CONFIG_*`; read the agent's
  uploaded `effective_config` and **arm only the features the kernel supports**.
- Feature arming (crashkernel/kdump reservation, gdbstub provisioning, sysrq) is left
  **as-is** in Spec 1. Spec 3 makes it conditional on kernel support.

## Testing strategy

- Delete all server-build test directories with their code.
- The external-upload lane tests (`test_expected_uploads_tool`, `test_create_upload_tool`,
  `test_complete_build_tool`, `test_validate_external_artifacts`, `test_install`,
  `test_external_provenance`, `runs/test_steps`) must stay green after the surgery.
- Adjust `tests/profiles/**` to the flattened `BuildProfile` (external cases only).
- Add a test that `effective_config` is accepted but **not** validated (a config that
  the old gate would have rejected now uploads and completes).
- Guardrails (`just lint`, `just type`, `just test`, `just docs-check`) green; the
  generated MCP tool reference regenerated for the removed tools.
- Live smoke (may be run at plan time, not gated in CI): upload a prebuilt kernel →
  `runs.complete_build` → `runs.install` → `runs.boot`.

## Risks

- **Unvalidated, optional `effective_config`.** Verify nothing downstream assumes it
  is present or checked. Mitigation: the plan traces every reader of the effective
  config / `profile_requirements` before deletion.
- **Destructive migration.** Only server-build infra tables; irreversible by design.
- **Inert enum values.** `BUILD` / `BUILD_INSTALL_BOOT` remain in `JobKind` — the one
  unavoidable bit of retained cruft, documented in the migration and the enum.
- **Blast radius.** This is a large deletion touching many files; the plan sequences
  it so guardrails stay green at each commit (delete leaves → prune boundary files →
  migration → docs regen).

## Relationship to the other specs

```
Spec 1 (this)  Remove server-build lane        → upload-only, no validation
Spec 2         Image metadata + config offer   → default kernel version + /boot/config-* hand-off
Spec 3         Debug-feature advertise + gate  → per-feature CONFIG manifest; arm only what the kernel supports
```

Spec 1 is a prerequisite: it removes the machinery Specs 2 and 3 would otherwise have
to reconcile with, and it leaves the flat `BuildProfile` and the accepted-but-unread
`effective_config` upload as the seams they build on.
