# ADR 0234 — External upload as the default build path; a `contributor` role (#771)

- **Status:** Accepted
- **Date:** 2026-06-24
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0048](0048-external-build-artifact-ingestion.md) (the
  external-build ingestion seam this ADR promotes to the default lane),
  [ADR-0081](0081-remote-build-kernel-bundle.md)/[ADR-0101](0101-local-libvirt-remote-build-host.md) (the
  combined kernel+modules tar this ADR unifies on), [ADR-0006](0006-oidc-rbac-attribution.md)/
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (the project-role rank this ADR inserts
  `contributor` into), [ADR-0013](0013-object-store-layout-retention.md) (the retention-class
  labels this ADR backs with an enforced TTL).
- **Epic:** [#771](https://github.com/randomparity/kdive/issues/771); sub-issues #765–#770.
- **Spec:** [`../superpowers/specs/2026-06-24-external-build-default-and-contributor-role.md`](../archive/superpowers/specs/2026-06-24-external-build-default-and-contributor-role.md).

## Context

The smooth, documented build path is `runs.build` (the *warm-tree* lane): the worker rsyncs a
kernel tree named by the worker-process env var `$KDIVE_KERNEL_SRC` and compiles it. The bare
`kernel_source_ref` on the profile is provenance-only — the worker always builds
`$KDIVE_KERNEL_SRC`, never the ref. This silently couples the build *input* to the worker host:
it only does what the agent intends when the agent's working directory and the worker host are
the same box. External ingestion (ADR-0048) — the agent builds wherever it is and uploads the
artifacts — is the lane that has no such coupling (install and boot read `kernel_ref` from the
same columns regardless of origin), yet today it is the secondary, less-discoverable path.

For an agent investigating or patching a kernel, the obvious mental model is "build in my own
checkout, then hand the result to kdive." Making that the default removes the shared-env-var
coupling from the loop entirely. Four things stand in the way:

1. **Format divergence.** `remote-libvirt` ingests a combined `.tar.gz` (`boot/vmlinuz` +
   `lib/modules/<ver>/`); `local-libvirt` ingests a raw bzImage plus an optional separate
   `modules_ref`. An agent must produce different bytes per provider, and the local path needs a
   separate modules upload for kdump to work.
2. **Access.** The external loop's tools are gated at `operator`
   (`runs.create`/`install`/`boot`/`complete_build`, `artifacts.create_run_upload`, `debug.*`,
   `postmortem.*`, `vmcore.fetch`, `allocations.*`). The base authenticated project role is
   `viewer` (read-only). There is no role between them, so an agent that only needs to build,
   boot, and debug must be granted full `operator` — which also grants system definition, image
   management, and host control.
3. **Retention.** `retention_class` is only an S3-lifecycle label; no build/kernel artifact has
   an enforced TTL, and closing an investigation flips a state flag with no cleanup. Agent-driven
   uploads will accumulate uploaded kernels indefinitely.
4. **Discoverability.** The required artifact format lives in the validator, not in any tool
   response, so "build and upload" is not self-describing over MCP.

## Decision

### 1. External upload is the default, documented build lane

The promoted loop is `runs.create(source="external") → artifacts.expected_uploads → upload →
runs.complete_build → install → boot`, self-described over MCP. The internal builders (warm-tree
from `$KDIVE_KERNEL_SRC`, worker/remote git-clone) remain supported secondary lanes — no removal.
`$KDIVE_KERNEL_SRC` and the warm-tree lane are documented as a single-host convenience, not the
backbone.

### 2. One artifact format across providers

Both providers ingest the **combined kernel+modules tar** already produced by remote-libvirt:
a gzip tar with `boot/vmlinuz` and `lib/modules/<ver>/`, excluding the `build`/`source` symlinks.
`local-libvirt` converges onto this shape; the separate `modules_ref` is **removed** (replace,
don't deprecate). The local libguestfs module injector already consumes tar member paths; the one
local-specific need — a raw bzImage for the libvirt `<kernel>` element — is met by extracting
`boot/vmlinuz` from the tar host-side before domain redefine. No per-provider artifact shape
remains. (Implemented in #766.)

### 3. A `contributor` project role for the build-debug loop

A new project role **`contributor`** is inserted into the existing rank:
`viewer (0) < contributor (1) < operator (2) < admin (3)`. Because the roles form a total rank,
`operator` and `admin` retain every `contributor` power for free; `viewer` is unchanged
(read-only: accounting, audit, activity, catalog reads). Role values are strings in the verified
`roles` claim and rank integers live only in `_RANK`/`_ROLE_RANK`, so the insertion needs no
migration and no persisted-state change.

`contributor` is the lowest role that can run a full crash-investigation loop. The tools that
drop from `operator` to `contributor`:

| Tool | Why contributor |
|------|-----------------|
| `runs.create` (bound + unbound), `runs.bind`, `runs.build`, `runs.complete_build`, `runs.install`, `runs.boot`, `runs.cancel` | the build→install→boot loop |
| `artifacts.create_run_upload` | upload a built kernel to a run |
| `debug.start_session`/`end_session`/`continue`/`interrupt`/`set_breakpoint`/`clear_breakpoint`/`read_memory`/`read_registers` | live + post-mortem debugging |
| `postmortem.crash`, `postmortem.triage`, `vmcore.fetch` | post-mortem analysis |

`postmortem.crash`/`postmortem.triage` were inconsistent before this change: gated at `viewer`
at the handler but advertised at `operator` in the exposure map. Because `postmortem.crash` runs
an arbitrary command list against a core (investigation work, not accounting/audit), both the
runtime gate and the exposure entry are consolidated to `contributor` — a tightening that removes
crash-command execution from pure `viewer`s while leaving every contributor and above unaffected.
| `allocations.request`/`release`/`renew` | obtain and hold a target |
| `investigations.open`/`close`/`link`/`unlink`/`set` | organize the work |

What stays **`operator`**: ~~`systems.define`/`provision`/`provision_defined`/`reprovision`,~~
~~`control.power`,~~ `images.upload`/`delete`, and ~~**`artifacts.create_system_upload`**~~. What stays
**`admin`**: `systems.teardown`, `control.force_crash`, `accounting.set_budget`/`set_quota`.
Platform-role tools are unaffected.

*Superseded in part: [ADR-0320](0320-leaseholder-power-lifecycle.md) moved `control.power` to
`contributor`; [ADR-0326](0326-provision-lane-contributor-lifecycle.md) moved the provision lane
(`systems.define`/`provision`/`provision_defined`, `artifacts.create_system_upload`) and
`systems.reprovision` to `contributor`. Only `images.upload`/`delete` remain `operator` from this
list.*

~~`artifacts.create_run_upload` and `artifacts.create_system_upload` share one enforcement seam
(`_create_upload`); the gate becomes conditional on the owner kind (run → `contributor`,
system → `operator`) rather than a single role constant.~~ *(Superseded by ADR-0326: both owner
kinds are now `contributor`.)*

### 4. Uploaded build artifacts expire and clear on close

Uploaded build/kernel artifacts get an enforced lifetime, settled here, implemented in #768
(migration **0048**):

- **Clear-on-close.** `investigations.close` marks the investigation for cleanup; a
  `gc_investigation_artifacts` repair in the reconciler (modeled on `gc_report_artifacts`)
  deletes the S3 objects + artifact rows for runs under a closed investigation after a grace
  period, with per-object failure handling.
- **TTL backstop.** Uploaded build artifacts carry a retention TTL independent of close, so
  never-closed investigations do not accumulate forever.
- **Scope is explicit.** Uploaded kernel/vmlinux/initrd are in scope to delete; **console and
  other crash evidence retention is a separate, deliberate choice** (it is the A/B evidence
  epic #764/#761 is working to *preserve*), reconciled with #761 rather than swept in here.
- **Decision-3/decision-4 interaction (binding constraint on #768).** Because decision 3 lowers
  `investigations.close` to `contributor`, close must not become a destructive evidence operation
  in a `contributor`'s hands. #768's clear-on-close is therefore constrained to be: (a) a deferred
  reconciler sweep after a grace period (never a synchronous delete in the `close` call path),
  (b) scoped to uploaded *build* artifacts (kernel/vmlinux/initrd) — never console or other crash
  evidence, and (c) audited. With those constraints a `contributor` closing its own investigation
  reclaims only the build inputs it uploaded, after a delay, reversibly within the grace window —
  not crash evidence. If #768 cannot honor (a)–(c), `investigations.close` must move back to
  `operator` rather than weaken the constraint.

### 5. kdive advises the required format

`artifacts.expected_uploads` (and the profile examples / `suggested_next_actions`) are enriched
to state, per artifact name, required-vs-optional, the format/magic contract, and the internal
layout — provider-neutral after decision 2 (one combined `kernel` tar; optional `vmlinux` with a
required matching `build_id`; optional `initrd`; conditional `effective_config`). An agent can
learn exactly what bytes to produce from MCP alone. (Implemented in #769, after #766.)

Decision 3 (the `contributor` role) landed in #765, decision 2 (the unified combined kernel tar)
in #766, and the base-role access in #767; the ADR is **Accepted** as those core decisions are in
effect. Decisions 4 (retention TTL + clear-on-close, #768) and 5 (the enriched format advisory,
#769) are additive follow-ups that build on the now-ratified contract.

## Consequences

- An agent granted only `contributor` can build a kernel in its own checkout, upload it, install,
  boot, and debug — without the `operator` grant that also confers system definition, image
  management, and host power control. `viewer` stays a pure observer.
- Every `operator`/`admin` principal keeps the full external loop unchanged (rank superset), so
  no existing grant loses a capability.
- **`viewer` loses runtime access to `postmortem.crash`/`postmortem.triage`** (and
  `debug.list_breakpoints`, whose exposure is corrected to match its long-standing engine-op
  runtime gate). These were never advertised to `viewer` (exposure was `operator`), but a viewer
  that called them by name at runtime could; after this change it is denied. An operator upgrading
  a deployment whose viewers relied on that undocumented path must grant `contributor`.
- The exposure classifier gains a `PROJECT_CONTRIBUTOR` scope and the re-gated tools advertise to
  contributors; the completeness guard still pins `CLASSIFIED_TOOLS | PUBLIC_TOOLS` to the
  registry.
- Both providers will build and install from one combined `kernel` tar; kdump works on
  local-libvirt with no separate modules upload (#766).
- Uploaded kernels will be freed on investigation close and by TTL (#768, migration 0048).
- Rank integers shift (`operator` 1→2, `admin` 2→3); they are compared relatively and never
  persisted, so the shift is internal. The `roles` claim is unchanged for existing tokens.

## Considered & rejected

- **Lower the external-loop tools to `viewer`.** Rejected: it widens "read-only viewer" to mean
  "can boot kernels and attach a debugger," collapsing the audit/accounting observer role the
  deployment relies on. A distinct role keeps `viewer` honest.
- **An independently-granted capability (platform-role style) instead of a rank insertion.**
  Rejected as premature machinery: the platform-role partial order exists for separation of
  duties between infra and project data; here `contributor ⊂ operator ⊂ admin` is a genuine total
  order (every higher role should do everything a lower one can), which the existing rank already
  models. An independent capability would also strip upload from current operators unless we
  added an implication, for no benefit.
- **Per-object ("own runs only") scoping for `contributor`.** Rejected: the rank model is
  project-scoped, not owner-scoped — `contributor` granting `allocations.release` means release of
  any allocation in the project, like every other role. Owner-scoped authorization is a separate,
  larger change (a new dimension on `RequestContext`), out of scope; the project boundary is the
  trust boundary today.
- **Keep the per-provider artifact formats and just document them.** Rejected: it leaves the
  agent producing different bytes per provider and forces a separate modules upload on
  local-libvirt for kdump. One format is the point of "build and upload" being obvious.
- **A DB `expires_at` column on every artifact.** Rejected for the close-driven case: linkage
  (artifact → run → investigation) already lets the reconciler find what a closed investigation
  owns, mirroring `gc_report_artifacts`. The TTL backstop (decision 4) is the only new persisted
  field, scoped to uploaded build artifacts (#768, migration 0048).
- **Remove the internal warm-tree / git-clone lanes.** Rejected: the user explicitly wants the
  external path to be the default, not the only method. Single-host setups keep the warm-tree
  convenience; CI/build-host topologies keep the remote git-clone lane.
