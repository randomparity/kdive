# ADR 0326 — Provision lane and reprovision are leaseholder control, not operator administration

- **Status:** Accepted
- **Date:** 2026-07-10
- **Deciders:** kdive maintainers
- **Spec:** [`../superpowers/specs/2026-07-10-provision-lane-contributor-1081-design.md`](../archive/superpowers/specs/2026-07-10-provision-lane-contributor-1081-design.md)
- **Issue:** [#1081](https://github.com/randomparity/kdive/issues/1081)
- **Supersedes (in part):** [ADR-0234](0234-external-build-default-and-contributor-role.md) §3
  (which classified `systems.define`/`provision`/`provision_defined`/`reprovision` and
  `artifacts.create_system_upload` as "stays operator")
- **Depends on:** [ADR-0320](0320-leaseholder-power-lifecycle.md) (the leaseholder-control
  principle and the destructive-taxonomy shape it left), [ADR-0234](0234-external-build-default-and-contributor-role.md)
  (the `contributor` role), [ADR-0037](0037-rbac-hardening-role-separation.md) §1 /
  [ADR-0038](0038-system-reprovision-in-place.md) §3 (the lifecycle split and the
  reprovision-as-iteration framing)

## Context

A `contributor` is the lowest role that runs the full crash-investigation loop over a
resource it leases: `allocations.request` reserves the slot, `runs.install`/`runs.boot`
stage and boot kernels, `control.power` (ADR-0320) reboots the guest, and arbitrary
in-guest drgn runs. But the five tools that instantiate a System on the slot the
contributor already leased were gated at `operator` (ADR-0234 §3): `systems.define`,
`artifacts.create_system_upload`, `systems.provision_defined`, `systems.provision`, and
`systems.reprovision` (the last also behind a `destructive_ops` opt-in). A contributor
could lease a slot and drive kernels onto a System but could not instantiate the System on
the slot it already held — the same mis-classification ADR-0320 corrected for power.

### The capacity-commit question

Whether provision should be leaseholder-controlled turns on one question: does
`systems.provision` commit scarce capacity/cost *beyond* what `allocations.request`
already reserved? The code answers it.

The **Allocation grant is the commit point** for every cost dimension.
`admission_gate` (`services/allocation/admission/core.py`) is an atomic check-then-debit
under `PROJECT → RESOURCE` locks; on grant, `_grant` reserves budget
(`accounting.reserve`), occupies the per-host capacity cap, claims PCIe devices, and
debits `max_concurrent_allocations`. A `GRANTED` Allocation already occupies the host slot.

`systems.provision` debits **none** of those. It inserts the System row and flips the
Allocation `granted → active` — an occupancy-neutral transition (both states are in the
`OCCUPYING` set) — then enqueues the job. There is no `accounting` call, no #833 funding
gate, and no host-cap or budget check on the provision path, and the System is sized to
exactly what the Allocation already priced. The one fresh scarce dimension provision
commits is the per-project `max_concurrent_systems` quota (`_within_system_quota`) — but
that is a **fail-closed count enforced at admission regardless of role**, so lowering the
tool's role does not uncap it. `reprovision` commits no fresh capacity: it re-stages an
existing `READY` System in place under the same Allocation, with no quota or accounting
call.

So the allocation is the commit point; provision and reprovision are "instantiate / restage
on the slot I already hold," directly analogous to power's "reboot the guest I already
provisioned." The contributor case from ADR-0320 holds.

`reprovision` was additionally fenced by the two-check destructive gate + a provision-time
`destructive_ops` opt-in. ADR-0038 §3 already frames reprovision as "iterating on your own
granted System, not administering the project," and it already enforces `READY`-only — so
the crash-evidence concern that gated power is handled here too. Keeping the opt-in would
make reprovision the lone `contributor`-role op still consuming `destructive_ops`, and would
reintroduce the exact ADR-0320 P2 trap: a leaseholder blocked from acting on its own
`READY` System because a provision-time opt-in it cannot change was never set.

## Decision

**Reclassify the provision lane and `reprovision` as leaseholder control: all five tools
require `contributor` and nothing more. `reprovision` drops the destructive gate and the
`destructive_ops` opt-in, keeping its `READY`-only and no-live-run guards.
`control.force_crash` is unchanged (`admin` + two-check gate + opt-in).**

1. **Authz.** Each tool moves `operator → contributor` in the exposure map
   (`mcp/exposure.py`, advisory discoverability) and in the runtime handler gate (the real
   boundary). The provision lane is gated in two enforcing layers, both of which move: the
   runtime-resolution wrapper (`registrar.py` `required_role` kwargs → `_runtime_resolution`
   `require_role`, the MCP-transport path) and the admission service's own
   `require_role(..., Role.OPERATOR)` at `admission.py:419` (`create_for_allocation`, for
   `define`/`provision`) and `:621` (`provision_defined`) — the latter is defense-in-depth
   and the only gate the handler-direct tests exercise. The upload seam moves via
   `_SYSTEM_UPLOAD.required_role`, the single field that made `create_system_upload` operator
   while its `_create_upload`-sharing sibling `create_run_upload` was already contributor.

2. **Reprovision drops the gate.** `_reprovision_in_lock` removes the `DestructiveOp`
   construction, `assert_destructive_allowed`, and the denial/audit branch; `_reprovision_opt_in`
   is deleted. Because the gate carried the handler's only role check (the handler-direct path
   bypasses the registrar wrapper), an explicit `require_role(ctx, system.project,
   Role.CONTRIBUTOR)` replaces it in the handler — matching `power_system`'s single in-handler
   `require_role` (ADR-0320). The registrar-layer gate (now contributor) still fronts the MCP
   path; project ownership, `REPROVISIONING` dedup, `READY`-only, no-live-run, and rootfs
   validation are retained. The `_docmeta.destructive()` annotation is kept as an agent caution
   hint (re-stage interrupts the guest), orthogonal to authz.

3. **Taxonomy — reprovision follows power out of the destructive family.** In
   `domain/operations/jobs.py`: `REPROVISION` leaves `DESTRUCTIVE_JOB_KINDS` (now
   `{TEARDOWN, FORCE_CRASH}`), so an accidental `DestructiveOp(REPROVISION)` fails closed;
   and leaves `OPT_IN_DESTRUCTIVE_JOB_KINDS` (now `{FORCE_CRASH}`). `force_crash` is the sole
   kind still routed through the two-check gate; `teardown` remains a member gated by role
   only (ADR-0129), unchanged.

4. **`jobs.cancel` allow-list stays honest.** `PROVISION` and `REPROVISION` join
   `CONTRIBUTOR_CANCELABLE_JOB_KINDS`. A contributor that can now enqueue provision/reprovision
   can cancel the job it enqueued — the set's own "acting on its own transient resource" rule,
   whose prior `# operator-gated provision lane` justification this decision removes. The gate
   stays fail-closed: any kind absent from the set still requires operator.

5. **Write-boundary — `"reprovision"` becomes a rejected token.** The validator's accepted set
   (`services/systems/validation.py::_VALID_DESTRUCTIVE_OP_VALUES`) derives from
   `OPT_IN_DESTRUCTIVE_JOB_KINDS`, so it narrows to `{force_crash}` automatically. A profile
   listing `"reprovision"` in `destructive_ops` is now rejected with `CONFIGURATION_ERROR`
   (`valid_destructive_ops: [force_crash]`) on both provision and reprovision — a deliberate
   pre-release break, per the ADR-0320/0315/0319 precedent. Stored rows stay readable (the
   structural read path never validates); only submitting the retired token fails.

6. **Contract.** The five wrapper docstrings + `Field` descriptions state the `contributor`
   classification; `reprovision`'s says it no longer consumes a `destructive_ops` opt-in. No
   ADR references in text that serializes into the tool schema (`test_no_adr_leak`).

**No DB migration.** Every change is a role constant, a taxonomy frozenset, or agent-facing
text — the same shape as ADR-0320.

## Consequences

- A `contributor` can define/upload-to/provision/reprovision a System on an Allocation it
  already holds, with no operator involvement and no `destructive_ops` opt-in. This is a
  deliberate widening of a self-service capability over one's own transient, project-scoped
  resource, consistent with in-guest sudo on the default provider; the project-role check is
  retained, so it is not a cross-project or cross-tenant grant.
- The `max_concurrent_systems` quota still fences System instantiation for every role; a
  contributor cannot exceed it. Cost, budget, funding, host capacity, and PCIe claims remain
  committed at the Allocation grant, upstream of every tool moved here.
- `destructive_ops` now has a single consumer, `force_crash`. The two-check gate is untouched
  and still guards `force_crash` (admin + opt-in) and `systems.teardown` (admin, role only).
- Any client or doc that assumed `operator` for these five tools sees them succeed at
  `contributor`. A profile that listed `"reprovision"` under `destructive_ops` now fails
  submission — the honest signal that reprovision is no longer opt-in-gated.
- `reprovision` re-stage still does not detach live DebugSessions (unchanged); the reconciler's
  dead-session detach (ADR-0021) reaps any left stale, as with a power reboot.

## Considered & rejected

- **Keep `reprovision`'s `destructive_ops` opt-in while lowering its role to contributor.**
  Rejected: it makes reprovision the lone contributor op consuming `destructive_ops` (an
  inconsistency) and reintroduces the ADR-0320 P2 wedge — a contributor holding a `READY`
  System it cannot reprovision because a provision-time opt-in was never set. Dropping the
  opt-in matches power's treatment and removes the trap.
- **Keep `reprovision` in `DESTRUCTIVE_JOB_KINDS` as a role-only member (like teardown).**
  Rejected: reprovision is now behaviorally identical to power (contributor, `READY`-only, no
  gate, no opt-in), so it should follow power fully out of the destructive family; leaving it
  in the set invites accidental re-gating and misreads its classification.
- **Move the provision lane but not `jobs.cancel`.** Rejected: it leaves a contributor able to
  start a provision it cannot cancel and leaves the allow-list's `# operator-gated provision
  lane` justification factually wrong. ADR-0320 kept the cancel path in sync (power is in the
  set); this follows it.
- **Reclassify `systems.teardown` or `images.upload`/`delete` too.** Out of scope and
  different in kind: teardown is attribution-bearing administrative destruction of a
  still-allocated System (the leaseholder's self-service path is `allocations.release` →
  reconciler GC, ADR-0037 §1); the image catalog is shared cross-tenant state.
- **Reclassify `force_crash`.** Rejected (as in ADR-0320): it is deliberate fault injection
  entangled with the `ready → crashed` transition and DebugSession detachment, not
  "instantiate my own System." Left as `admin` + gate + opt-in.
- **Keep `"reprovision"` as an accepted-but-inert `destructive_ops` token.** Rejected: a
  backward-compat shim that contradicts replace-don't-deprecate and misleads — an accepted
  token reads as "reprovision is still opt-in-gated" when it no longer is. Hard-rejecting names
  the change to the agent.
