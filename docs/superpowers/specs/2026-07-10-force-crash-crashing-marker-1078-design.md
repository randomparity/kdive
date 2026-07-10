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

- **R1.** A `power` job whose execution re-check observes the System **at or after** the point
  `force_crash` enters the crash window (commits `CRASHING`) must not drive the physical power
  op. This closes the case #1078 names — a power op that dequeues/re-checks during the
  NMI-to-`CRASHED` window. It does **not** mathematically eliminate the fully-concurrent
  interleaving where a power op already passed its READY re-check *before* the marker was
  committed and then fires its own unlocked physical op after the NMI; that residual is narrowed
  to a microsecond window by handler ordering (below) and named as a bounded, accepted remainder
  (see "Residual: the symmetric power-side window").
- **R2.** The admission-time `control.power` check must also reject a System in `crashing`
  (defence in depth; admission is the first gate an agent hits).
- **R3.** A `force_crash` handler that dies **after** entering the crash window but before
  `CRASHED` lands must not leave the System in a permanent limbo that forever blocks power.
- **R4.** No regression to the existing `force_crash` contract: it still requires
  `admin` + the two-check destructive gate, drives the System to `CRASHED`, and detaches
  every non-terminal DebugSession.
- **R5.** No new agent-facing tool, parameter, or gate bypass. The change is internal to the
  `force_crash` state machine, the reconciler, and a behaviour-preserving reorder in
  `power_handler` (no new power guard).

## Decision (summary; full rationale in ADR-0325)

Introduce a durable transient System state **`crashing`** between `ready` and `crashed`.
`force_crash` transitions `ready → crashing` **under the `SYSTEM` lock, before** firing the
NMI, and `crashing → crashed` after. Because the power path already rejects any non-`READY`
System at both admission and execution, a `crashing` System is **automatically** refused by
the existing power guards — no new power-path check is added (a behaviour-preserving reorder in
`power_handler` tightens the symmetric power-side window; see the residual note). Leak recovery
is handled by a reconciler repair that resolves a stalled `crashing` System (one with no active
`force_crash` job) **to `crashed`** (evidence-first). The marker closes the interleaving #1078
names (a power op arriving during the crash window); a fully-concurrent power op that already
passed its READY re-check before the marker is a narrowed, bounded residual, not fully closed.

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

The flow is **state-conditional**, not a flat step list, to satisfy two ordering constraints at
once: on a **first attempt** the provider controller must resolve *before* the `CRASHING`
marker (so a resolution failure leaves the System `READY`, not a committed marker whose NMI
never fired); on a **finalize-only retry** the controller is not needed at all and must not gate
finalize. `_controller → resolver.binding_for_system` is a **DB-backed** resolution (a `systems`
row read + in-memory port lookup — *not* a provider network round-trip; the only real provider
call is the NMI itself), which can still fail with `NOT_FOUND` / a DB error.

1. **`_force_crash_precheck`** (under `SYSTEM` lock, **no transition**) reads state and classifies:
   - terminal (`torn_down`/`failed`) or `CRASHED` → **done** (nothing to do / already finalized).
   - `CRASHING` → **finalize-only** (a retry): skip the controller and the NMI, go straight to
     step 4. The marker means the NMI was already dispatched on a prior attempt.
   - `READY` → **first attempt**: proceed to steps 2–3.
   - any other non-terminal state (`defined`/`provisioning`/`reprovisioning`) → terminal
     `CONFIGURATION_ERROR`; admission already blocked these, so this is defence in depth.
2. **First attempt only:** resolve the controller (`_controller`; may fail → the System is still
   `READY`, power-recoverable, exactly today's behaviour), then **`_enter_crashing`** (under the
   `SYSTEM` lock) re-reads and transitions `READY → CRASHING`. This commit is the **last DB write
   immediately before** the NMI dispatch. If the re-read finds the state already moved (a raced
   teardown, etc.), skip the NMI and fall through to finalize/return. The marker→NMI gap holds no
   DB read and no binding lookup — only the `asyncio.to_thread` dispatch hand-off (see the window
   note under the residual, below).
3. Fire the physical NMI **unlocked**. If `control.force_crash` **raises**, let
   the exception **propagate** (no special-casing). The provider does not tell us whether the
   NMI landed: `LocalLibvirtControl.force_crash` raises `CONTROL_FAILURE` for a missing domain
   **and** for any `libvirt.libvirtError`, and a `libvirtError` can be a transport/RPC error
   raised *after* the NMI was already delivered to QEMU. So we do **not** assume "raise ⇒ NMI
   did not land." `CONTROL_FAILURE` is non-terminal, so `queue.fail` requeues the job to
   `QUEUED`; the retry re-enters `_force_crash_precheck`, sees `CRASHING`, and finalizes to
   `CRASHED` (finalize-only), and if retries exhaust the reconciler backstop does the same — both
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
retry re-enters `_force_crash_precheck`, sees `CRASHING`, and finalizes **without** re-injecting
the NMI. Re-injecting is deliberately avoided: after `CRASHING` the guest is (overwhelmingly)
mid-kdump writing the vmcore this feature protects, and a second NMI into the running crash
kernel is not demonstrably inert — it can abort or corrupt the in-progress dump. Because the
controller is pre-resolved and the marker is committed with no DB read or binding lookup left
before the NMI dispatch, a `CRASHING` System means "NMI dispatch attempted"; finalize-only
resolves the ambiguous outcome (landed / raised-after-delivery / raised-before-delivery)
evidence-first to `CRASHED`. The reconciler recovery below is the backstop for when retries
**are** exhausted.

**On the size of the marker→NMI gap.** The gap between the `CRASHING` commit and the NMI
actually starting is not "a few microseconds of Python": both the NMI and the power op fire via
a bare `asyncio.to_thread(...)`, which submits onto the event loop's **shared default
`ThreadPoolExecutor`**. Between the submit and the callable starting on a worker thread, the
submission can wait behind other concurrent blocking ops, so the gap is bounded by
executor-thread availability — typically sub-millisecond, but it **degrades under executor
saturation**, not a hard microsecond bound. The claim this design rests on is the weaker,
defensible one: the ordering hardening removes the DB read and the binding lookup from the gap
(the parts we control), leaving only the executor hand-off; it **narrows** the window, it does
not prove a microsecond ceiling. Giving the physical ops a dedicated/bounded executor so they
are not starved by unrelated `to_thread` work is a possible further hardening, out of scope
here.

**Windows where `CRASHING` sits on a possibly-healthy guest (mislabel cost, stated honestly).**
Finalize-only + reconciler-to-`CRASHED` means a guest whose NMI never actually landed can be
mislabelled `CRASHED` and torn down. This can arise two ways: (1) a worker kill in the gap
between the `CRASHING` commit and the NMI dispatch (no DB/binding work in this gap, but bounded
by executor-dispatch latency per the note above, not strictly microseconds); (2) an `injectNMI`
raise where the NMI did **not** reach QEMU (e.g. a
missing domain or a pre-delivery transport error) followed by finalize/reconciler resolving to
`CRASHED`. Both are confined to the worker-crash / degraded-provider paths, not the normal
flow, and both are accepted deliberately: the alternative to each — re-firing the NMI, or
marking `FAILED` on any raise — risks destroying the crash memory of a guest that *did* crash,
which is the worse outcome for a crash-debugging tool. Evidence-first is the chosen default.

### Power path (guards unchanged; one ordering hardening)

`power_system` admission (`is not SystemState.READY → configuration_error`) and
`_power_target` execution (`is not SystemState.READY → terminal CategorizedError`) already
reject any non-`READY` System, so both automatically refuse `CRASHING` — **no new guard**. The
only agent-facing effect is that the returned `current_status` may now read `crashing` (a
transient) in addition to `crashed`; the `control.power` wrapper docstring is updated to name
`crashing` alongside `crashed` as a refused, evidence-protecting state so the contract the agent
reads matches the behaviour.

**One ordering hardening in `power_handler`.** Today `power_handler` runs `_power_target` (the
READY re-check, under the lock) → `_controller` (a DB-backed binding resolution) → the unlocked
`control.power`. Reorder so `_controller` runs **first** and `_power_target` is the **last** DB
read immediately before dispatch. This is behaviour-preserving (same checks) and symmetric to the
`force_crash` ordering rule: it removes the binding resolution from the power op's "checked
READY → fired physical op" gap, leaving only the `to_thread` dispatch hand-off (bounded by
executor-thread availability, per the window note above — not a hard microsecond bound). No new
check, no lock held across the physical op.

#### Residual: the symmetric power-side window (bounded, accepted)

The `crashing` marker serializes the two state **reads**, giving two interleavings of a
concurrent `power` and `force_crash` on the same System (`dequeue` uses `FOR UPDATE SKIP LOCKED`
with no per-System single-flight, so both can run on separate workers):

- **A — closed.** `force_crash` commits `CRASHING` before the power job's `_power_target`
  re-check → power reads `crashing` and is refused. This is the #1078 case (a power op arriving
  during the crash window) and AC3 tests it.
- **B — narrowed, not eliminated.** The power job's `_power_target` re-check reads `READY`
  *before* `_enter_crashing` commits `CRASHING`; power then fires its unlocked `control.power`
  after the NMI. No marker checked *before* the physical op can close B, because at the instant
  power decided, the marker did not yet exist. The ordering hardening above shrinks B to the gap
  between power's under-lock re-check and its `to_thread` dispatch (symmetric to the marker→NMI
  gap on the `force_crash` side) — bounded by executor-thread availability, so it degrades under
  `to_thread` saturation rather than being a hard microsecond ceiling. Fully eliminating B
  requires **serializing the two physical ops** — a per-System single-flight making `power` and
  `force_crash` mutually exclusive, or holding the lock across the blocking op (the anti-pattern
  ADR-0320 avoids). That is a larger change, deliberately out of scope here and left as a
  possible follow-up. The residual is deliberately accepted as a narrowing (both the marker→NMI
  and re-check→dispatch gaps are as small as we can make them without cross-op serialization),
  not a proof of elimination.

### Leak recovery (reconciler → `crashed`, evidence-first)

A new reconciler repair `repair_stalled_crashing_systems` (`reconciler/repairs/systems.py`)
runs each tick **after** `repair_abandoned_jobs` (which already dead-letters a zombie
`force_crash` job whose lease expired and attempts are exhausted → `FAILED`). It:

1. Selects Systems in `crashing` that have **no `force_crash` job in an active state
   (`queued` or `running`)** — keyed by the stable dedup_key `{system_id}:force_crash`. This one
   predicate covers every stalled case and avoids enumerating outcomes:
   - `FAILED` (dead-lettered by `repair_abandoned_jobs`: lease expired + attempts exhausted) →
     recovered.
   - `CANCELED` (operator `jobs.cancel`; `force_crash` **is** operator-cancelable while
     `RUNNING` — `cancel_job` transitions `running → canceled` and does not interrupt the
     in-flight handler) → recovered. Excluding it would strand the System in `crashing`
     **forever** with power permanently blocked — the permanent limbo R3 forbids. A cancel
     cannot un-fire an already-dispatched NMI, so evidence-first `crashed` is right.
   - Owning row **absent** → recovered defensively (see invariant below). This should not occur.
   - `RUNNING` or **`QUEUED`** → **left alone**, because **a worker can still reclaim the job.**
     A `queued` job (force_crash mid-retry after a non-terminal `CONTROL_FAILURE`) will be
     dequeued; a `running` job with a valid lease has a live handler that may finalize; and a
     `running` job whose lease **lapsed** with attempts remaining (worker died, not yet
     dead-lettered) is re-dequeued in place by `queue.dequeue` and finalized by the next worker
     (only when attempts are *exhausted* does `repair_abandoned_jobs` move it to `FAILED`, which
     then satisfies the "no active job" predicate). So "leave alone" is keyed on *reclaimability*,
     not on a live handler. These are recovered by **normal job processing, not the reconciler** —
     the reconciler backstops only jobs that will *never* run again. R3 **promptness** therefore
     rests on an explicit **worker-liveness assumption**: while the fleet is dequeuing, the
     worst-case pre-recovery stall is bounded by the force_crash retry budget × backoff; if the
     entire fleet is down, *every* job stalls (not specific to `crashing`), which is out of scope.

   **Invariant (verified):** the `force_crash` job row must remain discoverable for at least as
   long as any System can read `crashing`. This holds — no code path deletes a job row (there is
   no `DELETE FROM jobs` anywhere; retention sweeps touch `idempotency_keys`, `artifacts`,
   `image_catalog`, `upload_manifests`, `resources`, never `jobs`). The "absent row" branch above
   is a belt-and-suspenders backstop, not a live path.
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
- **AC3 (R1, interleaving A).** An adversarial/interleaving test drives `force_crash` to the
  point where `CRASHING` is written and the NMI has fired but `CRASHED` has not, then a power op
  whose execution re-check runs **after** the marker is refused **and the provider
  `control.power` method receives zero calls** (spy call-count `== 0`) — observing the
  load-bearing property (no physical reset), not merely that the DB guard raised.
- **AC3b (R1 residual, interleaving B — documents, does not eliminate).** A test where the power
  op's `_power_target` re-check reads `READY` **before** `force_crash` commits `CRASHING`
  asserts the marker does not retroactively stop that already-decided power op — encoding the
  known, accepted residual (the ordering hardening shrinks it to microseconds; full closure
  needs per-System single-flight, deferred). This AC exists so the residual is proven-understood,
  not silently claimed closed.
- **AC4 (R4).** `force_crash` end-to-end still drives `ready → crashing → crashed` and
  detaches every non-terminal DebugSession; the audit trail shows the `crashing->crashed`
  transition.
- **AC4a (finalize-only retry).** A retry that re-enters the handler with the System already
  `crashing` finalizes to `crashed` **without** calling the provider NMI method a second time
  (spy on `control.force_crash` sees exactly one call across the two attempts) **and without
  resolving the controller** (the finalize-only branch skips `_controller`, so finalize is not
  gated on binding resolution).
- **AC4b (NMI-dispatch failure).** When the provider NMI call raises a (non-terminal)
  `CONTROL_FAILURE`, the exception propagates and the job requeues; the retry finalizes the
  already-`crashing` System to `crashed` **without** re-firing the NMI (spy sees one NMI call).
  The System is never marked `failed` by the handler on a raise. When controller resolution
  raises *before* the marker, the System stays `ready` (no `crashing` marker written).
- **AC5 (R3, no active job → recover).** With a `crashing` System whose `force_crash` job is
  `FAILED` (dead-lettered), `CANCELED` (operator cancel), **or absent**,
  `repair_stalled_crashing_systems` transitions the System to `crashed` (not left `crashing`),
  detaches sessions, and audits. One parametrized test covers all three; the `CANCELED` case is
  set up via a real `jobs.cancel` on a `RUNNING` force_crash.
- **AC5a (R3, active job → leave alone).** A `crashing` System whose `force_crash` job is
  `RUNNING` (valid lease) **or `QUEUED`** (mid-retry) is **not** touched by the repair — it is
  left for the worker/retry path to finalize. This proves the reconciler backstops only jobs
  that will never run again (the worker-liveness boundary).
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
