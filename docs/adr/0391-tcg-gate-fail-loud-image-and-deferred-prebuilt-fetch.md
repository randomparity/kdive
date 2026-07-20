# ADR 0391 — Fail loud on the TCG gate's guest image; defer the fetch-prebuilt redesign

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-20
- **Deciders:** Maintainer (randomparity), Claude Code

## Context

The hosted `tcg` job in `.github/workflows/live.yml` (epic #1289 sub-D, ADR-0389
**Proposed**) stages its ppc64le guest by running `build-fs` — which customizes the
rootfs **by booting it** (ADR-0345). On the x86 `ubuntu-latest` hosted runner that
boot runs under **TCG emulation** (no KVM), minutes-to-tens-of-minutes per boot, so
whether a full customize-boot plus the subsequent proof boot completes inside
`timeout-minutes: 30` is unmeasured (#1320).

The same first live dispatch also exposed a concrete wiring bug the earlier fix
(PR #1318) missed. PR #1318 repaired the `workflow_dispatch` `tcg_image` **input
default** (line ~17) but not the run block's schedule/push **fallback** (line ~104),
which still read `${TCG_IMAGE_INPUT:-fedora-ppc64le}`. `fedora-ppc64le` is not a
`rootfs_catalog.toml` entry, so every scheduled or push run resolved to a bogus
image. `test_live_workflow_shape` only pinned the input default, not the fallback.

Two questions had to be answered: (1) how the gate should react to an unknown/bad
image, and (2) whether to remove the emulated build entirely by fetching a prebuilt
ppc64le rootfs instead (#1320's Option 2).

## Decision

**1. Fail loud at the image, keep building under TCG for now.**

- Repair the line-104 fallback to the same real catalog entry as the input default
  (`fedora-kdive-ready-43-cloud-ppc64le`) and pin the two defaults **equal** in
  `test_live_workflow_shape` so a dispatch and a scheduled run build the same guest
  and neither default can silently drift to a non-catalog name.
- Rely on `build-fs`'s existing loud rejection of an unknown `--image` (it exits
  non-zero and lists the valid catalog names) rather than duplicating catalog
  knowledge in the shell — verified: a bogus `--image` exits 2 with the available
  names, so under `set -euo pipefail` the stager fails at the `build-fs` call, not
  deep at the later `virt-ls`.
- Add one shell-side guard for the **other** miss the issue named: a build that
  exits 0 without producing the qcow2. `produce_rootfs_and_kernel` now asserts a
  non-empty `rootfs.qcow2` immediately after `build-fs`, so a silent build miss
  fails there with a clear message instead of surfacing as the misleading
  `virt-ls: … rootfs.qcow2: No such file`. This helper is shared, so the warm-store
  path gets the same guard.

**2. Defer the fetch-prebuilt redesign — no source exists to fetch from.**

Removing the emulated customize-boot by having the gate **fetch** a prebuilt
kdive-ready ppc64le rootfs + kernel presupposes such an artifact is published
somewhere the ephemeral hosted runner can reach. It is not:

- Every `rootfs_catalog.toml` `source.kind` is `cloud-image` — an **upstream base**
  cloud image that still requires `build-fs` customization to become kdive-ready.
  There is no catalog row, registry, or URL for a prebuilt *kdive-ready* rootfs.
- The self-hosted **warm store** lives on that runner's local disk
  (`/var/lib/kdive/warm-store`) — unreachable from the hosted TCG runner.
- The live stack's S3/MinIO is stood up per-run (`compose up`) and torn down — not a
  persistent, operator-populated store.

Publishing such an artifact (a persistent object store or release, an operator
build-and-publish pipeline, pin/verify wiring, and its tests) is `effort:L`,
cannot be live-verified from this change, and would be a phantom feature until a
real artifact backs it. It is therefore split out for a dedicated issue rather than
fabricated here. ADR-0389 stays **Proposed**; measuring the TCG wall-time (its
Task 7) or standing up the publish pipeline remains the operator's open path to
Accept it.

## Consequences

- The scheduled/push TCG gate now resolves a real image, so its first failure mode
  is the actual boot/timeout question, not a guaranteed bad-image failure.
- Bad-image and empty-build misses fail loud at their source with actionable
  messages; `test_live_workflow_shape` guards both defaults and their agreement.
- The 30-minute floor timeout and the emulated customize-boot remain; the timeout
  viability from #1320 is unresolved and tracked there.
- No production code, schema, or migration changes — CI/test/scripts only.

## Alternatives considered

- **Measure and set the real timeout (#1320 Option 1 / ADR-0389 Task 7).** Needs a
  full live run to completion; operator-only, deferred.
- **Fetch a prebuilt ppc64le rootfs (#1320 Option 2).** Deferred — no publish source
  exists (above); inventing one exceeds this issue's scope.
- **A shell-side catalog-membership pre-check in `stage-tcg-images.sh`.** Rejected as
  duplicate: `build-fs` already fails loud and authoritatively on an unknown image.
