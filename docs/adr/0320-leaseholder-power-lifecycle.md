# ADR 0320 — Power lifecycle is leaseholder control, not destructive administration

- **Status:** Accepted
- **Date:** 2026-07-10
- **Deciders:** kdive maintainers
- **Spec:** [`../superpowers/specs/2026-07-10-leaseholder-power-lifecycle-1062-design.md`](../superpowers/specs/2026-07-10-leaseholder-power-lifecycle-1062-design.md)
- **Issue:** [#1062](https://github.com/randomparity/kdive/issues/1062) (BLACK_BOX_REVIEW.md P2)
- **Supersedes (in part):** [ADR-0037](0037-rbac-hardening-operator-admin.md) §1 (which
  classified `control.power off`/`cycle`/`reset` as `admin` destructive-administration and
  `control.power on` as `operator`) and [ADR-0130](0130-destructive-gate-drop-capability-scope.md)
  (which routed `POWER` through the two-check destructive gate)
- **Depends on:** [ADR-0006](0006-oidc-rbac-attribution.md), [ADR-0020](0020-rbac-audit-gate-implementation.md)
  (the role model + destructive gate), [ADR-0234](0234-external-build-default-and-contributor-role.md)
  (the `contributor` role)

## Context

The black-box review (BLACK_BOX_REVIEW.md P2) wedged a guest: SSH could not fork and there
was **no out-of-band reboot**. `control.power {off,cycle,reset}` and
`control.force_crash` are all destructive-gated behind a provision-time `destructive_ops`
opt-in that had not been set, so both reboot paths were permanently closed for the
System's life (a granted System is not reprovisioned). The only recovery was
`runs.install` with a changed cmdline (re-stage) + `runs.boot`.

The issue asked whether to add a platform-admin **break-glass reboot** that bypasses the
per-System `destructive_ops` opt-in. That was deferred as `needs-design` because a bypass
would break the two-check destructive-gate invariant (role **and** profile opt-in, all or
nothing) codified across ADR-0006/0020/0130.

The deferral surfaced the real defect: the classification itself is wrong, not the gate.
Under ADR-0037 §1 the power lifecycle was pinned high — `on` at `operator`,
`off`/`cycle`/`reset` at `admin` + gate. But:

- A `contributor` who holds the lease already has **in-guest sudo** on the transient VM
  (an agreed property of the platform) and can `reboot`/`poweroff` **in-band**. Gating the
  *out-of-band* power path grants no protection an in-band `reboot` doesn't already bypass;
  it only removes the leaseholder's sole recovery when the in-band path is wedged — the
  exact P2 scenario.
- `contributor` is the lowest role that runs the **full** crash-investigation loop
  (build/upload, install, boot, debug, post-mortem, and the allocations that loop needs).
  It is the leaseholder role in every practical sense.
- Power operations move **no System state** — a domain restart is not a reprovision — and
  have no accounting or reconciler consequence. They are pure runtime lifecycle over a
  transient, project-scoped VM.

`force_crash` is different in kind: it is the deliberate fault-injection primitive
(drives `ready → crashed`, detaches every non-terminal DebugSession, ties to vmcore
capture). It is "cause a crash for debugging," not "recover my VM."

## Decision

**Reclassify the power lifecycle as leaseholder control. `control.power` for every action
(`on`/`off`/`cycle`/`reset`) requires `contributor` and nothing else — no `admin`, no
`destructive_ops` opt-in. `control.force_crash` is unchanged (`admin` + two-check gate +
opt-in).**

1. **Authz.** `power_system` (`mcp/tools/lifecycle/control.py`) drops the
   destructive-gate branch for power and calls a single
   `require_role(ctx, system.project, Role.CONTRIBUTOR)` for all actions, after the
   in-project resolution. The now-dead `_DESTRUCTIVE_POWER_ACTIONS`, `_POWER_ON_ACTIONS`,
   and `_power_required_role` are removed. `_authorize_destructive`/`_op_opt_in` and the
   `resolver` dependency remain for `force_crash`.

1a. **Power acts only on `READY`, enforced at admission and execution.** A `CRASHED`
   System holds preserved crash memory that `capture_vmcore` (contributor-admissible,
   `CRASHED`-gated) reads; destroying it is not leaseholder lifecycle. Admission narrows
   the power-only `_STARTED_SYSTEM = {READY, CRASHED}` set to `{READY}`, returning
   `configuration_error` (directing to the crash workflow) otherwise. Because power is an
   async durable job and `power_handler` re-checks nothing today, admission alone is
   insufficient — a job admitted while `READY` could execute after a `ready→crashed`
   transition (e.g. an interleaved `force_crash`, also `READY`-only). So `power_handler`
   re-reads `system.state` **under the `SYSTEM` advisory lock it already holds**, before
   the physical `control.power()` call (the physical libvirt op runs after the lock is
   released — the lock is not held across the blocking op), and fails the job terminally
   when not `READY`. This closes the DB-state race (a `CRASHED` transition that completed
   before the power job dequeues) and the sequential mislabel-after-reboot. It does **not**
   close one narrow residual: `force_crash_handler` fires its physical NMI *unlocked* and
   writes `CRASHED` only afterward, so between the NMI and that write the DB reads `READY`
   and a concurrent power re-check can still pass. That residual is bounded — `CRASHED`
   (hence capturable evidence) is produced only by the `admin`+opt-in-gated `force_crash`,
   so it is a privileged, sub-second coordination race, not an unprivileged path — and
   fully closing it requires a pre-NMI `crashing` marker on `force_crash`, deferred as a
   follow-up. `force_crash` (already `READY`-only) and `diagnostic_sysrq` are unaffected —
   they never used `_STARTED_SYSTEM`.

2. **Taxonomy.** `POWER` leaves `DESTRUCTIVE_JOB_KINDS`
   (`domain/operations/jobs.py`), which becomes `{REPROVISION, TEARDOWN, FORCE_CRASH}`.
   The gate's `DestructiveOp.__post_init__` keeps deriving from that set (so power can no
   longer be routed through the gate even by mistake). The write-boundary validator's
   accepted-token set (`services/systems/validation.py`'s `_VALID_DESTRUCTIVE_OP_VALUES`)
   is **decoupled** from `DESTRUCTIVE_JOB_KINDS` and derives from a new
   opt-in-consuming-kinds constant `{FORCE_CRASH, REPROVISION}` — so both `"power"`
   (removed here) and `"teardown"` (gated by role only, its opt-in never consulted —
   ADR-0129, an accepted-but-inert phantom token) become rejected (see point 5).

3. **`destructive_ops` scope.** The per-provider provisioning-profile `destructive_ops`
   list now governs the opt-in factor for `force_crash` **and** `systems.reprovision`
   (`_reprovision_opt_in` → `destructive_opt_in(profile, REPROVISION)`), and no power
   action. (`systems.teardown` is gated by role only and never consulted `destructive_ops`
   — ADR-0129.) Its field docstrings and `systems.profile_examples` say so. The freeform
   `list[str]` shape is unchanged.

4. **Contract.** The `control.power` wrapper docstring + `action` `Field` state the
   contributor classification and name `reset`/`cycle` as the leaseholder's recovery path
   for a wedged guest.

5. **No DB migration, but `"power"` and `"teardown"` become rejected write-boundary
   tokens.** `destructive_ops` lives in the `provisioning_profile` JSON as a freeform
   string list; no column, enum, CHECK, or data change. Because the validator's accepted
   set narrows to `{force_crash, reprovision}`, `_reject_unknown_destructive_ops` now raises
   `CONFIGURATION_ERROR` (`valid_destructive_ops: [force_crash, reprovision]`) for any
   profile listing `"power"` or `"teardown"`, on both provision and reprovision (a
   deliberate pre-release breaking change, per the ADR-0315/0319 precedent). The unguarded
   structural read path never raises, so stored rows remain readable; only submitting a
   rejected token fails — the honest signal that neither is an opt-in-gated op.

The `_docmeta.destructive()` MCP annotation on `control.power` is retained: it is an
agent caution hint (a hard reset interrupts the guest), orthogonal to authorization.

## Consequences

- The P2 recovery gap closes on the normal MCP path: a `contributor` with no opt-in can
  `control.power reset` a wedged `READY` System — no new tool, no gate bypass, no invariant
  exception. A `CRASHED` System is not powerable through this path (crash evidence is
  protected); its recovery is the crash workflow.
- The two-check destructive gate is **untouched** and still guards `force_crash`
  (admin + `destructive_ops` opt-in), `systems.reprovision` (operator + `destructive_ops`
  opt-in), and `systems.teardown` (admin role only). This ADR narrows *what the gate
  governs*, not *how it works*.
- `destructive_ops` keeps two consumers (`force_crash`, `reprovision`) — power leaves it.
- Any client or doc that assumed `admin` for `control.power off/cycle/reset` sees the op
  succeed at `contributor`. This is a deliberate widening of a self-service capability
  over one's own transient VM, consistent with in-guest sudo on the default provider; it
  is not a cross-project or cross-tenant grant (the project-role check is retained). Any
  project contributor — not only the provisioning/debugging actor — may power a System;
  power off/cycle/reset does not detach live DebugSessions (unchanged from today), so a
  reboot can leave a session stale for the reconciler's dead-session detach (ADR-0021) to
  reap. Making power reboot detach live sessions like `force_crash` is a possible
  follow-up, out of scope here.

## Considered & rejected

- **Platform-admin break-glass reboot (the issue's original ask).** A new
  `ops.force_reboot` mirroring `ops.force_teardown` (ADR-0062 §4) that bypasses the
  per-System opt-in. Rejected: it adds a tool and a gate-bypass to work around a
  mis-classification. Reclassifying removes the need entirely, and a leaseholder — not
  only a platform admin — is who needs to recover their own guest.
- **A break-glass mode/param on `control.power`.** Mixes two authorization models
  (project-gate vs platform-role) in one handler and one agent-facing docstring; muddies
  the contract.
- **Platform-admin-mutable `destructive_ops` post-provision.** Changes the deliberately
  provision-time/immutable opt-in semantics, adds surface, and still needs a second call
  to recover.
- **Default `destructive_ops` to include power.** Weakens deny-by-default globally and
  still leaves power inside the destructive gate — the wrong classification, just
  pre-opted.
- **Keep `"power"` as an accepted-but-inert legacy token** (e.g.
  `_VALID_DESTRUCTIVE_OP_VALUES ∪ {"power"}`). Rejected: a backward-compat shim that
  contradicts replace-don't-deprecate, and it misleads — an accepted `"power"` token reads
  as "power is still gated" when it no longer is. Hard-rejecting names the change to the
  agent (`unknown_destructive_ops: ["power"]`).
- **Also reclassify `force_crash` to `contributor`.** Out of scope and less clearly
  correct: `force_crash` is deliberate fault injection entangled with the `ready →
  crashed` transition and DebugSession detachment. Left as `admin` + gate + opt-in.
- **Allow contributor power on a `CRASHED` System (uniform READY+CRASHED).** Rejected:
  a CRASHED System holds irreplaceable crash evidence that contributor-admissible
  `capture_vmcore` reads, and power races/destroys it with no lock coordination. Two
  sub-variants were considered and also rejected: (i) keep CRASHED-state power under the
  existing `admin` + `destructive_ops` opt-in gate — reintroduces a meaningful `"power"`
  token, contradicting its removal; (ii) uniform contributor power plus a "refuse while a
  non-terminal `capture_vmcore` exists" interlock — more code, and still allows destroying
  uncaptured evidence and the post-reset mislabel. Denying power on non-`READY` at both
  admission and execution keeps power fully out of `destructive_ops` and closes the DB-state
  race and the mislabel; the crash workflow (`capture_vmcore` → `teardown`/`reprovision`)
  is the path for a CRASHED System. The remaining sub-second physical-crash-window race
  (`force_crash`'s unlocked NMI before its `CRASHED` write) is a bounded, admin-gated
  follow-up (a pre-NMI `crashing` marker on `force_crash`), deliberately not taken here.
