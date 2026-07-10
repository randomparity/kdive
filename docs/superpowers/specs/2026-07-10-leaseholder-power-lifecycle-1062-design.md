# Spec — Reclassify power lifecycle as leaseholder control (#1062)

- **Issue:** [#1062](https://github.com/randomparity/kdive/issues/1062) —
  "control: no in-band break-glass reboot when `destructive_ops` was not opted in at
  provision (P2)" (BLACK_BOX_REVIEW.md P2)
- **ADR:** [ADR-0320](../../adr/0320-leaseholder-power-lifecycle.md)
- **Status:** Draft
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
opt-in" is the defect. A `contributor` who holds the lease already has in-guest sudo on
the transient VM — they can `reboot`/`poweroff` **in-band**. Gating the *out-of-band*
power path therefore protects nothing: it only removes the leaseholder's sole recovery
at the exact moment the in-band path is wedged. The two-check gate's real job is guarding
genuinely-destructive *administration* (deliberate crash, cross-project teardown), and
the project-role check already scopes power to the allocation's project.

So the fix is to **reclassify** the power lifecycle out of the destructive gate rather
than to add a gate-bypassing break-glass tool. This dissolves the invariant tension
instead of breaking it: no new tool, no bypass, no ADR-0006/0020/0130 exception.

## Requirements

1. `control.power` for **every** action (`on`/`off`/`cycle`/`reset`) requires `contributor`
   — the lowest role that runs the full crash-investigation loop and holds the lease —
   and nothing else. No `admin` requirement and no `destructive_ops` opt-in for any power
   action.
2. `control.force_crash` is **unchanged**: `admin` + the two-check destructive gate +
   `destructive_ops` opt-in. It is the deliberate fault-injection primitive (drives
   `ready → crashed`, detaches DebugSessions, ties to vmcore capture), not a recovery
   path.
3. `destructive_ops` (the per-provider provisioning-profile list) consequently governs
   **only** `force_crash`.
4. The agent-facing contract (`control.power` wrapper docstring + `action` `Field`
   description) states the new role classification and names `reset`/`cycle` as the
   leaseholder's recovery path for a wedged guest.
5. `systems.profile_examples` surfaces `destructive_ops` with a note that it now governs
   only `force_crash` (fault injection), so an agent learns the knob's scope from the MCP
   surface (issue quick-win 1).
6. A short wedged-guest recovery note documents `control.power reset` as the first-class
   recovery, with the `runs.install`(changed cmdline) + `runs.boot` re-stage as the
   fallback when the guest will not respond to reset (issue quick-win 2).
7. A new superseding ADR (0320) records the classification change; ADR-0037 §1 and
   ADR-0130 are cited and superseded in the affected part only (not edited in place).

## Non-goals

- No new tool; no platform-admin break-glass; no gate-bypass parameter.
- `control.force_crash` classification is not changed.
- No per-principal lease binding — authorization stays project-role-scoped (any
  `contributor` in the allocation's project may power the System, exactly as every other
  op is scoped today).
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

The `control.power` MCP annotation stays `_docmeta.destructive()`: the annotation is an
agent *caution hint* (a hard reset still interrupts the guest), orthogonal to the authz
classification. Changing it is out of scope.

### Domain taxonomy (`domain/operations/jobs.py`)

`POWER` is removed from `DESTRUCTIVE_JOB_KINDS` (which becomes
`{REPROVISION, TEARDOWN, FORCE_CRASH}`). This keeps the taxonomy honest: `DestructiveOp`
(`security/authz/gate.py`) validates `kind ∈ DESTRUCTIVE_JOB_KINDS` in `__post_init__`,
so after this change constructing a `DestructiveOp(kind=POWER)` correctly raises — power
can no longer be routed through the gate even by mistake. All consumers of
`DESTRUCTIVE_JOB_KINDS` must be audited during implementation; the known consumer is the
gate's validation. `JobKind.POWER` itself is unchanged (the job still exists and runs).

### Provisioning profile (`profiles/`)

No code change to the `destructive_ops` field: it stays a freeform
`list[NonEmptyStr]` on each provider section. Its *meaning* narrows to force_crash-only,
documented in field docstrings and `profile_examples`.

### Migration / compatibility

**None.** `destructive_ops` is stored inside the `provisioning_profile` JSON on each
System row as a freeform string list. An existing System provisioned with
`destructive_ops: ["power"]` keeps that entry; it simply becomes inert (never consulted
for a power op). No column, enum, or CHECK changes; no data rewrite. This is the same
"pre-existing entry becomes inert" pattern the codebase already tolerates for freeform
profile lists.

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
- Idempotency-key replay on a power action is unchanged.
- `profile_examples` output carries the `destructive_ops` note/field.
- `DestructiveOp(kind=JobKind.POWER)` raises `ValueError` (POWER left the destructive
  set).

## Acceptance criteria

- A `contributor` with no `destructive_ops` opt-in can `control.power reset` a wedged
  started System and get a `power` job — the P2 recovery gap is closed on the normal MCP
  path with no new tool.
- `control.force_crash` still requires `admin` + `destructive_ops` opt-in.
- `control.power`'s agent-facing docstring/`Field` state the contributor classification
  and the recovery use.
- `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test).
