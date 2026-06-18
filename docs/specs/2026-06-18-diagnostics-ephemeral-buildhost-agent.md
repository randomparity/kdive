# Spec — Diagnostics ephemeral-libvirt build-host guest-agent check (#544, #531)

- **Issue:** [#544](https://github.com/randomparity/kdive/issues/544) (split from
  [#533](https://github.com/randomparity/kdive/issues/533))
- **ADR:** [`0167`](../adr/0167-diagnostics-ephemeral-buildhost-agent-check.md)
- **Covers:** #531 (the ephemeral-libvirt guest-agent reachability blind spot) and the
  ADR-0163-deferred `enabled`-gate on `local_kernel_src`.
- **Date:** 2026-06-18

## Problem

`ops.diagnostics` validated the remote-libvirt runtime provider and the secret backend, and
(ADR-0163) the seeded local build host's warm-tree source, but it has no check that touches an
`ephemeral_libvirt` build host's **runtime**. Each `ephemeral_libvirt` host provisions a throwaway
`kdive-build-<run_id>` builder VM per build (ADR-0100) over its operator-staged base build image. If
that image boots but its `qemu-guest-agent` never connects, every build routed to the host fails
deterministically at `wait_for_agent` — the #531 failure, invisible to `doctor`.

This probe **provisions cost-bearing infrastructure on every run** (the `guest_egress` shape), so it
must carry the ADR-0091 mutating-probe guards: opt-in, reaper-visible markers, single-flight, and an
honest treatment of the operator-staged-image prerequisite.

## Decision (per ADR-0167)

### 1. New check `ephemeral_libvirt_buildhost_agent` (server vantage, opt-in)

Assembled into the service only under a new `with_buildhost_agent` opt-in. At **probe time** it
enumerates `kind='ephemeral_libvirt' AND enabled=true` build hosts and probes each:

| Per-host observation | `BuildHostAgentOutcome` |
|---|---|
| agent connected, trivial in-guest command `rc 0` | `AGENT_READY` |
| builder started, guest agent never connected (`session` raised `PROVISIONING_FAILURE`) | `AGENT_UNREACHABLE` |
| host/config unreachable (`CONFIGURATION_ERROR` / `TRANSPORT_FAILURE` / `INFRASTRUCTURE_FAILURE`), or the host has no staged `base_image_volume` | `HOST_UNREACHABLE` |

Aggregated into **one** `CheckResult` (precedence mirrors `secret_ref`):

| Aggregate condition | Status | failure_category | fix |
|---|---|---|---|
| any host `AGENT_UNREACHABLE` | `fail` | `configuration_error` | `BUILDHOST_AGENT_FIX` |
| no `fail`, but any `HOST_UNREACHABLE`, or **no** `ephemeral_libvirt` host registered | `error` | (carried for transport/config) | — (never on error) |
| every probed host `AGENT_READY` | `pass` | — | — |

The probe provisions through `EphemeralBuildVm.session(base_image_volume, run_id=…, wait_network=False)`:
provision → `wait_for_agent` → yield transport → run one trivial command (e.g. `["true"]`) →
teardown in `finally`. `wait_network=False` scopes it to agent reachability so a network timeout can
never be misreported as `AGENT_UNREACHABLE`; `source=None` already skips the egress preflight.

### 2. Mutating-probe guards (ADR-0091)

- **Opt-in:** `doctor --with-buildhost-agent` / `ops.diagnostics(with_buildhost_agent=true)`,
  independent of `--with-egress`. Provisioning is audited under its own `ops.diagnostics.buildhost_agent`
  event, distinct from the read-only run.
- **Reaper markers:** a `buildhost_agent_probe_guests` row (`build_host_id`, `run_id`, `heartbeat_at`,
  `ttl_deadline`, `released_at`) is written before the builder boots; a heartbeat task advances
  `heartbeat_at` for the probe's whole duration. `reap_orphan_build_vms` gains one live-holder clause:
  a `kdive-build-<run_id>` domain whose `run_id` has a fresh, unreleased probe heartbeat (within the
  staleness window and before `ttl_deadline`) is **live** and not reaped. A leaked probe (process
  died → heartbeat stale) is reaped by that same sweep; `ttl_deadline` is the hard backstop.
- **Single-flight:** in-process `SingleFlight` (reused from `egress_probe`) keyed on `build_host_id`,
  backstopped by a partial-unique index on `build_host_id WHERE released_at IS NULL`. A cross-process
  second caller hits the index → `ProbeInFlightError` → that host contributes `HOST_UNREACHABLE`
  ("a probe is already in flight for this host").
- **Staged image:** required per host (`build_hosts.base_image_volume`). A host without it →
  `HOST_UNREACHABLE`, never a silent drop. Zero `ephemeral_libvirt` hosts → `error`.

### 3. `enabled`-gate `local_kernel_src` (ADR-0163 follow-up)

`LocalKernelSrcCheck` gains an injected deferred `enabled` probe (default always-enabled). When the
seeded `worker-local` host is **disabled**, the check returns `pass` with an "n/a — local build host
disabled" detail (clears the ADR-0163 `0 → 1` exit regression). Enabled → unchanged warm-tree
verdict. A DB error resolving the flag fails **open to enabled** (never hides the latent failure).

## Components

| Component | File | Change |
|---|---|---|
| Migration | `db/schema/0041_buildhost_agent_probe_guests.sql` | new table + partial-unique index |
| Marker repo | `db/buildhost_agent_probes.py` | `register` / `heartbeat` / `release` / `is_probe_live` |
| Check + outcome enum + fix | `diagnostics/checks.py` | `EphemeralLibvirtBuildHostAgentCheck`, `BuildHostAgentOutcome`, `BUILDHOST_AGENT_FIX`; `enabled` probe on `LocalKernelSrcCheck` |
| Production probe adapter | `diagnostics/buildhost_agent.py` | enumerate hosts + wrap `EphemeralBuildVm.session` (the only `EphemeralBuildVm` import) |
| Service assembly | `diagnostics/service.py` | `with_buildhost_agent` param; assemble the check under it with generous timeouts; wire the `local_kernel_src` `enabled` probe |
| Build-VM session | `providers/remote_libvirt/lifecycle/build_vm.py` | `wait_network: bool = True` kwarg on `session()` |
| Reaper guard | `reconciler/repairs/build_hosts.py` | `reap_orphan_build_vms` honors a fresh probe heartbeat |
| Tool | `mcp/tools/ops/diagnostics.py` | `with_buildhost_agent` param + distinct audit event |
| CLI | `cli/commands/registry.py`, `cli/commands/doctor.py` | `--with-buildhost-agent` flag → payload |
| App wiring | `mcp/app.py` | thread `with_buildhost_agent` through `_service_factory` |

## Acceptance criteria

1. `doctor --with-buildhost-agent` against a host whose builder boots but whose agent never connects
   returns a `fail` naming the host, with `BUILDHOST_AGENT_FIX`, and exits nonzero.
2. The same against an unreachable host returns `error` (not `fail`) — no confident wrong fix.
3. A healthy host returns `pass`; a deployment with zero `ephemeral_libvirt` hosts returns `error`.
4. Without the flag, `ops.diagnostics` is unchanged (no builder is provisioned).
5. A `kdive-build-<run_id>` domain with a fresh probe heartbeat is not reaped by
   `reap_orphan_build_vms`; one with a stale heartbeat and no live BUILD job is.
6. Two concurrent probes against one host provision exactly one builder (in-process coalesced;
   cross-process → the second reports in-flight).
7. `local_kernel_src` returns `pass` (n/a) when the seeded host is disabled, and its prior
   warm-tree verdict when enabled.
8. The provisioning action is audited under `ops.diagnostics.buildhost_agent`, distinct from the
   read-only run; `with_buildhost_agent` is rejected for a non-`platform_operator` caller (the
   existing gate).

## Out of scope

- Worker-vantage build-host checks (the split-deployment refinement; ADR-0163 backlog).
- Reachability probing of SSH build hosts (ADR-0103 already flips their state via the reconciler).
- Un-gating `guest_egress` (its deployment-wide probe-guest seam is still unwired).
