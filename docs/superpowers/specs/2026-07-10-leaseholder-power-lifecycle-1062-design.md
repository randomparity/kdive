# Spec — Reclassify power lifecycle as leaseholder control (#1062)

- **Issue:** [#1062](https://github.com/randomparity/kdive/issues/1062) —
  "control: no in-band break-glass reboot when `destructive_ops` was not opted in at
  provision (P2)" (BLACK_BOX_REVIEW.md P2)
- **ADR:** [ADR-0320](../../adr/0320-leaseholder-power-lifecycle.md)
- **Follow-up:** [#1078](https://github.com/randomparity/kdive/issues/1078) (residual force_crash physical-crash-window race)
- **Status:** Accepted
- **Date:** 2026-07-10

## Problem

The black-box review wedged a guest by misconfiguring fault injection: SSH could no
longer even fork (banner-exchange timeout), and there was **no out-of-band reboot**.
`control.power {off,cycle,reset}` and `control.force_crash` are all destructive-gated
behind a provision-time `destructive_ops` opt-in that had not been set, so both reboot
paths were permanently closed for that System's life (no reprovision on a granted
System). Recovery was only possible via `runs.install` with a changed cmdline (re-stage)
+ `runs.boot`.

The issue was filed asking whether to add a platform-admin **break-glass reboot** that
bypasses the per-System `destructive_ops` opt-in, and was deferred as `needs-design`
because a bypass would break the two-check destructive gate invariant
(ADR-0006/0020/0130).

### Root cause is a mis-classification, not a missing bypass

The premise "a leaseholder may not reboot their own guest without a provision-time
opt-in" is the defect. The power lifecycle is not destructive *administration* at all:

- **It moves no System state and has no side effects on other objects.** `control.py`'s
  own docstring notes "power moves no System state (a domain restart is not a
  reprovision)"; power has no accounting, lease, or reconciler consequence. It is pure
  runtime lifecycle over a transient, project-scoped VM.
- **On the default provider it is already reachable in-band.** For local-libvirt — the
  motivating black-box case — a `contributor` who holds the lease has in-guest sudo and
  can `reboot`/`poweroff` from inside the guest, so gating the *out-of-band* power path
  protects nothing an in-band `reboot` doesn't already bypass; it only removes the
  leaseholder's sole recovery when the in-band path is wedged.

The in-band-equivalence argument is strongest for local-libvirt; it is weaker for
remote-libvirt (guest access rides the guest-agent seam) and vacuous for fault-inject (a
mock with no real guest). But the classification does not depend on it: on every provider,
power over one's own transient allocated VM is leaseholder lifecycle, not project
administration, and the project-role check already scopes it to the allocation's project.
Lowering it to `contributor` on remote-libvirt/fault-inject is therefore a deliberate,
bounded self-service widening (a contributor may already provision/boot/reprovision on
those providers), not a cross-project grant.

So the fix is to **reclassify** the power lifecycle out of the destructive gate rather
than to add a gate-bypassing break-glass tool. This dissolves the invariant tension
instead of breaking it: no new tool, no bypass, no ADR-0006/0020/0130 exception.

## Requirements

1. `control.power` for **every** action (`on`/`off`/`cycle`/`reset`) requires `contributor`
   — the lowest role that runs the full crash-investigation loop and holds the lease —
   and nothing else. No `admin` requirement and no `destructive_ops` opt-in for any power
   action.
1a. `control.power` acts **only on a `READY` System**, not on `CRASHED`, enforced at
   **both** admission and execution. A CRASHED System holds preserved crash memory that
   `capture_vmcore` (contributor-admissible, `CRASHED`-gated) reads; destroying it is not
   leaseholder lifecycle. Enforcement:
   - **Admission** (`power_system`): narrow the power-only
     `_STARTED_SYSTEM = {READY, CRASHED}` set to `{READY}`. Power on a non-`READY` System
     returns `configuration_error` (`current_status` in `data`) directing the caller to
     the crash workflow (`capture_vmcore` → `systems.teardown`/`systems.reprovision`).
   - **Execution** (`power_handler`): admission alone is insufficient because power is an
     async durable job — a job admitted while `READY` could dequeue after the guest has
     gone `CRASHED`, and `power_handler` today drives the domain with no state re-check.
     So the handler re-reads `system.state` **under the `SYSTEM` advisory lock**, and fails
     the job terminally (clear reason) when the System is not `READY`; the physical
     `control.power()` call runs only past that guard. The lock must be *held across* the
     state re-read (the existing `_control_target` already reads under the lock — reuse
     that read as the guard); the physical libvirt op then runs after the lock is released,
     matching today's structure and `force_crash`'s own unlocked-physical-op pattern (do
     **not** hold a Postgres transaction + advisory lock across the multi-second blocking
     libvirt reset — that would block every other `SYSTEM`-lock contender: reconciler
     dead-session detach, teardown, reprovision, `force_crash` finalize).
   - **What this closes and the residual.** The re-check closes the realistic cases: a
     `force_crash` (or any `CRASHED` transition) that *completed* before the power job
     dequeues, and the sequential mislabel-after-reboot. It does **not** fully close one
     narrow window: `force_crash_handler` fires its physical NMI *unlocked* and writes
     `CRASHED` only afterward (`jobs/handlers/control.py` — target read+release, NMI, then
     finalize), so between the NMI and the `CRASHED` write the DB still reads `READY` and a
     concurrent power job's re-check can pass and reset a crashing guest mid-kdump. This
     residual is bounded: `CRASHED` (hence any capturable crash evidence) is produced
     **only** by `force_crash`, which is `admin` + opt-in-gated, so the interleaving
     requires a privileged, deliberate `force_crash` racing a power op in a sub-second
     window — a within-project coordination race, not an unprivileged path. Fully closing
     it needs a pre-NMI durable "crashing" marker on `force_crash` that the power re-check
     also rejects; that expands `force_crash`'s state machine (and adds a marker-leak
     failure mode) and is **out of scope for #1062**, tracked as follow-up #1078. See
     Non-goals.
2. `control.force_crash` is **unchanged**: `admin` + the two-check destructive gate +
   `destructive_ops` opt-in. It is the deliberate fault-injection primitive (drives
   `ready → crashed`, detaches DebugSessions, ties to vmcore capture), not a recovery
   path.
3. `destructive_ops` (the per-provider provisioning-profile list) consequently governs
   the opt-in factor for `force_crash` **and** `systems.reprovision` — the two
   gated ops that still resolve their opt-in from it. (`systems.teardown` is also gated
   but by role only — it does not consult `destructive_ops`, ADR-0129.) It no longer
   governs any power action.
4. The agent-facing contract (`control.power` wrapper docstring + `action` `Field`
   description) states the new role classification and names `reset`/`cycle` as the
   leaseholder's recovery path for a wedged guest.
5. `systems.profile_examples` surfaces `destructive_ops` with a note that it opts into
   `force_crash` (deliberate kernel crash / fault injection) and `systems.reprovision`,
   and that power/reboot no longer require it, so an agent learns the knob's scope from
   the MCP surface (issue quick-win 1).
6. A short wedged-guest recovery note documents `control.power reset` as the first-class
   recovery **for a `READY` System** — `power_system` admits only `READY` — with the
   `runs.install`(changed cmdline) + `runs.boot` re-stage as the fallback when the guest
   will not respond to reset **or** is not `READY` (wedged before boot, or `CRASHED`),
   where `control.power` returns a `configuration_error`. For a `CRASHED` System the note
   points to the crash workflow (`capture_vmcore` → `teardown`/`reprovision`), not power
   (issue quick-win 2).
7. A new superseding ADR (0320) records the classification change; ADR-0037 §1 and
   ADR-0130 are cited and superseded in the affected part only (not edited in place).

## Non-goals

- No new tool; no platform-admin break-glass; no gate-bypass parameter.
- `control.force_crash` classification is not changed.
- No per-principal lease binding — authorization stays project-role-scoped. "Leaseholder"
  here means *any* `contributor` in the allocation's project, not an exclusive owner; the
  project boundary is the trust boundary in this RBAC model, exactly as every other op is
  scoped today. A power action is therefore reachable by any project contributor, not only
  the actor who provisioned or is debugging the System (see the DebugSession note below).
- No change to `force_crash`'s locking. The sub-second physical-crash-window race (Req 1a
  residual) — where `force_crash`'s unlocked NMI has fired but `CRASHED` is not yet written,
  so a concurrent power op's READY re-check can still pass — is **not** closed here. Closing
  it requires a pre-NMI durable "crashing" marker on `force_crash` (a change to its state
  machine, with a marker-leak failure mode). It is admin-gated and narrow (see Req 1a);
  tracked as follow-up #1078, not fixed in #1062.
- No change to DebugSession handling on power. `control.force_crash` detaches every
  non-terminal DebugSession (the crashed kernel is gone); power off/cycle/reset do **not**
  — this is pre-existing, unchanged behavior. A power reboot can therefore leave a live
  gdbstub/drgn DebugSession pointing at a now-stale guest; the reconciler's dead-session
  detach (ADR-0021) is the existing cleanup. Whether a power reboot should also detach live
  sessions (as force_crash does) is a separate classification decision, deliberately out of
  this issue's scope and noted as a possible follow-up.
- No DB migration and no schema change (see below).

## Design

### Authorization (`mcp/tools/lifecycle/control.py`)

`power_system` currently branches: destructive power actions
(`off`/`cycle`/`reset`) go through `_authorize_destructive` (role `admin` + gate +
`_op_opt_in`), and `on` goes through `require_role(operator)` via `_power_required_role`.

After: **every** power action takes a single `require_role(ctx, system.project,
Role.CONTRIBUTOR)` check, run after the in-project resolution (so it can never be
evaluated against a foreign project). Deleted as now-dead: `_DESTRUCTIVE_POWER_ACTIONS`,
`_POWER_ON_ACTIONS`, `_power_required_role`. `_authorize_destructive`, `_op_opt_in`, and
the `resolver` dependency **remain** — `force_crash` still uses them.

`power_system` also narrows its state admission from `_STARTED_SYSTEM` (`{READY, CRASHED}`)
to `{READY}` — a non-`READY` System returns `configuration_error` (see Req 1a).

The `control.power` MCP annotation stays `_docmeta.destructive()`: the annotation is an
agent *caution hint* (a hard reset still interrupts the guest), orthogonal to the authz
classification. Changing it is out of scope.

### Worker-side state re-check (`jobs/handlers/control.py`)

`power_handler` drives the physical domain (`control.power(domain, action)`) with no
state check today, and the op is async — so the READY-only invariant must also hold at
execution (Req 1a). Extend `_control_target` (or add a peer read) so the `SYSTEM`
advisory-lock transaction it already opens also reads `system.state` and raises a terminal
error (message naming `current_status`) when the System is not `READY`. The physical
`control.power` call stays *after* the lock transaction (as today) — do **not** hold the
advisory lock across the blocking libvirt op. No new lock, no new job kind, no new durable
state.

This guard is load-bearing for the DB-state race (a completed `CRASHED` transition before
the power job runs) but not for the sub-second window where `force_crash`'s physical NMI
has fired and its `CRASHED` write has not yet landed — see Req 1a's residual note and
Non-goals. That window is out of scope here (it requires an admin-gated `force_crash`
racing power, and closing it means changing `force_crash`).

### Domain taxonomy (`domain/operations/jobs.py`)

`POWER` is removed from `DESTRUCTIVE_JOB_KINDS` (which becomes
`{REPROVISION, TEARDOWN, FORCE_CRASH}`). This keeps the taxonomy honest: `DestructiveOp`
(`security/authz/gate.py`) validates `kind ∈ DESTRUCTIVE_JOB_KINDS` in `__post_init__`,
so after this change constructing a `DestructiveOp(kind=POWER)` correctly raises — power
can no longer be routed through the gate even by mistake. `JobKind.POWER` itself is
unchanged (the job still exists and runs).

`DESTRUCTIVE_JOB_KINDS` has **two** in-tree consumers, handled differently:

1. The gate's `DestructiveOp.__post_init__` validation (above) — keeps deriving from
   `DESTRUCTIVE_JOB_KINDS` (it still guards `force_crash`/`reprovision`/`teardown`
   construction). POWER simply leaves the set.
2. `services/systems/validation.py`: `_VALID_DESTRUCTIVE_OP_VALUES` — the write-boundary
   set of *accepted* `destructive_ops` tokens. Today it derives from all of
   `DESTRUCTIVE_JOB_KINDS`, so it accepts `"teardown"` — but `systems.teardown` gates by
   role only and never reads `destructive_ops` (ADR-0129), making `"teardown"` an
   accepted-but-inert phantom token. This change **decouples** the accepted set from
   `DESTRUCTIVE_JOB_KINDS` and derives it from the *opt-in-consuming* kinds
   `{FORCE_CRASH, REPROVISION}` (introduced as a named constant beside
   `DESTRUCTIVE_JOB_KINDS`). The accepted set then contains exactly the tokens that gate
   something; both `"power"` (removed here) and `"teardown"` (long inert) become rejected
   — see Migration/compatibility.

(`_docmeta.py` only *references* `DESTRUCTIVE_JOB_KINDS` in a comment; no code dependency.)

### Provisioning profile (`profiles/`)

No code change to the `destructive_ops` field: it stays a freeform
`list[NonEmptyStr]` on each provider section. Its *meaning* narrows from
`{force_crash, reprovision, power}` to `{force_crash, reprovision}` (power leaves;
`reprovision` still resolves its opt-in here via `_reprovision_opt_in` →
`destructive_opt_in(profile, REPROVISION)`), documented in field docstrings and
`profile_examples`.

### Migration / compatibility

**No DB migration** — `destructive_ops` lives in the `provisioning_profile` JSON as a
freeform string list; no column, enum, CHECK, or data change.

But the write-boundary accepted-token set narrows to `{force_crash, reprovision}`, so
**`"power"` and `"teardown"` both become rejected tokens** — a deliberate pre-release
breaking change (consistent with the repo's replace-don't-deprecate stance and the
ADR-0315/0319 pre-release-break precedent). `_reject_unknown_destructive_ops` now raises
`CONFIGURATION_ERROR` (`unknown_destructive_ops`, `valid_destructive_ops:
[force_crash, reprovision]`) for any profile that lists `"power"` or `"teardown"` — on
both `systems.provision` and `systems.reprovision` (including the read-modify-resubmit
*echo* of a stored profile that carried either token).

This is the correct, honest behavior: after this change `"power"` is not a destructive op
and `"teardown"`'s opt-in was never consulted, so listing either is an error the agent
should see and remove — silently accepting an inert token falsely implies the op is
opt-in-gated (a phantom knob). The unguarded read path (`control._op_opt_in` via the
structural `ProvisioningProfile.parse`) still never raises on a stored token, so a stored
System row is readable; only a *write* submitting a rejected token fails. Recovery is a
one-token edit, which the error names explicitly.

### Docs

- `control.power` wrapper docstring + `action` `Field` (agent-facing contract — the
  load-bearing change per CLAUDE.md).
- `control.py` and `gate.py` module docstrings (remove the "power off/cycle/reset →
  admin + gate" statements; state power is contributor lifecycle).
- `profiles/provisioning.py` `destructive_ops` field docstrings (force_crash-only).
- `profile_examples.py` note surfacing `destructive_ops` scope.
- A wedged-guest recovery note (tool docstring and/or the relevant runbook) naming
  `control.power reset` as the first-class recovery and re-stage as the fallback.
- ADR-0320 + `docs/adr/README.md` index row.
- `docs/design/destructive-gate-per-op-revision.md` — its "affected behavior" table still
  lists `control.power off/cycle/reset` as `admin` + `power`-in-`destructive_ops`; update
  that row (and note ADR-0320 supersession) so the living design doc is not stale.
- Any guide/reference doc that states the power role classification (audit
  `docs/guide/` for `control.power` role text).

## Test plan

Behavioral tests (the existing power tests that assert admin/opt-in are the red-to-green
drivers — they must flip):

- `contributor` may `off`/`cycle`/`reset` a started System with **no** `destructive_ops`
  opt-in and **no** admin role → job enqueued.
- `contributor` may `power on` (previously operator).
- `viewer` is denied any power action (`RoleDenied`).
- A System with no `destructive_ops` opt-in: power `reset` succeeds (the exact
  black-box scenario), while `force_crash` is still denied (`missing=["profile_opt_in"]`).
- `force_crash` unchanged: still requires `admin` + opt-in (existing tests stay green).
- `systems.reprovision` unchanged: its opt-in still reads `destructive_ops` — a profile
  without `"reprovision"` is still denied `profile_opt_in` (guards against the regression
  where narrowing `destructive_ops`'s scope accidentally drops reprovision).
- A power action on a `READY` System with a non-terminal DebugSession succeeds and leaves
  the DebugSession untouched (documents the unchanged no-detach boundary).
- A `CRASHED` System returns `configuration_error` (`current_status: "crashed"`) on
  `control.power reset`/`off`/`cycle`/`on` at admission — crash evidence is protected;
  power admits only `READY`. (Red-to-green flip for any existing test that admitted power
  on CRASHED.)
- Worker-side execution guard: a `power` job whose System is `CRASHED` at execution time
  (admitted `READY`, then transitioned — e.g. `force_crash` interleaved before the power
  job dequeues) fails terminally in `power_handler` and does **not** call the physical
  `control.power` op (the evidence-protection invariant holds at execution, not just
  admission). A `READY`-at-execution System still powers normally.
- A pre-`READY` System (e.g. `provisioning`) returns `configuration_error` on
  `control.power reset` (recovery boundary; re-stage is the fallback).
- Idempotency-key replay on a power action is unchanged.
- `profile_examples` output carries the `destructive_ops` note naming `force_crash` and
  `reprovision`.
- `DestructiveOp(kind=JobKind.POWER)` raises `ValueError` (POWER left the destructive
  set); `DestructiveOp(kind=JobKind.REPROVISION)` still constructs.
- Write-boundary validation: a profile with `destructive_ops: ["power"]` **or**
  `["teardown"]` is rejected with `CONFIGURATION_ERROR` / `unknown_destructive_ops` on
  provision **and** reprovision (echo path); `["force_crash", "reprovision"]` still
  validate; `valid_destructive_ops` is `["force_crash", "reprovision"]`. This flips
  `tests/services/systems/test_system_validation.py` (the `valid_destructive_ops`
  assertion and the accepts-`"power"`/`"teardown"` cases) — red-to-green drivers to update.

## Acceptance criteria

- A `contributor` with no `destructive_ops` opt-in can `control.power reset` a wedged
  `READY` System and get a `power` job — the P2 recovery gap is closed on the normal MCP
  path with no new tool.
- `control.power` on a `CRASHED` System is refused with `configuration_error` — crash
  evidence is not destroyable through the power path.
- `control.force_crash` still requires `admin` + `destructive_ops` opt-in.
- `control.power`'s agent-facing docstring/`Field` state the contributor classification
  and the recovery use.
- `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test).
