# 0004 — ADR-0583's owning-Run modifier on force-crash is not enforced

## Status

Open
review-by: 2027-03-02

## Concern

ADR-0583's `active`-state admission clause lists force-crash among the operations an active
external-boot activation admits, under an owning-Run modifier — the operation is admitted
for the Run that owns the activation, not for any caller.

`control.force_crash` cannot enforce that modifier. Its handler
(`src/kdive/mcp/tools/lifecycle/control/registrar.py`, `force_crash_system`) carries only a
`system_id`; no Run reaches it, so the admission guard has no caller Run to compare against
the activation's `run_id`. Passing one would mean inventing it.

The external-boot admission matrix therefore admits `FORCE_CRASH` in `active` on the tool's
own authorization and omits it from the owning-Run-scoped set, where
`EXTERNAL_BOOT_RELEASE`, `CAPTURE_VMCORE`, `CAPTURE_TRAFFIC`, `DEBUG_ATTACH`, and
`DEBUG_DETACH` sit. The consequence: any project `ADMIN` may force-crash a System whose
external boot is `active` and owned by a different Run.

Exposure is bounded by two controls that remain in force — the project `ADMIN` role and the
ADR-0130 destructive-operation gate at `src/kdive/security/authz/gate.py` — so this is a
missing defence in depth rather than an open path for an unprivileged caller. It is
recorded because the bound is a role check, not the per-Run fence ADR-0583 specifies, and
because nothing in the code fails when the modifier goes unenforced.

The same reasoning applies to `SYSTEM_WATCH_CRASH`, added to the matrix in this change:
`control.watch_for_crash` likewise carries only a `system_id`.

## Why deferred

Both candidate remedies fall outside issue #2117's frozen surface.

Adding a `run_id` parameter to `control.force_crash` changes a registered public MCP
contract, which #2117's charter does not authorize and which would need its own agent-facing
migration of the tool schema. Deriving the caller's Run from a live DebugSession on the
owning Run makes admission depend on debug state that force-crash does not currently
require, which is a design decision ADR-0583 does not settle and which would change the
tool's behavior for callers with no external boot in play at all.

Choosing between them is work for whoever owns the activation lifecycle, because the second
option only becomes coherent once activations are actually created and attached — which is
#2118's.

## Non-regression boundary

- `FORCE_CRASH` and `SYSTEM_WATCH_CRASH` must stay **denied** in every restricted activation
  state other than `active`. That is the part the matrix does enforce, and
  `tests/services/external_boot/test_reverse_admission.py` asserts it directly.
- The ADR-0130 destructive-operation gate and the project `ADMIN` requirement on
  `control.force_crash` must not be relaxed while this record is open; they are the bound.
- The call site carries a comment naming this record, so the gap is not mistaken for
  coverage by anyone reading the guard.

## What would resolve it

Decide which fence ADR-0583:351 intends, then implement it: either add a `run_id` to
`control.force_crash` (and `control.watch_for_crash`) and move both operations into the
owning-Run-scoped set, or define admission in terms of a live DebugSession on the owning Run
and record that reading in ADR-0583.

Done when a project `ADMIN` acting outside the owning Run is denied force-crash against a
System whose external boot is `active`, and a negative test covers it.

## Provenance

target: src/kdive/services/external_boot/admission.py
target: src/kdive/mcp/tools/lifecycle/control/registrar.py
Found by the `$gauntlet` design pass on the #2117 branch on 2026-09-02 and restated as a
residual in that revision; reclassified as a deferral requiring an owned record by the
`$oathbind` scope audit the same day (finding F5), on the grounds that a knowing
under-enforcement whose remedies are both out of scope is a deferral rather than a residual.
tracker: #2118
