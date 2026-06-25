# ADR 0244 — Per-Run vmcore capture: one core per crashing Run on a reused System

- **Status:** Accepted
- **Date:** 2026-06-25
- **Deciders:** kdive maintainers
- **Issue:** [#796](https://github.com/randomparity/kdive/issues/796) (split from the #781 design
  discussion; epic [#764](https://github.com/randomparity/kdive/issues/764))
- **Spec:** [`../superpowers/specs/2026-06-25-per-run-vmcore-capture-796.md`](../superpowers/specs/2026-06-25-per-run-vmcore-capture-796.md)
- **Supersedes:** [ADR-0050](0050-vmcore-method-aware-storage.md) — refines its multiplicity scope
  from **per System** to **per Run**; keeps the method-aware storage and the
  first-method-wins-then-reject contract intact, now Run-scoped.
- **Builds on (does not supersede):** [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md)
  (the `Retriever.capture` port and the retrieve plane), [ADR-0049](0049-crash-capture-tiers.md)
  (the capture-method vocabulary and the method-aware admission dedup key), [ADR-0016](0016-repository-layer-locks-idempotency.md)
  (the job `dedup_key` ledger + advisory locks), [ADR-0193](0193-uniform-mutation-idempotency.md)
  (the `idempotency_keys` envelope replay — the `vmcore.fetch` kind is unchanged),
  [ADR-0243](0243-owner-fetchable-vmcore-vmlinux.md) (#781 egress, which named this change as its
  forward-compatible successor).

## Context

The raw `vmcore` is stored **per System**: keyed `…/systems/{system_id}/vmcore-{method}` with a
single core per System (ADR-0050 first-method-wins). The capture handler (`jobs/handlers/vmcore.py`)
enforces this under a per-System advisory lock — a second capture on the same System is *refused*
(a same-method re-dispatch returns the existing key; a different method raises
`configuration_error`), never overwritten. There is no data-loss bug today.

But a System hosts many Runs over its life (`System ──< Run`), and the core is keyed and owned by
the **System**, not the **Run** that actually crashed. The capture job has no `run_id` at all:
`vmcore.fetch(system_id, method)` is **System-addressed** and `CaptureVmcorePayload` carries
`system_id + method` and **no `run_id`**, so the core cannot be attributed to the crashing Run.
This is the indirection ADR-0243's egress had to carry (`run.system_id → raw_vmcore_key(system_id)`,
with a forward-compat note that it would "swap to addressing by `run_id` directly" once per-Run
capture landed).

**Reachability of the multi-core case.** The issue frames this as "a reused System that crashes
under a later Run loses the second core." Under the *current* System state machine that case is not
yet reachable: `CRASHED` transitions only to `TORN_DOWN`/`FAILED` (`domain/capacity/state.py`), with
no edge back to `READY`/`REPROVISIONING`, so a given `system_id` reaches `CRASHED` — and produces a
vmcore — **at most once** in its lifetime. So per-System keying does not actually drop a core today.
The reachable, concrete value of this change is therefore (1) **correct Run attribution** of the one
core, which lets the #781 egress resolve it by `run_id` with no System indirection, and (2)
**forward-compatibility**: if a later ADR makes a `CRASHED` System reprovisionable (so one System
can crash under successive Runs), each crashing Run already retains its own core with no further
change to the capture/egress contract. Making cores per-Run is not a key rename — it requires
designing the Run association, re-owning the artifact, and moving the capture concurrency boundary
from the System to the Run.

ADR-0243 (#781) made the raw core owner-fetchable and explicitly deferred this, resolving the
`vmcore` egress through `run.system_id → raw_vmcore_key(system_id)` with a note that the resolver
would "swap to addressing by `run_id` directly" when per-Run capture landed. This is that change.

## Decision

Capture is **Run-addressed** and cores are **Run-owned**. One core per crashing Run: the captured
core is owned by the Run that crashed, and if a System ever crashes under successive Runs (see
"Reachability" above) each Run retains its own distinct, independently-fetchable core.

### 1. `vmcore.fetch` is Run-addressed

The tool becomes `vmcore.fetch(run_id, method)`, **replacing** `system_id` (greenfield, pre-1.0;
no deprecation shim). The handler resolves the Run, derives its bound System
(`run.require_system_id()`), and applies the unchanged precondition: the System must be `CRASHED`.
The caller names the Run that crashed — it already knows which Run it booted — so no fragile
"which Run is currently booted on this System" inference is needed. `contributor` on the Run's
project is still required.

### 2. Cores are Run-owned

The raw and redacted objects move to `owner_kind='runs'`, `owner_id={run_id}`, names
`vmcore-{method}` and `vmcore-{method}-redacted` — key `…/runs/{run_id}/vmcore-{method}`. This is
the same `owner_kind='runs'` shape the external-build `vmlinux` already uses, so **no migration and
no new artifact metadata**: the `artifacts` row's existing owner columns carry the association.
`raw_vmcore_key` resolves a Run's core by `owner_kind='runs' AND owner_id={run_id}` (still excluding
the `-redacted` sibling). The capturing method stays encoded in the key suffix (ADR-0050 Decision 2,
unchanged).

### 3. One core per Run; first-method-wins per Run

ADR-0050's contract is preserved, scoped to the Run instead of the System: a Run holds at most one
raw core; a same-method re-dispatch returns it (idempotent); a different-method dispatch raises
`configuration_error` naming both methods. The two raw-core readers (`postmortem.*`,
`introspect.from_vmcore`) stay **method-blind and single-core** — they resolve *the* one core for a
Run — so no method-selection rule and no new reader argument is introduced. Per-method multiplicity
within one Run is rejected for the same reason ADR-0050 rejected it per System (see below). A
distinct core per *crash* is achieved because each crash belongs to a distinct Run (a Run is one
build→boot lifecycle; a re-crash on a reused System is a new Run).

### 4. The idempotency / concurrency boundary moves to the Run

- **Admission dedup key:** `{run_id}:capture_vmcore:{method}` (was `{system_id}:…`). Distinct Runs
  enqueue distinct capture jobs; a same-Run, same-method retry replays the one job (ADR-0016).
- **Advisory lock:** `precheck`/`finalize` serialize on `LockScope.RUN` (was `LockScope.SYSTEM`).
  The first-method-wins re-check is placed in both `precheck` (before the slow `capture()` seam, so
  the common case writes no object) and `finalize` (the race backstop), exactly as ADR-0050 §3, now
  under the per-Run lock.
- **Envelope replay:** the `vmcore.fetch` idempotency-store kind (ADR-0193) is unchanged — still the
  tool name; a keyed retry replays the identical job envelope.

**Load-bearing concurrency invariant.** A given `system_id` reaches `CRASHED` **at most once** in
its lifetime: `CRASHED` transitions only to `TORN_DOWN`/`FAILED` (`domain/capacity/state.py`), with
no edge back to a bootable state. So there is never a second capture against the same live domain —
the only real concurrency is same-Run `vmcore.fetch` races (a client retrying), which the per-Run
lock + dedup serialize. This is why the lock moves wholesale from System to Run rather than
co-holding both: the System scope is redundant for capture. (Were a future ADR to make a `CRASHED`
System reprovisionable, successive crashes would still be **sequential** — separated by the
reprovision that returns the System to `READY` — so the per-Run boundary remains correct; if
concurrent live captures of one domain ever became possible, re-adding the System lock as the outer
scope is the documented next step, §"Considered & rejected".)

### 5. The `Retriever.capture` port gains `run_id`

`capture(system_id, run_id, method)`. `system_id` still locates the **live resource** — the libvirt
domain (`domain_name_for(system_id)`), the local overlay, the remote dump volume — so the capture
*mechanics* are untouched. `run_id` sets the **artifact owner/key**. All three providers
(`local-libvirt`, `remote-libvirt`, `fault-inject`) write `owner_kind='runs'`, `owner_id={run_id}`.

### 6. The #781 egress resolves by `run_id` directly

`artifacts.fetch_raw`'s `vmcore` branch resolves `raw_vmcore_key(run_id)` and gates on **the Run's
project** — the Run owns the core, so the asset's project is unambiguously `run.project`. ADR-0243
deliberately re-checked the *System's* project instead, calling `run.project == system.project` an
invariant it would "not assume". That invariant is in fact **enforced**: `runs.create` rejects a
System whose project differs from the Run's investigation project (`services/runs/admission.py`,
`system.project != inv.project`), and `runs.bind` rejects `system.project != run.project`
(`services/runs/bind.py`). A bound Run therefore always shares its System's project, so gating on
`run.project` preserves exactly the cross-project isolation ADR-0243's System-project re-check
provided, and drops the now-redundant `run.system_id → system_project → raw_vmcore_key(system_id)`
indirection. The closed `RawAsset` allow-list, the URL-only contract, and the audit are unchanged.

### 7. Payload, attribution, audit

`CaptureVmcorePayload` moves off `SystemPayload` to `RunPayload` (`run_id` + `method`); the worker
resolves `system_id` from the Run. `CAPTURE_VMCORE` is registered run-bearing
(`run_id_from_payload`), so the reconciler attributes a stuck capture job to its Run. The capture
audit event records `object_kind='runs'`, `object_id={run_id}`.

## Consequences

- The captured core is attributed to the Run that crashed and fetchable by `run_id`. If a System
  ever crashes under successive Runs (a future state-machine capability — see "Reachability"), each
  Run retains its own core with no further contract change; today, where a `system_id` crashes at
  most once, the concrete win is correct Run attribution.
- The #781 egress is simpler: one Run-keyed lookup, gated on `run.project`, with no System
  indirection. `raw_fetch`'s `system_project`/`run.system_id` use for `vmcore` is removed.
- **No migration.** `owner_kind='runs'` is an existing artifact shape; M0/M1 carries no persisted
  production cores (same one-time format-shift reasoning as ADR-0050), so no backfill.
- The generated agent-facing tool reference for `vmcore.fetch` changes (its first argument is now
  `run_id`); it is regenerated in lockstep.
- Within a single Run the model is still one core (first-method-wins), so the readers stay
  single-core and method-blind — no surface growth.
- **Object-store orphan on the finalize race backstop** persists unchanged in shape (ADR-0050
  Consequences): a `finalize` reject after `capture()` wrote the loser's object leaves it
  unreferenced, reaped by the `retention_class="vmcore"` sweep. The trigger is now per-Run rather
  than per-System; no new kind of leak.
- The `vmcore.fetch` precondition error surface gains the Run-resolution failures (`not_found` for
  an absent/cross-project Run, `configuration_error` for a Run not bound to a System) on top of the
  existing System-state checks.

## Considered & rejected

- **Keep `system_id`; auto-resolve the System's currently-booted Run.** Rejected: needs a "current
  Run on System" query that is ambiguous when zero or many Runs are bound, and is fragile across
  reprovision boundaries. The caller already knows which Run crashed, and Run-addressing is what the
  #781 egress consumes.
- **System-owned key with `run_id` embedded in the name (`vmcore-{run_id}-{method}`).** Rejected: it
  keeps the per-System owner indirection #796 set out to drop, needs extra key parsing on every
  read, and divorces the artifact's `owner_id` from its logical owner (the Run), so the egress could
  not address it by `run_id` without re-deriving the System.
- **Per-method multiplicity within one Run** (store `host_dump` and `kdump` cores for the same Run).
  Rejected for the reason ADR-0050 gave per System: the two readers would each need a
  method-selection rule (fixed precedence or a new `method` argument) no consumer needs; one core
  per Run keeps them method-blind.
- **Co-hold the System and Run locks** (`SYSTEM → RUN`). Rejected: the single-`CRASHED`-Run-per-
  System invariant (§4) makes the System scope redundant for capture, and the issue's intent is to
  move the boundary to the Run. If that invariant were ever weakened (e.g. concurrent live captures
  of one domain), re-adding the System lock as the outer scope is the documented next step — it is
  not pre-built speculatively.
- **Migrate / backfill existing per-System cores to per-Run keys.** Rejected: M0/M1 carries no
  persisted production cores, so this is a one-time format shift, not a data migration (same basis
  as ADR-0050).
