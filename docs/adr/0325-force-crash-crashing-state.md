# ADR 0325 — A `crashing` System state closes the force_crash physical-crash-window race

- **Status:** Accepted
- **Date:** 2026-07-10
- **Deciders:** kdive maintainers
- **Spec:** [`../superpowers/specs/2026-07-10-force-crash-crashing-marker-1078-design.md`](../superpowers/specs/2026-07-10-force-crash-crashing-marker-1078-design.md)
- **Issue:** [#1078](https://github.com/randomparity/kdive/issues/1078)
- **Depends on:** [ADR-0320](0320-leaseholder-power-lifecycle.md) §1a (which named this
  residual race and deferred it here), [ADR-0021](0021-reconciler-loop-drift-repair.md) (the
  reconciler drift-repair loop this reuses for leak recovery)

## Context

ADR-0320 reclassified `control.power` as `contributor` leaseholder control and denied power on
a non-`READY` System — at admission and at execution — so that crash evidence on a `CRASHED`
System is not destroyed through the power path. The execution guard (`_power_target`) re-reads
`system.state` under the `SYSTEM` advisory lock and refuses when not `READY`.

That guard serializes **DB-state writes**, not **physical ops**. `force_crash_handler`
(`jobs/handlers/control.py`) reads its target under the `SYSTEM` lock and **releases** it,
fires the physical NMI **unlocked** (`control.force_crash`), then re-takes the lock to write
`CRASHED` (`_finalize_force_crash`). Between the NMI and the `CRASHED` write, the DB still
reads `READY`, so a concurrent `power` job's READY re-check passes and can reset the guest
**mid-kdump**, destroying the crash memory `capture_vmcore` reads.

ADR-0320 §1a documented this residual and deferred it as bounded: `CRASHED` (hence any
capturable evidence) is produced only by the `admin` + `destructive_ops`-gated `force_crash`,
so closing it is a within-project coordination race in a privileged, sub-second window, not an
unprivileged-contributor path. The lock cannot be held across the blocking NMI (holding a DB
advisory lock across a hardware round-trip is the anti-pattern ADR-0320 avoids), so the fix
must be a durable marker the power path already respects.

## Decision

**Introduce a durable transient System state `crashing` between `ready` and `crashed`.**
`force_crash` sets `ready → crashing` **under the `SYSTEM` lock, before** firing the NMI, and
`crashing → crashed` after. A reconciler repair resolves a stalled `crashing` System to
`crashed`.

1. **State + transitions.** `SystemState` gains `CRASHING = "crashing"`
   (`domain/capacity/state.py`). The table becomes
   `READY → {CRASHING, TORN_DOWN, REPROVISIONING, FAILED}` (the direct `READY → CRASHED` edge
   is **removed** — `force_crash` was its only producer), `CRASHING → {CRASHED, FAILED,
   TORN_DOWN}`, `CRASHED → {TORN_DOWN, FAILED}` (unchanged). Migration `0065` adds `'crashing'`
   to the `systems_state_check` CHECK (forward-only; no row is `crashing`, no backfill).

2. **`force_crash` handler.** `_force_crash_target` transitions `READY → CRASHING` under the
   lock before returning the target (idempotent for a retry: `CRASHING` re-fires the NMI,
   `CRASHED`/terminal short-circuit). `_finalize_force_crash` transitions `CRASHING → CRASHED`
   (was `READY → CRASHED`), audits `crashing->crashed`, and detaches every non-terminal
   DebugSession.

3. **Power path unchanged.** `power_system` admission and `_power_target` execution already
   refuse any non-`READY` System, so a `CRASHING` System is **automatically** rejected at both
   points — no new power-path check. The only agent-facing effect is that `current_status` may
   read `crashing`; the `control.power` wrapper docstring names it alongside `crashed`.

4. **Leak recovery (reconciler → `crashed`).** A new `repair_stalled_crashing_systems`
   (`reconciler/repairs/systems.py`) runs after `repair_abandoned_jobs` and transitions a
   `crashing` System whose `force_crash` job is in a terminal **non-success** state
   (`failed`/`canceled`) to `crashed` under the `SYSTEM` lock — auditing and detaching sessions
   exactly as finalize would. A System whose `force_crash` job is still `running` (valid lease)
   is left alone.

5. **State-set fan-out.** Every enumerated `SystemState` set is audited (see spec table).
   `CRASHING` is added to the "live/non-terminal" sets — `_NON_TERMINAL_SYSTEM` (quota),
   `_LIVE_SYSTEM_STATES` (allocation reaper), and the console live sets — so a crashing System
   keeps its quota slot, is not reclaimed as orphaned, and keeps its crash-window console. It
   is **not** added to `RUN_HOSTABLE`/`SYSTEM_GONE` (no new Runs; not "gone") or to any
   terminal set.

## Consequences

- The physical-crash-window race closes: a power op cannot drive the physical power op on a
  System whose `force_crash` NMI has fired, because the System is `crashing` (not `READY`)
  from before the NMI until `crashed`, and the power path already refuses non-`READY`.
- `force_crash` gains a two-step state path (`ready → crashing → crashed`) and its retry path
  self-recovers a handler that died after the marker; the reconciler is the backstop when
  retries are exhausted. A stalled `crashing` System never permanently blocks power — it
  resolves to `crashed`, a terminal-capable state cleared by the standard crash workflow.
- `crashing` is a new agent-visible transient state. Agents polling a `force_crash` may
  briefly observe `crashing`; a power op on a `crashing` System is refused with
  `configuration_error` (`current_status: crashing`), directing to the crash workflow.
- The reconciler recovery is evidence-first: in the tiny window where the handler died
  *before* dispatching the NMI, a healthy guest is mislabelled `crashed` and torn down rather
  than powered. Accepted — that window is orders of magnitude smaller than the
  NMI-to-`CRASHED` window, and evidence-first is the safe default for a crash-debugging tool.

## Considered & rejected

- **A job-scoped `crashing_job_id` column instead of a state.** A nullable FK on `systems`
  set before the NMI, with the power path checking "marker set AND owning job still live".
  Rejected: it bolts a parallel guard onto the power path that must be duplicated at admission
  **and** execution (easy to get subtly wrong), whereas a `crashing` state reuses the
  codebase's central "state transitions are guarded data" invariant that the power path
  **already** consults — zero new power-path checks. The migration cost is the same (either a
  new column or a CHECK edit).
- **Recover a stalled `crashing` System to `ready` (availability-first).** Unblocks power
  fastest, but in the likely leak case (NMI already fired) the next power op destroys the
  evidence this change exists to protect — reopening the race on the reconciler-driven
  recovery path. Rejected in favour of evidence-first `crashed`.
- **Recover to `failed` (fail-safe).** Never destroys evidence via power, but discards the
  System unconditionally — no `capture_vmcore` chance, always a reprovision — even when the
  guest is fine. Heavier-handed than `crashed`, which preserves the capturable crash memory.
- **Pre-write `CRASHED` before the NMI (no intermediate state).** Removes the window but
  asserts "crash evidence exists" before the NMI has fired; if the NMI then fails, a healthy
  System is falsely `CRASHED` (one-way to teardown). `crashing` is the honest "NMI in flight,
  outcome pending" marker and is recoverable.
- **Hold the `SYSTEM` advisory lock across the physical NMI.** Serializes power against the
  NMI directly, but holds a DB advisory lock across a blocking hardware round-trip — the
  anti-pattern ADR-0320 explicitly avoids (a stuck NMI would wedge every lock waiter on that
  System, including the reconciler).
