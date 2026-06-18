# ADR 0163 — Diagnostics: local build-host warm-tree source check

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0091](0091-doctor-diagnostics-model.md) (the
  `Check`/three-state model + server-vs-worker vantage), [ADR-0161](0161-local-warm-tree-build-admission.md)
  (the `KDIVE_KERNEL_SRC` usability rule and `warm_tree_source_error` predicate this check
  reuses), [ADR-0139](0139-diagnostics-worker-vantage-substitution-honesty.md) (the
  worker-vantage substitution honesty this check is unaffected by, and the backlog the
  worker-vantage refinement defers to).
- **Spec:** [`../specs/2026-06-17-diagnostics-local-kernel-src.md`](../specs/2026-06-17-diagnostics-local-kernel-src.md)

## Context

`ops.diagnostics` reports a healthy environment while every build deterministically fails.
The check registry validates the remote-libvirt runtime provider
(`remote_libvirt_reachability`, ADR-0125; `remote_libvirt_base_image_staging`, ADR-0150) and
the secret backend (`secret_ref`, ADR-0091), but it has **no check that touches any build
host**. In the black-box session that motivated #533, `doctor` passed every check while
build → boot → debug was gated by a broken build stage it could not see: a `LOCAL` build host
whose `KDIVE_KERNEL_SRC` was unusable (#532).

The seeded `worker-local` build host (`db/build_hosts.py` `WORKER_LOCAL_ID`) is a database
invariant — `list_all_hosts` documents that a migrated database "always contains at least the
seeded `worker-local` row". Its warm-tree build lane is admitted only when `KDIVE_KERNEL_SRC`
is usable (ADR-0161). When it is unset or invalid, every local warm-tree build fails
deterministically at `sync_tree`/admission — the class of failure (an operator prerequisite a
cheap server-vantage read could have caught) that a doctor exists to surface before the user
pays for it.

`KDIVE_KERNEL_SRC` is readable from the server process: `config.get` resolves the env snapshot
regardless of the setting's `processes=_WORKER` tag (`processes` only gates startup
`validate()`), and `warm_tree_source_error` is a pure predicate over the string plus a
filesystem stat. So the check has the same shape and cost as the existing server-vantage reads
and needs no worker job and no DB read.

#533 also proposes an `ephemeral_libvirt` guest-agent reachability probe (#531). That is a
different shape — it provisions a throwaway VM, the cost-bearing/mutating operation that
`guest_egress` gates behind `--with-egress`, reaper-visible markers, single-flight, a TTL, and
a refusal to wire without an operator-staged image — and it needs an async DB pool the
synchronous factory does not hold (build hosts are DB rows). It is split to a follow-up; this
ADR is the cheap, read-only half (#532).

## Decision

Add a **server-vantage** diagnostic check, `local_kernel_src`, that resolves `KDIVE_KERNEL_SRC`
and reports three-state over the **single shared** `warm_tree_source_error` predicate
(`providers/shared/build_host/workspace.py`) — the same rule `sync_tree` (build-time) and
`check_warm_tree_source_admission` (admission-time, ADR-0161) enforce:

- **`PASS`** — `KDIVE_KERNEL_SRC` is an existing absolute directory.
- **`FAIL`** — it is unset/empty/whitespace, or set but not an existing absolute tree. `fix`
  is the `LOCAL_KERNEL_SRC_FIX` constant (stage a kernel tree + set `KDIVE_KERNEL_SRC`, or
  route builds to a registered git build host). `failure_category = configuration_error`. The
  `unset` and `invalid` cases carry distinct `detail` but the same `fix` (one remediation
  covers both).

There is no `ERROR` outcome: a config read plus a local `stat` cannot be "indeterminate" — it
always reaches a usable/unusable verdict — so unlike the libvirt probes there is no
check-cannot-run boundary to report.

### Layering — mirror the reachability seam exactly

The check class lives in `diagnostics/checks.py` next to the other `Check`s; it consumes an
injected async probe returning a small `WarmTreeSourceOutcome` enum, so the check holds the
three-state policy and is unit-tested without config or filesystem. The production probe is a
new `diagnostics/kernel_src.py` adapter that resolves `config.get(KERNEL_SRC)` (deferred to
probe time, so a post-assembly drift is reflected in the verdict — the `reachability.py`
rationale) and maps `warm_tree_source_error`'s three return values
(`None`/`KERNEL_SRC_UNSET_DETAIL`/`KERNEL_SRC_INVALID_DETAIL`) to the outcome enum.
`checks.py` stays free of any provider import; the one import of the provider-owned predicate
lives in the probe adapter, the only legal place (`diagnostics → providers`).

### Where the `fix` text lives (dependency direction)

`diagnostics → providers` is the only legal import direction, so the diagnostic `fix` constant
`LOCAL_KERNEL_SRC_FIX` is owned by `checks.py` (diagnostic-output policy), mirroring
`BASE_VOLUME_NOT_STAGED_FIX` (ADR-0150). It names the same two build lanes as `workspace.py`'s
`_BUILD_LANE_GUIDANCE` but is an independent, test-asserted literal — a small, low-risk
duplication kept in exchange for a clean dependency direction.

### Wiring

A new `_build_host_checks()` helper returns `[LocalKernelSrcCheck(...)]`;
`default_service_factory` calls it **unconditionally** (the seeded `worker-local` `LOCAL` host
always exists). No new MCP tool, parameter, config setting, migration, DDL, or generated-doc
change: `ops.diagnostics` surfaces every assembled check generically. The factory stays
synchronous and pool-free.

## Consequences

- A deployment whose local warm-tree source is unset/invalid gets a `FAIL` (with the fix) from
  `ops.diagnostics`/`doctor` **before** a build is attempted, and the doctor gate exits nonzero
  on it — the acceptance criterion for #532.
- The check is additive: `secret_ref` and the remote-libvirt checks are untouched, and a
  deployment with a usable `KDIVE_KERNEL_SRC` sees an extra `PASS`. The existing default-factory
  tests that assert the assembled set is exactly `{secret_ref}` are updated to include
  `local_kernel_src` (the check is always assembled).
- The warm-tree rule has one home (`warm_tree_source_error`), now shared by three callers
  (build-time `sync_tree`, admission-time `check_warm_tree_source_admission`, and this
  diagnostic); a future change to the rule changes one function.
- The check reads the **server** process's `KDIVE_KERNEL_SRC`. This is correct when server and
  worker share an environment (the default single-host / compose deployment — the one #532
  reproduced on). A split deployment whose worker has a distinct environment is the
  worker-vantage refinement (see below).
- Because the check is always assembled, a deployment that has the seeded `worker-local` host
  but intentionally never builds locally and never sets `KDIVE_KERNEL_SRC` will see a
  `local_kernel_src` `FAIL`. This is acceptable: the seeded `LOCAL` lane is selectable, so an
  unusable warm-tree source is a real latent failure; respecting an operator's choice to
  disable local builds is the DB-`enabled`-gating refinement (see below).

## Considered & rejected

- **A worker-vantage check (run `KDIVE_KERNEL_SRC` resolution on the worker).** `KDIVE_KERNEL_SRC`
  is a worker-process setting (`processes=_WORKER`), so on a split deployment the worker's value
  is the authoritative one. But a worker-vantage check needs the worker-job dispatch that is
  unwired in this deployment (it would surface as an unavailable substitution under ADR-0139),
  and the issue specifies a server-vantage check "no worker dispatch needed". The server read is
  correct for the shared-env default deployment #532 reproduced on; the split-deployment
  refinement is deferred with the rest of the worker-vantage dispatch backlog (#514). Choosing
  server vantage now ships the #532 fix without taking on dispatch infrastructure.
- **Gate the check on the `LOCAL` host's DB `enabled` flag.** Precise (it would suppress the
  `FAIL` for a deployment that disabled local builds), but it requires enumerating build hosts
  via `list_all_hosts`, which needs an async DB connection the synchronous, pool-free factory
  does not have. Plumbing a pool into the factory is exactly the heavyweight change the #531
  split avoids; it lands with that work. Until then, always-on is the honest default: the
  seeded local lane exists and is selectable, so an unusable source is worth a `FAIL`.
- **Implement #531's ephemeral-libvirt guest-agent probe in this ADR.** It provisions a
  throwaway VM (the cost-bearing, mutating shape `guest_egress` gates behind opt-in + reaper
  markers + single-flight + TTL + a staged-image refusal) and needs the async DB pool above.
  That is a second mutating-probe subsystem, not a check; it is split to a follow-up so the
  cheap #532 fix is not blocked behind it.
- **An `ERROR` outcome for "could not determine".** A config read plus one local `stat` always
  reaches a verdict; there is no backend that can be "simply down" here, so an `error` branch
  would be dead. The libvirt probes have one because their RPC can be unreachable; this check
  does not.
- **Reuse `workspace.py`'s `KERNEL_SRC_UNSET_DETAIL`/`KERNEL_SRC_INVALID_DETAIL` strings
  verbatim as the diagnostic `detail`/`fix`.** Those messages embed the full operator guidance
  in one string and are provider-layer-owned; the diagnostic owns its own output policy
  (`checks.py`), keeps `detail` (the violation) separate from `fix` (the remediation) per the
  `CheckResult` contract, and reuses only the **predicate**, not the prose. The two remediations
  describe the same operator action and are each test-asserted.
