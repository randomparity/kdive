# ADR 0362 — Architecture/accelerator as a differential cost + placement factor

- **Status:** Accepted
- **Date:** 2026-07-15
- **Amends:** [ADR-0007](0007-metering-budgets-admission.md) (the kcu cost model —
  this ADR adds an accelerator factor to the rate formula)
- **Composes with:** [ADR-0338](0338-guest-arches-discovery.md) (a host's advertised
  `guest_arches`), [ADR-0339](0339-admission-arch-validation-accel-persist.md) (the
  resolved `accel` persisted on the System), [ADR-0112](0112-systems-inventory-config.md)
  and [ADR-0186](0186-pool-selection-axis.md) (placement candidate resolution)
- **Issue:** #1176

## Context

ADR-0007 pins the kcu cost model: `rate = coeff(cost_class) × (W_CPU × vcpus +
W_MEM × memory_gb)`. The coefficient comes only from a manually-assigned per-Resource
`cost_class` label. The model is **architecture-blind**: a `ppc64le` guest emulated
with TCG on an x86_64 host and a native `x86_64` KVM guest of the same size price
identically, even though the emulated guest consumes several times the host compute
per guest-vcpu-hour. ADR-0339 persists the resolved accelerator (`kvm`/`tcg`) on the
System and its migration comment names "cost accounting" as a downstream consumer,
but nothing priced ever reads it.

Placement (`placement.py`) filters candidates by kind, pool, PCIe, project affinity,
and status — never by architecture. In a multi-host, multi-arch fleet there is no way
to route a `ppc64le` request to a host that can boot `ppc64le`, nor to price the
architecture an agent is choosing before it commits.

Size differentiation (charge more for a larger VM) already works through the
`W_CPU × vcpus + W_MEM × memory_gb` term and is out of scope here.

## Decision

### 1. An accelerator factor multiplies the kcu rate

The rate formula gains a pinned, fleet-uniform accelerator factor:

```
rate(kcu/hr) = coeff(cost_class) × A(accel) × (W_CPU × vcpus + W_MEM × memory_gb)
```

- **`A(accel)` is a global reference weight**, pinned here exactly like `W_CPU` /
  `W_MEM` — not per-host, not operator-tunable per resource, because it reflects a
  property of the accelerator *technology* (native execution vs. dynamic binary
  translation), uniform across the fleet. `A(kvm) = 1.0` (the native baseline);
  `A(tcg) = 4.0` (a full-emulation guest reserves host compute at a large multiple of
  native — 4× mirrors the ADR-0007 "one vcpu-hour ≈ four GB-hours" reference scale).
  The absolute value is a reference scale; what is load-bearing is that it is fixed,
  documented, and applied identically at estimate, reserve, and reconcile.
- **`A(accel)` fails open to `1.0`** for a `None`/unknown accelerator. A resource that
  advertises no `guest_arches` (remote-libvirt, fault-inject, a host not re-discovered
  since ADR-0338) resolves no accel and is priced at the native baseline — byte-identical
  to pre-ADR behavior, so no existing allocation regresses. This matches the ADR-0339
  "NULL accel = pre-ADR behavior" persisted semantics.

No schema change: the factor is a pure lookup off the already-persisted `accel`
(ADR-0339's `systems.accel`) and the already-advertised `guest_arches` (ADR-0338).
There is no per-arch cost-class row and no new priced column.

### 2. The factor is applied at every priced point that knows the accel

- **Estimate** (`accounting.estimate`): the read-only tool accepts an optional `accel`
  (`kvm`/`tcg`, unknown → `configuration_error`) so an agent can price each architecture
  it is choosing between and compare. The response surfaces the priced `accel` and its
  factor. This is the "adequate information for an agent to choose" the issue asks for.
- **Reserve** (allocation admission): the allocation request gains an optional `arch`.
  When set and the chosen Resource advertises `guest_arches`, admission resolves the
  accelerator (the ADR-0339 `resolve_accel` branch) and prices the reserved estimate
  with `A(accel)`. Unset/no-`guest_arches` → native baseline (today's behavior).
- **Reconcile** (ledger actual): `_actual_cost` reads the System's persisted `accel`
  for the allocation and applies `A(accel)`, so the recorded bill is architecture-true.

Reserve and actual use the same factor keyed off the same accel resolution, so a TCG
guest reserves and reconciles consistently rather than under-reserving. The allocation
`arch` is an advisory placement + pricing hint; the System's provisioned arch (and thus
the reconciled accel) remains the billing source of truth, the same reserve/actual
divergence tolerance ADR-0007 already accepts when a coefficient changes mid-lease.

### 3. Placement is architecture-aware

`PlacementRequest` gains an optional `arch`. A candidate is admitted only if it can boot
that arch: a resource that advertises `guest_arches` is kept only when the set contains
`arch`; a resource that advertises **no** `guest_arches` is kept (fail-open — it cannot
prove it does not support the arch, preserving remote-libvirt / fault-inject behavior).
The predicate lives beside the affinity predicate (`affinity.py`) and gates the by-id,
by-pool, and by-kind lanes, so a `ppc64le` request routes to a `ppc64le`-capable host and
falls through hosts that only advertise `x86_64`.

## Consequences

- A TCG guest costs 4× a same-size KVM guest; an agent sees the difference before it
  commits (estimate) and is billed it (reserve + reconcile).
- A multi-arch fleet routes each request to a host that can boot the requested arch.
- No migration, no new cost-class rows, no cost-model branch per provider — the factor
  is one reference weight and one lookup.
- The accel factor is fleet-uniform, not per-resource; an operator who needs per-host
  arch pricing would extend `cost_class`, not this factor. Deferred until a real need
  exists (no speculative per-host arch table).
