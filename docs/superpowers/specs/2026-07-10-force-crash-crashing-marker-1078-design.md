# Spec — Pre-NMI `crashing` marker closes the force_crash physical-crash-window race (#1078)

- **Issue:** [#1078](https://github.com/randomparity/kdive/issues/1078) —
  "control: force_crash physical NMI can be raced by a power op before CRASHED is written"
- **ADR:** [ADR-0325](../../adr/0325-force-crash-crashing-state.md)
- **Parent:** [#1062](https://github.com/randomparity/kdive/issues/1062) /
  [ADR-0320](../../adr/0320-leaseholder-power-lifecycle.md) §1a (which named this residual and
  deferred it here)
- **Status:** Accepted
- **Date:** 2026-07-10

## Problem

`force_crash_handler` (`src/kdive/jobs/handlers/control.py`) resolves its target and reads
`system.state` under the `SYSTEM` advisory lock (`_force_crash_target`), **releases** the
lock, fires the physical NMI **unlocked** (`control.force_crash`), then re-takes the lock to
write `CRASHED` (`_finalize_force_crash`). Between the NMI firing and the `CRASHED` write the
DB row still reads `READY`.

`power_handler`'s execution-time re-check (`_power_target`, ADR-0320) and `control.power`
admission both accept a `READY` System. So a `power` job that interleaves in that window
passes its READY re-check and drives a physical power op — resetting the guest **mid-kdump**
and destroying the crash memory that `capture_vmcore` reads.

The `SYSTEM` advisory lock serializes **DB-state writes**, not **physical ops**: the NMI is a
side effect on hardware that outlives the lock's hold, so no amount of DB-state re-checking
under the existing lock closes the window. ADR-0320 §1a documented this residual and deferred
it here as bounded (`CRASHED`, hence any capturable evidence, is produced only by the
`admin` + `destructive_ops`-gated `force_crash`, so the interleaving needs a privileged,
deliberate `force_crash` racing a power op in a sub-second window).

## Requirements

- **R1.** A `power` job must not drive the physical power op on a System whose `force_crash`
  NMI has fired but whose `CRASHED` write has not yet landed.
- **R2.** The admission-time `control.power` check must also reject a System in that window
  (defence in depth; admission is the first gate an agent hits).
- **R3.** A `force_crash` handler that dies **after** entering the crash window but before
  `CRASHED` lands must not leave the System in a permanent limbo that forever blocks power.
- **R4.** No regression to the existing `force_crash` contract: it still requires
  `admin` + the two-check destructive gate, drives the System to `CRASHED`, and detaches
  every non-terminal DebugSession.
- **R5.** No new agent-facing tool, parameter, or gate bypass. The fix is internal to the
  `force_crash` state machine and the reconciler.

## Decision (summary; full rationale in ADR-0325)

Introduce a durable transient System state **`crashing`** between `ready` and `crashed`.
`force_crash` transitions `ready → crashing` **under the `SYSTEM` lock, before** firing the
NMI, and `crashing → crashed` after. Because the power path already rejects any non-`READY`
System at both admission and execution, a `crashing` System is **automatically** refused by
the existing power guards — no new power-path check is added. Leak recovery is handled by a
reconciler repair that resolves a stalled `crashing` System (whose `force_crash` job has been
dead-lettered) **to `crashed`** (evidence-first).

## Behaviour

### The `crashing` state and transitions

`SystemState` gains `CRASHING = "crashing"`. Transition table (`domain/capacity/state.py`):

- `READY → {CRASHING, TORN_DOWN, REPROVISIONING, FAILED}` — the direct `READY → CRASHED`
  edge is **removed**; `force_crash` was its only producer and now goes through `CRASHING`.
- `CRASHING → {CRASHED, FAILED, TORN_DOWN}` — normal completion (`CRASHED`), plus terminal
  edges so a stuck `crashing` System can still be failed/torn down.
- `CRASHED → {TORN_DOWN, FAILED}` — unchanged.

A forward-only migration (`0065`) adds `'crashing'` to the `systems_state_check` CHECK
constraint. No existing row is `crashing`, so there is no data backfill.

### `force_crash` handler flow

**Ordering matters: resolve the controller *before* marking `CRASHING`.** The provider
binding lookup (`_controller` → `resolver.binding_for_system`) is a DB + provider call that
can be slow or **fail** when the provider is degraded. It must run while the System is still
`READY`, so a resolution outage fails the handler with the System unchanged (`READY`,
power-recoverable, exactly today's behaviour) — never with a committed `CRASHING` marker whose
NMI never fired. The `READY → CRASHING` commit is the **last DB write immediately before** the
NMI dispatch, so the marker→NMI window is a few microseconds of Python (no I/O, no provider
call).

1. **`_controller`** — resolve the provider controller (may fail; leaves the System `READY`).
2. **`_enter_crashing`** (under `SYSTEM` lock) returns `(target, fire_nmi)`:
   - terminal (`torn_down`/`failed`) or `CRASHED` → return `None` (nothing to do / already
     finalized; e.g. a retry after a completed finalize).
   - `CRASHING` → return `(target, fire_nmi=False)` — a **retry**: the marker means the NMI
     was already dispatched on a prior attempt, so finalize only (see below).
   - `READY` → transition `READY → CRASHING`, return `(target, fire_nmi=True)`.
   - any other non-terminal state (`defined`/`provisioning`/`reprovisioning`) → terminal
     `CONFIGURATION_ERROR`; admission already blocked these, so this is defence in depth.
3. If `fire_nmi`: fire the physical NMI **unlocked**. If `control.force_crash` **raises**, let
   the exception **propagate** (no special-casing). The provider does not tell us whether the
   NMI landed: `LocalLibvirtControl.force_crash` raises `CONTROL_FAILURE` for a missing domain
   **and** for any `libvirt.libvirtError`, and a `libvirtError` can be a transport/RPC error
   raised *after* the NMI was already delivered to QEMU. So we do **not** assume "raise ⇒ NMI
   did not land." `CONTROL_FAILURE` is non-terminal, so `queue.fail` requeues the job to
   `QUEUED`; the retry re-enters `_enter_crashing`, sees `CRASHING`, and finalizes to `CRASHED`
   (finalize-only, below), and if retries exhaust the reconciler backstop does the same — both
   resolve the ambiguity evidence-first. (Marking the System `FAILED` on a raise was considered
   and rejected: it discards the crash memory of a genuinely-crashed guest whose NMI landed but
   whose response timed out, and it converts today's retryable NMI error into a non-retried
   terminal failure — see AC4b and the mislabel-window note.)
4. **`_finalize_force_crash`** (under `SYSTEM` lock):
   - terminal → return.
   - `CRASHED` → sessions already detached by the transition owner; return (idempotent).
   - `CRASHING` → transition `CRASHING → CRASHED`, audit `crashing->crashed`, detach every
     non-terminal DebugSession of the System.

**Retry is finalize-only — it never re-fires the NMI.** The stable dedup key
(`{system_id}:force_crash`) prevents a second concurrent `force_crash` job. A handler that
died after `READY → CRASHING` is recovered by job retry (`fail()` requeues to `QUEUED`): the
retry re-enters `_enter_crashing`, sees `CRASHING`, and finalizes **without** re-injecting the
NMI. Re-injecting is deliberately avoided: after `CRASHING` the guest is (overwhelmingly)
mid-kdump writing the vmcore this feature protects, and a second NMI into the running crash
kernel is not demonstrably inert — it can abort or corrupt the in-progress dump. Because the
controller is pre-resolved and the marker is committed microseconds before the NMI dispatch, a
`CRASHING` System means "NMI dispatch attempted"; finalize-only resolves the ambiguous outcome
(landed / raised-after-delivery / raised-before-delivery) evidence-first to `CRASHED`. The
reconciler recovery below is the backstop for when retries **are** exhausted.

**Windows where `CRASHING` sits on a possibly-healthy guest (mislabel cost, stated honestly).**
Finalize-only + reconciler-to-`CRASHED` means a guest whose NMI never actually landed can be
mislabelled `CRASHED` and torn down. This can arise two ways: (1) a worker kill in the
microsecond gap between the `CRASHING` commit and the NMI dispatch (controller pre-resolved, so
no I/O sits in this gap); (2) an `injectNMI` raise where the NMI did **not** reach QEMU (e.g. a
missing domain or a pre-delivery transport error) followed by finalize/reconciler resolving to
`CRASHED`. Both are confined to the worker-crash / degraded-provider paths, not the normal
flow, and both are accepted deliberately: the alternative to each — re-firing the NMI, or
marking `FAILED` on any raise — risks destroying the crash memory of a guest that *did* crash,
which is the worse outcome for a crash-debugging tool. Evidence-first is the chosen default.

### Power path (no change to the guards)

`power_system` admission (`is not SystemState.READY → configuration_error`) and
`_power_target` execution (`is not SystemState.READY → terminal CategorizedError`) already
reject any non-`READY` System, so both automatically refuse `CRASHING`. This satisfies R1 and
R2 with **zero** new power-path code; the only agent-facing effect is that the returned
`current_status` may now read `crashing` (a transient) in addition to `crashed`. The
`control.power` wrapper docstring is updated to name `crashing` alongside `crashed` as a
refused, evidence-protecting state so the contract the agent reads matches the behaviour.

### Leak recovery (reconciler → `crashed`, evidence-first)

A new reconciler repair `repair_stalled_crashing_systems` (`reconciler/repairs/systems.py`)
runs each tick **after** `repair_abandoned_jobs` (which already dead-letters a zombie
`force_crash` job whose lease expired and attempts are exhausted → `FAILED`). It:

1. Selects Systems in `crashing` whose `force_crash` job (dedup_key `{system_id}:force_crash`)
   is in a **terminal non-success** state — `FAILED` (dead-lettered by `repair_abandoned_jobs`
   after its lease expired and attempts were exhausted) **or `CANCELED`** (an operator
   `jobs.cancel`; `force_crash` **is** operator-cancelable while `RUNNING` —
   `cancel_job`/`mcp/tools/jobs.py` transitions `running → canceled` and does not interrupt the
   in-flight handler). Both must be recovered: once `CRASHING` is set the NMI has (over­
   whelmingly) fired, so a terminal owning job whose System still reads `crashing` means the
   handler stopped before finalize — leaving the System stranded unless the reconciler resolves
   it. A still-`running` (valid-lease) or `succeeded` job is left alone (a `succeeded` job
   already wrote `CRASHED`, so its System would not read `crashing`; a `running` job may yet
   finalize). Recovering `CANCELED` evidence-first to `crashed` is deliberate: a cancel cannot
   un-fire the NMI, and excluding it would strand the System in `crashing` **forever** with
   power permanently blocked — exactly the permanent limbo R3 forbids.
2. Under the `SYSTEM` lock, re-reads the state (skip if it left `crashing`) and transitions
   `crashing → crashed`, records an audit event (`tool="control.force_crash"`,
   `transition="crashing->crashed"`, reconciler principal), and detaches every non-terminal
   DebugSession (the same effect `_finalize_force_crash` would have had).

**Why `crashed`, not `ready`:** because the controller is pre-resolved and the marker is
committed microseconds before the NMI dispatch, and the reaper fires only after the job's full
lease + retry budget is exhausted (minutes), a stall almost always means the NMI already fired
and the guest is down. Recovering to `crashed` preserves crash memory (the feature's own
purpose) and hands the System to the standard crash workflow
(`capture_vmcore` → `teardown`/`reprovision`). Recovering to `ready` to "unblock power" would,
in that likely case, let the next power op destroy the very evidence this change protects —
reopening the race on the recovery path. The System is **not** wedged: `crashed` is a normal,
terminal-capable state, so R3 is met — the marker never leaks into a permanent limbo. The
possibly-healthy-guest mislabel windows are the two enumerated above (worker kill in the
pre-dispatch gap; `injectNMI` raised without delivery); both are confined to worker-crash /
degraded-provider paths and accepted as the evidence-first tradeoff.

## State-set fan-out (every enumerated `SystemState` set audited)

Adding `CRASHING` is not a one-line enum change; each set that enumerates states was audited:

| Set (file) | Include `CRASHING`? | Rationale |
| --- | --- | --- |
| transition table (`domain/capacity/state.py`) | yes | as above |
| `systems_state_check` (migration `0065`) | yes | CHECK must allow the value |
| `_NON_TERMINAL_SYSTEM` (`services/systems/admission.py`) | **yes** | a crashing System occupies a per-project quota slot |
| `_LIVE_SYSTEM_STATES` (`reconciler/repairs/allocations.py`) | **yes** | a crashing System's allocation is live, not orphaned — must not be reclaimed mid-crash |
| `_RUNNING_SYSTEM_STATE_VALUES` (`providers/infra/console_hosting.py`) | **yes** | keep streaming the crash-window console |
| `_LIVE_STATES` (`jobs/handlers/console_rotate.py`) + `_LIVE_SYSTEM_STATES` (`reconciler/repairs/console_rotation.py`) | **yes** | keep console rotation live across the crash |
| `RUN_HOSTABLE` / `SYSTEM_GONE` (`services/runs/states.py`) | **no** | no new Run may start on a crashing System; it is not "gone" either (transient) |
| `TERMINAL_SYSTEM_STATES` (`rules.py`), `_TERMINAL_SYSTEM_STATES` (`services/images/retention.py`), `_ORPHANED_SYSTEM_TERMINAL_STATES` (`reconciler/repairs/systems.py`) | **no** | `CRASHING` is non-terminal |

## Acceptance criteria

- **AC1 (R1).** A `power` job whose System is in `crashing` fails terminally at
  `_power_target` (execution re-check) with `configuration_error`/non-READY, and never calls
  `control.power`. Covered by a handler-level test that sets `crashing` between admission and
  the power op.
- **AC2 (R2).** `control.power` admission on a `crashing` System returns
  `configuration_error` with `current_status: "crashing"`.
- **AC3 (R1, the race).** An adversarial/interleaving test drives `force_crash` to the point
  where `CRASHING` is written and the NMI has fired but `CRASHED` has not, and asserts a
  concurrent power op is refused **and the provider `control.power` method receives zero
  calls** in that window (spy call-count `== 0`) — so the test observes the load-bearing
  property (no physical reset), not merely that the DB guard raised.
- **AC4 (R4).** `force_crash` end-to-end still drives `ready → crashing → crashed` and
  detaches every non-terminal DebugSession; the audit trail shows the `crashing->crashed`
  transition.
- **AC4a (retry / F2).** A retry that re-enters the handler with the System already `crashing`
  finalizes to `crashed` **without** calling the provider NMI method a second time (spy on
  `control.force_crash` sees exactly one call across the two attempts).
- **AC4b (NMI-dispatch failure).** When the provider NMI call raises a (non-terminal)
  `CONTROL_FAILURE`, the exception propagates and the job requeues; the retry finalizes the
  already-`crashing` System to `crashed` **without** re-firing the NMI (spy sees one NMI call).
  The System is never marked `failed` by the handler on a raise. When controller resolution
  raises *before* the marker, the System stays `ready` (no `crashing` marker written).
- **AC5 (R3, dead-letter).** With a `force_crash` job dead-lettered (`FAILED`) while its System
  is `crashing`, `repair_stalled_crashing_systems` transitions the System to `crashed` (not left
  `crashing`), detaches sessions, and audits; a System with a still-`running` force_crash job is
  untouched.
- **AC5a (R3, operator cancel).** An operator `jobs.cancel` on a `RUNNING` force_crash whose
  System is `crashing` (handler no longer finalizing) does **not** strand the System:
  `repair_stalled_crashing_systems` recovers the `canceled` + `crashing` System to `crashed`.
- **AC6 (fan-out).** Quota accounting counts a `crashing` System; the allocation reaper does
  not reclaim an `active` allocation whose only System is `crashing`; console
  hosting/rotation treat `crashing` as live.
- **AC7.** `just ci` green (lint, `ty`, tests, doc/schema guards). The removed
  `READY → CRASHED` edge breaks no other producer (verified by grep + test).

## Out of scope

- Reworking the `SYSTEM` advisory lock to span the physical NMI (holding a DB lock across a
  blocking hardware op is the anti-pattern ADR-0320 explicitly avoids).
- Changing `force_crash`'s authorization (`admin` + gate + opt-in is unchanged).
- Any change to `capture_vmcore`, teardown, or the power authorization model.
