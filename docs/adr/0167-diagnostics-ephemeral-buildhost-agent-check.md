# ADR 0167 — Diagnostics: ephemeral-libvirt build-host guest-agent reachability check

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0091](0091-doctor-diagnostics-model.md) (the
  `Check`/three-state model, the server-vs-worker vantage, and the mutating-probe machinery —
  opt-in, reaper-visible markers, single-flight, TTL — that `guest_egress` established),
  [ADR-0100](0100-ephemeral-libvirt-build-vm.md) (the `EphemeralBuildVm.session()` builder this
  check provisions through, and the `kdive-build-<run_id>` reaper it teaches one new live-holder
  signal), [ADR-0163](0163-diagnostics-local-kernel-src-check.md) (the build-host preflight split
  whose deferred ephemeral-probe and `enabled`-gate this ADR implements),
  [ADR-0164](0164-diagnostics-worker-vantage-dispatch.md) (which already plumbed the async pool
  into `default_service_factory`).
- **Spec:** [`../specs/2026-06-18-diagnostics-ephemeral-buildhost-agent.md`](../specs/2026-06-18-diagnostics-ephemeral-buildhost-agent.md)
- **Issue:** [#544](https://github.com/randomparity/kdive/issues/544) (split from
  [#533](https://github.com/randomparity/kdive/issues/533); covers
  [#531](https://github.com/randomparity/kdive/issues/531))

## Context

`ops.diagnostics` reported a healthy environment while every build deterministically failed
(#533). ADR-0163 shipped the cheap, read-only half — `local_kernel_src`, a server-vantage config
read for the seeded `worker-local` `LOCAL` host. It explicitly deferred two pieces, both of which
this ADR delivers:

1. **The `ephemeral_libvirt` guest-agent reachability probe (#531).** Each `ephemeral_libvirt`
   build host provisions a throwaway builder VM per build; if its operator-staged base build image
   boots but the `qemu-guest-agent` never connects, every build routed to that host fails
   deterministically at `wait_for_agent` — a class of failure `doctor` could not see, because no
   check touched a build host's runtime.
2. **The `enabled`-flag gate on `local_kernel_src`.** ADR-0163 made that check always-on and
   flagged the resulting exit-code regression: a git/SSH/ephemeral-only deployment that never sets
   `KDIVE_KERNEL_SRC` (a supported, healthy configuration) still carries the seeded `worker-local`
   row, so on upgrade its `doctor` exit flips `0 → 1`. ADR-0163 deferred the precise fix —
   suppress the check when the operator has disabled the seeded host — to "once the factory has
   pool access".

Both were deferred behind "the factory needs an async DB pool". Since ADR-0163, **ADR-0164 already
plumbed `pool` into `default_service_factory`** (for worker-vantage job dispatch), so that
prerequisite is met. The factory itself stays synchronous: a check that needs the DB **defers the
read to probe time** (the `reachability.py` rationale ADR-0163 cites), so no check needs the
factory to `await`.

Unlike `local_kernel_src` (a config read plus one `stat`), this probe **provisions cost-bearing
infrastructure on every run** — the same mutating shape as `guest_egress`. ADR-0091 gates that
shape behind four guards, all of which apply here.

## Decision

Add a **server-vantage** check, `ephemeral_libvirt_buildhost_agent`, assembled into the diagnostics
service only under a new opt-in, and DB-`enabled`-gate the existing `local_kernel_src` check.

### The check

`EphemeralLibvirtBuildHostAgentCheck` enumerates the `kind='ephemeral_libvirt' AND enabled=true`
build hosts (`db.build_hosts.list_all_hosts`, filtered) at **probe time** and, for each, provisions
a throwaway builder through `EphemeralBuildVm.session(..., wait_network=False)`, waits for its guest
agent (`wait_for_agent`, reused inside `session`), runs one trivial in-guest command, and tears the
domain + overlay down in the session's `finally`. It aggregates the per-host outcomes into **one**
three-state `CheckResult` (the framework runs `Check.run()` once → one result; `secret_ref` is the
precedent for aggregating many sub-probes into one verdict):

| Per-host observation | Outcome | Aggregate effect |
|---|---|---|
| agent connected + trivial command `rc 0` | `AGENT_READY` | contributes to `pass` |
| builder started but the guest agent never connected (`PROVISIONING_FAILURE` from `session`), or the agent connected but the trivial command returned `rc != 0` or dropped mid-exec | `AGENT_UNREACHABLE` | forces `fail` |
| host/config could not be reached **before** the agent connected (`CONFIGURATION_ERROR` / `TRANSPORT_FAILURE` / `INFRASTRUCTURE_FAILURE`, or no staged base image, or a probe already in flight) | `HOST_UNREACHABLE` | `error` unless a `fail` dominates |

The adapter's agent-vs-host discriminator is **whether `wait_for_agent` returned**: a failure at or
after it is `AGENT_UNREACHABLE` (a reachable host with a broken builder is a contract violation, not
an indeterminate run); a failure before it is `HOST_UNREACHABLE`.

Aggregation precedence (mirrors `secret_ref`'s "any unresolved → fail; backend down → error"):

- **`fail`** — any host is `AGENT_UNREACHABLE`. The verdict names the failing host(s) and carries
  `failure_category=configuration_error` and `BUILDHOST_AGENT_FIX` (rebuild/repair the
  operator-staged base build image so its guest agent starts). A build routed to that host fails
  deterministically, so this is a real contract violation.
- **`error`** — no host failed, but at least one host was `HOST_UNREACHABLE`, **or** no
  `ephemeral_libvirt` host is registered at all. An indeterminate or absent target is never a
  confident `fail` (emitting "your guest agent is broken" when the host was simply unreachable is
  the confident-wrong-fix failure ADR-0091 forbids), and never a silent `pass` (a host we could not
  probe must not read as healthy).
- **`pass`** — every probed host is `AGENT_READY`.

`wait_network=False` (a new keyword on `EphemeralBuildVm.session`, default `True` preserving the
BUILD path) scopes the probe to **agent reachability**: a builder whose agent connects but whose
network never comes up is a different fault than "the agent never connected", so the probe does not
wait for the network and cannot misreport a network timeout as `AGENT_UNREACHABLE`. The egress
preflight is already skipped (the probe passes `source=None`). The trivial command needs no network.

### The four mutating-probe guards (ADR-0091), applied

- **Opt-in.** Assembled only under a new, dedicated `with_buildhost_agent` flag
  (`doctor --with-buildhost-agent`, `ops.diagnostics(with_buildhost_agent=…)`), independent of
  `--with-egress`. The two cost-bearing probes are unrelated (one execs from a workload guest on the
  runtime provider bridge; one provisions a builder on a build host), so they are independently
  opted in and **independently audited** — the provisioning action records under its own
  `ops.diagnostics.buildhost_agent` audit event, so it cannot be amplified under cover of "just
  running doctor".
- **Reaper-owned cleanup, not assumed.** The builder is a real `kdive-build-<run_id>` domain
  (ADR-0100), already owned by the reconciler's `reap_orphan_build_vms` sweep. That sweep reaps a
  build VM whose owning **BUILD job** is terminal/gone — and a `doctor` probe has no BUILD job, so
  **without intervention it would reap the probe mid-check**. The probe therefore registers a
  reaper-visible marker (`buildhost_agent_probe_guests`: `run_id`, `heartbeat_at`, `ttl_deadline`)
  before it provisions and advances the heartbeat for the probe's whole duration; `reap_orphan_build_vms`
  gains one new live-holder clause — a build VM whose `run_id` has a **fresh** probe heartbeat is
  live and is not reaped. The staleness/TTL predicate evaluates `now()` **in Postgres**, matching
  `provider_reaping`'s clock-in-DB convention. When the `doctor` process dies mid-probe, the
  heartbeat goes stale and the **existing** sweep reaps the leaked builder (no BUILD job, no fresh
  heartbeat); the `ttl_deadline` is the hard backstop. No new reconciler sweep is needed — the build
  VM already has a reaper; this teaches it one signal. Because `session()` is a synchronous
  (uncancellable-thread) contextmanager run via `asyncio.to_thread`, the heartbeat-cancel and
  marker-release live in the probe **coroutine's** `finally`, so a `run_check` timeout that cancels
  the coroutine still stops the heartbeat and frees the marker (or leaves it to TTL).
- **Single-flight per host.** Concurrent `doctor` runs do not each spin a builder on the same host:
  a **module-level** per-host `SingleFlight` (reused from `egress_probe`) shares one in-flight probe,
  backstopped by the DB partial-unique index on `build_host_id` (live rows only). It must be a
  process-level singleton, not built per factory call — `default_service_factory` runs fresh on every
  `ops.diagnostics` call, so a per-assembly coalescer coalesces nothing (`egress_probe`'s own
  `SingleFlight` docstring calls this out). A second *process* that cannot share the coalescer hits
  the index and reports `error` ("a probe is already in flight for this host"), still exactly one
  builder. The probe takes **no** build-host lease (it is not a BUILD job), so it does not count
  against `max_concurrent`: an operator-initiated probe may transiently over-subscribe a saturated
  host by one builder, which is accepted (single-flighted, transient, operator-initiated).
- **Operator-staged base build image required, fail honestly otherwise.** A builder needs the
  operator-staged base image volume (the M2.4 constraint, ADR-0080). It is a per-host DB column
  (`build_hosts.base_image_volume`), so — unlike `guest_egress`, whose probe-guest seam is missing
  deployment-wide and is refused at factory assembly — this seam exists per host. A host with no
  staged `base_image_volume` cannot be probed and contributes `HOST_UNREACHABLE` (→ `error`),
  never a silent drop. `with_buildhost_agent` against a deployment with **zero** `ephemeral_libvirt`
  hosts is also `error`, not a vacuous `pass`.

### `enabled`-gating `local_kernel_src`

`LocalKernelSrcCheck` gains an injected, **deferred** `enabled` probe (a `Callable[[], Awaitable[bool]]`,
defaulting to always-enabled so existing unit tests and a pool-free assembly keep current behavior).
The production probe reads the seeded `worker-local` host's `enabled` flag via the pool at check time.
When the seeded host is **disabled**, the check returns `pass` with an "n/a — local build host
disabled" detail (the three-state model has no "skip"; a disabled lane has no contract to violate, so
`pass` is the honest verdict and clears the `0 → 1` exit regression). When it is enabled (the default),
the existing warm-tree verdict is unchanged. A DB read failure while resolving the flag **fails open
to enabled** — a transient blip must surface the latent local-lane failure, never hide it.

### Layering

- The marker table access (`register` / `heartbeat` / `release` / `is_probe_live`) lives in a new
  `db/buildhost_agent_probes.py` repository, the natural home for table I/O. Both consumers sit
  above it: the diagnostics check (which owns the heartbeat loop, single-flight, and three-state
  policy, mirroring `GuestEgressCheck`) and the reconciler's `reap_orphan_build_vms` (which imports
  only the `is_probe_live` predicate). This is a deliberate divergence from `egress_probe`, which
  keeps its raw SQL inside `diagnostics/`; routing a *second* probe table's SQL through the db layer
  keeps both the diagnostics check and the reconciler repair free of raw SQL for it.
- The production probe adapter (`diagnostics/buildhost_agent.py`) is the only place that imports
  `EphemeralBuildVm` (`diagnostics → providers`, the legal direction). `checks.py` holds the
  outcome enum + three-state policy and is unit-tested without libvirt or a DB, via injected probes
  — the same seam shape as every other check.
- `BUILDHOST_AGENT_FIX` is owned by the diagnostics layer (diagnostic-output policy), mirroring
  `LOCAL_KERNEL_SRC_FIX` and `BASE_VOLUME_NOT_STAGED_FIX`.

## Consequences

- A deployment whose `ephemeral_libvirt` builder boots but never connects its guest agent gets a
  `fail` (naming the host, with the repair-the-base-image fix) from `doctor --with-buildhost-agent`
  **before** a build is attempted — the #531 acceptance criterion. Run without the flag, `doctor` is
  unchanged: the cheap read-only checks only.
- `local_kernel_src` no longer fails a git/SSH/ephemeral-only deployment that disabled the seeded
  local host — the ADR-0163 exit-code regression is closed. A deployment that leaves the seeded host
  enabled sees the unchanged warm-tree verdict.
- The build-VM reaper gains one live-holder signal (`run_id` with a fresh probe heartbeat). A leaked
  probe builder is still reaped by the existing sweep once its heartbeat goes stale, with the
  `ttl_deadline` as the hard backstop; no new sweep is added.
- One new additive migration (`buildhost_agent_probe_guests`, forward-only per ADR-0015). No DDL on
  existing tables. The new opt-in adds one CLI flag, one MCP boolean parameter, and one audit event;
  the `ops.diagnostics` verdict shape is unchanged (the new check flows through the generic item
  projection).
- The probe is the **first mutating diagnostic check that actually assembles** (`guest_egress` is
  refused at factory time pending its deployment-wide seam). So the service, when a mutating check is
  present, runs under **generous per-check and overall timeouts** sized above the builder's own
  `wait_for_agent` bound — the cheap checks are unaffected (a larger ceiling is an upper bound, not a
  delay). Without `with_buildhost_agent`, the default tight timeouts are unchanged.

## Considered & rejected

- **Fold the probe under the existing `--with-egress` flag.** One flag for "all mutating probes" is
  a smaller surface, but it couples two unrelated cost-bearing probes and erases the distinct-audit
  property (a build-host provision recorded under `ops.diagnostics.egress` is a misattribution). A
  dedicated flag keeps each probe independently opt-in and independently audited.
- **A new reconciler sweep + a distinct probe-domain prefix (mirror `guest_egress` exactly).** The
  egress probe uses a `kdive-egress-probe-` prefix and its own `repair_leaked_probe_guests` sweep
  because its guest is not otherwise owned. The build-host probe's guest **is** a `kdive-build-<run_id>`
  domain the `reap_orphan_build_vms` sweep already owns; adding a parallel sweep + a second naming
  scheme would duplicate reaping for a domain that already has a reaper. Teaching the existing sweep
  one heartbeat clause is less code and keeps a single owner for build-VM reaping.
- **One `Check` instance per host (assembled in the factory).** Per-host checks would need the
  factory to enumerate hosts, i.e. an async factory — the ripple ADR-0164's pool plumbing was
  careful to avoid (the `ServiceFactory` protocol, the tool's synchronous `service_factory(...)`
  call, the worker handler). One aggregating check that enumerates at probe time keeps the factory
  synchronous, exactly as `reachability` defers its config read.
- **Make `default_service_factory` async to enumerate hosts / read the `enabled` flag at assembly.**
  Rejected for the same ripple. Both needs are met by deferring the DB read into an injected probe
  the check awaits at run time.
- **Wait for the guest network before the trivial command (reuse `session()` unchanged).** The probe
  asserts *agent* reachability; a network timeout (`PROVISIONING_FAILURE`) would then be
  indistinguishable from an agent timeout and misreport as `AGENT_UNREACHABLE`. `wait_network=False`
  removes that confound and makes the probe faster; the BUILD path keeps the default `True`.
- **Return `error` (not `pass`) when the seeded local host is disabled.** `error` reads as "the
  check could not run", which is false — it ran and found the lane disabled. `pass` with an explicit
  "n/a" detail is the honest three-state encoding of "no contract to violate" and is what clears the
  exit-code regression.
