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
| agent connected (`wait_for_agent` returned) but the trivial command returned `rc != 0` | `AGENT_UNREACHABLE` |
| agent connected but the exec then raised `TRANSPORT_FAILURE` (agent dropped mid-exec) | `AGENT_UNREACHABLE` |
| host/config unreachable before the agent connected (`CONFIGURATION_ERROR` from `lookup_pool`/`ensure_named_overlay`, `TRANSPORT_FAILURE` from the TLS connect, `INFRASTRUCTURE_FAILURE` from a libvirt RPC), or the host has no staged `base_image_volume`, or a probe is already in flight for the host | `HOST_UNREACHABLE` |

A demonstrably-connected agent that then fails (rc != 0 or a mid-exec drop) is a broken builder
(`AGENT_UNREACHABLE` → `fail`), not an unreachable host: emitting `error` there would understate a
real broken-image fault, since the host was reachable. The distinction the adapter makes is
**whether `wait_for_agent` returned** before the failure: a failure before it is `HOST_UNREACHABLE`,
a failure at or after it is `AGENT_UNREACHABLE`.

Aggregated into **one** `CheckResult` (precedence mirrors `secret_ref`):

| Aggregate condition | Status | failure_category | fix |
|---|---|---|---|
| any host `AGENT_UNREACHABLE` | `fail` | `configuration_error` | `BUILDHOST_AGENT_FIX` |
| no `fail`, but any `HOST_UNREACHABLE`, or **no** `ephemeral_libvirt` host registered | `error` | see rule below | — (never on error) |
| every probed host `AGENT_READY` | `pass` | — | — |

The single aggregate `error` result carries a **deterministic** `failure_category`: `transport_failure`
only when **every** error cause was a transport drop (TLS/libvirt RPC); otherwise `configuration_error`
(no hosts registered, no staged base image, a mix of causes, or any config cause present). This is a
fixed rule, not "whichever host was last", so the category is stable for programmatic triage; the
per-host specifics live in `detail`.

The probe provisions through `EphemeralBuildVm.session(base_image_volume, run_id=…, wait_network=False)`:
provision → `wait_for_agent` → yield transport → run one trivial command (e.g. `["true"]`) →
teardown in `finally`. `wait_network=False` scopes it to agent reachability so a network timeout can
never be misreported as `AGENT_UNREACHABLE`; `source=None` already skips the egress preflight.

#### Timeouts (this is the first mutating check that actually assembles)

The service's default per-check (10 s) / overall (30 s) timeouts (`diagnostics/service.py`) are far
below the builder's `wait_for_agent` bound (180 s, `build_vm.py`), so under defaults `run_check`
would map the probe to `error` at 10 s — it could never `pass` or `fail`. Therefore: when
`with_buildhost_agent` is set, `default_service_factory` builds the service with **generous**
timeouts — per-check `>=` the builder's agent-wait bound plus margin, and an overall budget that
covers every probed host. The hosts are probed **sequentially** inside the one check (so the
builder footprint is one VM at a time), and the per-check timeout bounds the **whole** check, so it
must be `>= N_hosts * (agent_wait + teardown) + margin`; the overall timeout is set to `None` (the
per-check bound is the cap) or to the same generous value. The assembled-timeout values are an
acceptance criterion (they are invisible to the injected-probe unit tests).

**Cross-check timeout coupling (intended, stated).** `DiagnosticsService` applies **one**
`per_check_timeout` to every assembled check (`_run_within_budget` uses `min(self._timeout, remaining)`).
So raising it for the build-host probe also raises it for the cheap checks co-assembled in the same
run (`secret_ref`, `local_kernel_src`, and, when a remote provider is configured,
`remote_libvirt_reachability` / `remote_libvirt_base_image_staging`). A *healthy* cheap check is
unaffected (the ceiling is an upper bound, not a delay), but a *hung* one — e.g. a `reachability`
probe against a black-holed remote host — is now bounded at the generous value instead of 10 s. This
is accepted: `--with-buildhost-agent` is an explicit, rarely-run operator action that already
provisions a builder, so a looser bound on a co-assembled hung check during that run is a reasonable
trade for not adding per-check timeouts to the framework. The default run (no flag) keeps the tight
10 s / 30 s bounds unchanged. The probe additionally self-bounds: each per-host
`EphemeralBuildVm` carries its own `wait_for_agent` deadline (180 s) and teardown, so the service
timeout is a backstop above the probe's own internal bound, not the probe's only guard.

#### Cancellation and cleanup

`EphemeralBuildVm.session()` is a **synchronous** contextmanager (blocking libvirt + `time.sleep`),
so the adapter runs it inside `asyncio.to_thread` while an async heartbeat task beats. A thread is
**not cancellable**: if `run_check`'s `asyncio.timeout` fires mid-probe, the awaiting coroutine
raises `TimeoutError` but the thread runs `session()` (including its `finally` teardown) to
completion, detached. So the heartbeat-cancel and marker-release live in the **probe coroutine's**
`finally` (not only the threaded session's), so cancellation still stops the heartbeat and releases
(or, if the coroutine is hard-cancelled before release, leaves to TTL) the marker. A leaked builder
from an orphaned thread is reclaimed by `reap_orphan_build_vms` once the heartbeat goes stale, with
`ttl_deadline` as the hard backstop. The generous per-check timeout makes this the rare path.

### 2. Mutating-probe guards (ADR-0091)

- **Opt-in:** `doctor --with-buildhost-agent` / `ops.diagnostics(with_buildhost_agent=true)`,
  independent of `--with-egress`. Provisioning is audited under its own `ops.diagnostics.buildhost_agent`
  event, distinct from the read-only run.
- **Reaper markers:** a `buildhost_agent_probe_guests` row (`build_host_id`, `run_id`, `heartbeat_at`,
  `ttl_deadline`, `released_at`) is written before the builder boots; a heartbeat task advances
  `heartbeat_at` for the probe's whole duration. `reap_orphan_build_vms` gains one live-holder clause:
  a `kdive-build-<run_id>` domain whose `run_id` has a fresh, unreleased probe heartbeat (within the
  staleness window and before `ttl_deadline`) is **live** and not reaped. The staleness/TTL
  predicate (`db.buildhost_agent_probes.is_probe_live`) evaluates `now()` **in Postgres** — never a
  Python clock — matching `provider_reaping`'s clock-in-DB convention and reusing
  `DEFAULT_PROBE_HEARTBEAT_STALE_AFTER`. A leaked probe (process died → heartbeat stale) is reaped by
  that same sweep; `ttl_deadline` is the hard backstop.
- **Single-flight:** a **module-level** `SingleFlight` (reused from `egress_probe`) keyed on
  `build_host_id`. It **must** be a process-level singleton, not built per factory call:
  `default_service_factory` is invoked fresh on every `ops.diagnostics` call (the `_service_factory`
  closure in `mcp/assembly/app.py`), so a per-assembly coalescer would coalesce nothing — `egress_probe`'s
  own `SingleFlight` docstring calls this out. The DB partial-unique index on
  `build_host_id WHERE released_at IS NULL` is the cross-process backstop: a second *process* that
  cannot share the coalescer hits the index → `ProbeInFlightError` → that host contributes
  `HOST_UNREACHABLE` ("a probe is already in flight for this host"), still exactly one builder.
- **Capacity:** the probe is **not** a BUILD job and takes **no** build-host lease, so its builder is
  not counted against `build_hosts.max_concurrent`. This is deliberate: an operator-initiated probe
  provisions one transient builder per host (single-flighted), so it may transiently over-subscribe a
  saturated host by one VM (4 vCPU / 8 GiB, per `render_build_domain_xml`). The probe does not block
  on or consult lease capacity; the footprint is documented so an operator running it against a busy
  host is not surprised.
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
| App wiring | `mcp/assembly/app.py` | thread `with_buildhost_agent` through `_service_factory` |

## Acceptance criteria

1. `doctor --with-buildhost-agent` against a host whose builder boots but whose agent never connects
   returns a `fail` naming the host, with `BUILDHOST_AGENT_FIX`, and exits nonzero.
2. The same against an unreachable host returns `error` (not `fail`) — no confident wrong fix.
3. A healthy host returns `pass`; a deployment with zero `ephemeral_libvirt` hosts returns `error`.
4. Without the flag, `ops.diagnostics` is unchanged (no builder is provisioned).
5. A `kdive-build-<run_id>` domain with a fresh probe heartbeat is not reaped by
   `reap_orphan_build_vms`; one with a stale heartbeat and no live BUILD job is.
6. Two concurrent **same-process** probes against one host provision exactly one builder (the
   module-level `SingleFlight` coalesces them); a cross-process second caller reports in-flight
   (`HOST_UNREACHABLE`).
7. `local_kernel_src` returns `pass` (n/a) when the seeded host is disabled, and its prior
   warm-tree verdict when enabled; a DB error resolving the flag falls open to enabled.
8. The provisioning action is audited under `ops.diagnostics.buildhost_agent`, distinct from the
   read-only run; `with_buildhost_agent` is rejected for a non-`platform_operator` caller (the
   existing gate).
9. With `with_buildhost_agent`, the assembled `DiagnosticsService`'s per-check timeout is `>=` the
   builder's `wait_for_agent` bound plus margin (so the probe can reach a `pass`/`fail` verdict
   rather than always timing out to `error`); without the flag, the default timeouts are unchanged.
10. An agent that connects but whose trivial command returns `rc != 0` (or drops mid-exec) yields a
    `fail`, not an `error` — a reachable host with a broken builder is a contract violation.

## Out of scope

- Worker-vantage build-host checks (the split-deployment refinement; ADR-0163 backlog).
- Reachability probing of SSH build hosts (ADR-0103 already flips their state via the reconciler).
- Un-gating `guest_egress` (its deployment-wide probe-guest seam is still unwired).
