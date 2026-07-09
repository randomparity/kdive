# ADR 0316 — Remove the server-build lane; build only from uploaded artifacts

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** kdive maintainers
- **Spec:** [`../superpowers/specs/2026-07-08-remove-server-build-lane-design.md`](../superpowers/specs/2026-07-08-remove-server-build-lane-design.md)
  (spec 1 of a three-spec redesign; specs 2 and 3 are [#1051](https://github.com/randomparity/kdive/issues/1051)
  and [#1052](https://github.com/randomparity/kdive/issues/1052)).
- **Supersedes:** [ADR-0137](0137-build-profile-schema-discoverability.md) (server-build profile
  schema + discoverability), [ADR-0161](0161-local-warm-tree-build-admission.md) (local warm-tree
  build admission), [ADR-0265](0265-warm-tree-dirty-provenance.md) (warm-tree dirty provenance),
  [ADR-0282](0282-warm-tree-dirty-file-manifest.md) (warm-tree dirty-file manifest). Each described
  a facet of the now-removed server-build lane.
- **Amends:** [ADR-0234](0234-external-build-default-and-contributor-role.md) — the external-upload lane it made the
  default is now the *only* lane. The build-config catalog / fragment machinery
  ([ADR-0096](0096-kdump-config-fragment-build-input.md), ADR-0065's `ConfigRequirements` validator)
  and the always-on rootfs/crash config guards are removed; kdive no longer inspects a `.config`.

## Context

kdive offered two ways to get a kernel onto a VM: a **server-build lane** (the worker checked out
a kernel tree, merged a `.config` fragment, validated it against hard-coded requirements, and
compiled the kernel on a build host or ephemeral build VM) and an **external-upload lane** (the
agent builds locally and uploads the artifacts, which kdive installs).

The server-build lane had grown into a large, tangled subsystem — a whole
`providers/shared/build_host/**` package, per-provider builders, an ephemeral build-VM lifecycle,
a build-host fleet with leases and agent probes, a build-config catalog with fragment composition
and multi-gate `.config` validation, and the MCP surface to drive it. It was hard for a human *and*
an agent to navigate, and its config-validation gates actively fought the agent (a composed config
that dropped a symbol was rejected mid-build). The external-upload lane already existed, was
self-sufficient, and was the documented default (ADR-0234); install and boot are source-agnostic —
they consume `run.kernel_ref` / `BuildStepResult` regardless of who produced the artifacts.

## Decision

Delete the entire server-build lane and all kernel-config validation. The external-upload lane is
the only lane: the agent builds the kernel locally, uploads artifacts to S3, and kdive installs and
boots them. kdive never compiles a kernel and never inspects or validates a `.config`.

Concretely:

- **Build profile** collapses to one flat `BuildProfile` — no `source` discriminator, no
  `ServerBuildProfile`, no git-source types, no `profile_requirements`. (Superseding ADR-0137.)
- **Deleted:** `providers/shared/build_host/**`, both providers' kernel `build.py`, the ephemeral
  build-VM lifecycle, the build-host fleet (`db`/`reconciler`/`services` + `ops.build_hosts` tools),
  the build-config catalog (`build_configs/**`, `buildconfig.*` tools, the kdump fragment,
  `platform_config`), the build job handlers, `runs.build` / `runs.build_install_boot` /
  `runs.validate_profile` / `runs.profile_examples`, and warm-tree build diagnostics.
- **`effective_config`** stays an accepted upload artifact but is **no longer validated** — Spec 3
  will *read* it (advisory) to gate debug features, never to reject a build.
- **Schema:** a drop migration removes `build_config_catalog`, `build_hosts`, `build_host_leases`,
  `buildhost_agent_probe_guests`. The `JobKind.BUILD` / `BUILD_INSTALL_BOOT` enum values are left
  inert (Postgres cannot drop a value from an existing enum without recreating the type).

## Consequences

- The surface an agent (and a maintainer) must navigate shrinks dramatically (~28k lines removed).
- Bootability is no longer guaranteed by a validation guard; it is delivered by Spec 2 handing the
  agent the image's own known-good `/boot/config-*` as a starting point.
- Re-adding a server-side build later is a deliberate, additive change (the flat `BuildProfile`
  reintroduces a discriminator, plus a data migration) — accepted for a clean surface now.
- The `JobKind.BUILD*` enum members and `BuildPayload` types remain as inert vestiges.

## Considered & rejected

- **Keep the `source` discriminator (external-only).** Cleaner re-add path later, but leaves a
  now-meaningless field on every profile; a flat type is simpler for the only lane that exists.
- **Deprecate rather than delete (feature-flag the server lane off).** Retains the maintenance
  burden and the agent-confusing surface this ADR exists to remove.
- **Leave the orphaned tables in place (no drop migration).** Dead schema misleads future readers
  and agents; the drop is safe (server-build infra state only).
