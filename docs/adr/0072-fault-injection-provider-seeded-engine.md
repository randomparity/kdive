# ADR 0072 — Fault-injection provider + seeded decision-keyed fault engine (M1.5)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0071](0071-per-kind-provider-runtime-registry.md)
  (the registry this provider registers into), [ADR-0063](0063-typed-provider-runtime.md)
  (the typed ports it satisfies), [ADR-0021](0021-reconciler-loop-drift-repair.md) (the
  reconciler passes this provider exists to trigger),
  [ADR-0036](0036-reservation-lease-semantics.md) (the lease window a slow provision races),
  the PoC error taxonomy carried in [`../specs/top-level-design.md`](../specs/top-level-design.md)
  §Error taxonomy (the `ErrorCategory` values faults map to).
- **Spec:** [`../specs/m1.5-fault-injection-provider.md`](../specs/m1.5-fault-injection-provider.md)

## Context

The reconciler already has every drift-repair pass M1.5 wants to stress — orphaned System,
abandoned job, dead DebugSession, leaked provider infra, lease-expiry-mid-job (ADR-0021,
`reconciler/loop.py`). Cancel/compensation policy is declared per worker op (top-level
design §Reconciliation & teardown: "`jobs.cancel` on a half-done `provision`/`install` is
never undefined"). What is missing is a provider that can **reliably and reproducibly
trigger** each of these on demand. The real local-libvirt provider fails only when real
infrastructure fails — which is rare, slow, and non-deterministic, so the repair paths are
exercised by hand-built fakes in unit tests but never by a provider running the **whole**
spine (admission → job → worker → reconciler) end to end.

M1.5 fills that gap with a mock provider behind the real plane interfaces (ADR-0071). The
open design question is the **control model** for *when* it injects latency and failure.
The milestone's stated method is **seeded probabilistic chaos**: any plane may fail at a
configured rate, broad enough to surface drift a hand-written script would not think to
provoke. But M1.5's value comes from **assertable** tests — "this fault produced exactly
this repair" — and a naive probabilistic mock is unassertable in two ways:

1. **Wall-clock / `os.urandom` seeding** makes every run draw differently, so no test can
   pin a fault.
2. **A shared mutable PRNG stream** (`rng.random()` per call) is order-dependent: under
   concurrent workers the draw a given plane call receives depends on which worker reached
   the PRNG first, so even a *fixed seed* yields different per-call outcomes across runs.

So "seeded" is load-bearing only if the draw is reproducible **independent of concurrency
and call order**.

## Decision

We will ship a **fault-injection provider** satisfying every typed port with
synthetic-but-plausible outputs, driven by a **seeded, decision-keyed fault engine** whose
every decision is a **pure function of stable inputs**.

**Decision-keyed draw (the load-bearing rule).** A fault decision is

```
fault_for(seed, system_id, plane, attempt) -> {latency_s, failure: ErrorCategory | None}
```

computed by hashing `(seed, system_id, plane, attempt)` to a stable `[0,1)` draw and
comparing against the resource's `fault_rate` — **not** by advancing a shared PRNG stream.
Because the decision for "`connect`, attempt 2, system X" is a deterministic function of
stable inputs, it is **identical every run regardless of worker concurrency or call order**.
A CI test pins a `seed` known to fail a chosen plane; a soak run sweeps `seed` values to
widen coverage. The `seed` and `fault_rate` are **configured on the fault-inject resource's
`capabilities` jsonb** by discovery — **never** read from wall-clock or `os.urandom`, or
reproducibility is lost (a unit test asserts no nondeterministic seeding source is reachable).

**Per-plane fault catalog → existing `ErrorCategory`.** Each plane injects faults from the
**existing** taxonomy (the spec forbids inventing strings):

| Plane | Latency | Failure category |
|---|---|---|
| `provision` | configurable delay | `PROVISIONING_FAILURE` |
| `install` | delay | `INSTALL_FAILURE` / `BOOT_TIMEOUT` |
| `boot` | delay | `READINESS_FAILURE` / `BOOT_TIMEOUT` |
| `connect` | delay | `TRANSPORT_FAILURE` (e.g. drop on attempt N) |
| `control` | delay | `CONTROL_FAILURE` |
| `retrieve` | delay | `INFRASTRUCTURE_FAILURE` |

**Latency is the reconciler/cancel lever.** A configurable provision/install delay is what
lets a **short lease** expire *mid-job* (ADR-0036 lease-expiry → `failed(lease_expired)`),
what lets `jobs.cancel` land *mid-op* deterministically (ADR-0072 §cancel below), and what
keeps allocations active long enough for the admission-race tests to contend a real
resource. So "slow provision" is not a separate fault — it is the same engine emitting a
latency with no failure.

**Synthetic outputs are plausible, not real.** The mock returns a synthetic domain name
from `provision`, a loopback `TransportHandle` from `connect`, and a synthetic vmcore
artifact from `retrieve`, so the **happy path** (no drawn fault) drives the full spine to a
real terminal state. It owns a **mock infra-inventory seam** (`list_owned`/`destroy`, the
`InfraReaper` shape) so the reconciler's *leaked-domain* pass has synthetic infra to find
and reap.

**Each op declares its cancel/compensation policy.** Every fault-inject op states in code
whether a `jobs.cancel` mid-flight yields **clean-rollback**, **best-effort**, or
**orphan-flagged** state — so "cancel is never undefined" is tested against a provider that
can be made to pause mid-op (via injected latency) on demand, not merely asserted in prose.

## Consequences

- The full spine (admission → job → worker → reconciler → teardown) runs against a provider
  that can be driven to **every** drift state on demand, with each fault pinned by seed — so
  the M1.5 validation issues (reconciler repair, admission races, cancel/compensation) are
  **deterministic** CI tests, not flaky soak runs.
- **A real reconciler/admission bug surfaced by the engine is a finding, not a test fixture
  failure** — surfaced now, on a mock, before M2 makes the same bug a remote-provider
  incident. This is the milestone's purpose.
- **New obligation: the engine must have no nondeterministic seeding path.** A reachable
  `os.urandom`/wall-clock seed would silently break every downstream assertion; an explicit
  test guards that the only seed source is resource config.
- The fault-inject resource's `capabilities` jsonb carries `seed`, `fault_rate`, and the
  `secret_ref` (ADR-0073) — **no new DDL** beyond migration `0018`'s CHECK widen (ADR-0071);
  these are jsonb keys like the existing `vcpus`/`concurrent_allocation_cap`.
- Soak coverage (sweep seeds) and CI assertion (pin a seed) are the **same** engine at two
  `fault_rate`/seed settings — no second code path, so the chaos breadth the milestone
  wanted and the determinism its tests need are not in tension.

## Alternatives considered

- **Deterministic per-plane directive script** (an immutable `{plane: fault}` map in the
  profile). Maximally assertable and the simplest to reason about, but it only ever fails
  the planes an author *thought to script* — it cannot surface the drift a probabilistic
  sweep finds, which is the stated reason M1.5 chose chaos. Rejected in favor of the seeded
  engine, which **subsumes** it: pinning a seed at `fault_rate=1.0` for one plane *is* a
  directive, so the assertable case is a configuration of the chosen model, not a different
  one.
- **Shared mutable PRNG stream** (`random.Random(seed)`, one `.random()` per call). The
  obvious reading of "seeded," but order-dependent under concurrency — the same seed yields
  different per-call outcomes across runs, so tests flake. Rejected: decision-keying gives
  identical reproducibility without serializing the workers.
- **Operator-mutable fault-policy table** (a DB table + `faults.set` tool). Lets faults flip
  on a live resource without reprovisioning, but adds a table, migration, tool, and RBAC
  surface, and makes a test set-then-trigger (racier to assert). Rejected: `seed`/`fault_rate`
  in `capabilities` needs no new durable surface and the decision-keyed draw already lets a
  test target any plane/attempt.
