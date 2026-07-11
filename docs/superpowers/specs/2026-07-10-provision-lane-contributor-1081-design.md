# Provision lane + reprovision are leaseholder control (#1081)

- **Issue:** [#1081](https://github.com/randomparity/kdive/issues/1081)
- **ADR:** [ADR-0326](../../adr/0326-provision-lane-contributor-lifecycle.md)
- **Status:** Design approved
- **Date:** 2026-07-10

## Problem

A `contributor` already drives the full crash-investigation loop over a resource
it leases: `allocations.request` reserves the slot, `runs.install`/`runs.boot`
stage and boot kernels, `control.power` (ADR-0320) reboots the guest, and
arbitrary in-guest drgn runs. Yet the five tools that *instantiate a System on
the slot the contributor already leased* are gated at `operator`:

| Tool | Role today | What it does |
|------|-----------|--------------|
| `systems.define` | operator | open a pre-provision rootfs-upload window (System → `DEFINED`) |
| `artifacts.create_system_upload` | operator | mint the presigned upload URL for that window |
| `systems.provision_defined` | operator | admit a `DEFINED` System after upload |
| `systems.provision` | operator | instantiate a System directly on a granted Allocation |
| `systems.reprovision` | operator + `destructive_ops` opt-in | re-stage the profile on a `READY` System in place |

A contributor can lease a slot and drive kernels onto a System but cannot
instantiate the System on the slot it already holds. This is the same
mis-classification ADR-0320 corrected for `control.power`.

## The load-bearing question, answered from code

> Does `systems.provision` commit *additional* scarce capacity/cost beyond what
> `allocations.request` already reserved?

**The Allocation grant is the commit point for every cost dimension.**
`services/allocation/admission/core.py::admission_gate` performs an atomic
check-then-debit under `PROJECT → RESOURCE` locks and, on grant, `_grant`
(core.py:488-507) reserves budget (`accounting.reserve`), occupies the per-host
capacity cap, claims PCIe devices, and debits `max_concurrent_allocations`. A
granted Allocation already occupies the host slot (both `GRANTED` and `ACTIVE`
are in the `OCCUPYING` set).

`systems.provision` debits **none** of those. It inserts the System row and flips
the Allocation `granted → active` (`admission.py:755`) — an occupancy-neutral
transition — then enqueues the provision job. There is no `accounting.*` call, no
funding gate (#833), no host-cap or budget check anywhere in the provision path.
The System is sized to exactly what the Allocation already priced
(`_stored_profile_for` rejects any restatement that conflicts with the
at-grant snapshot).

**The single fresh scarce dimension provision commits is the per-project
`max_concurrent_systems` quota** (`admission.py:701`, `_within_system_quota`) — a
fail-closed count-then-create under the PROJECT lock. It is **enforced at
admission regardless of role**: lowering the tool's role to `contributor` does
not uncap it. So the quota is not a reason to keep the role at operator; it is an
independent governance limit that stays enforced either way.

`systems.reprovision` commits **no** fresh capacity: it acts in place on an
existing `READY` System under the same Allocation, with no `_within_system_quota`
call and no accounting.

**Verdict:** the allocation is the commit point. Provision and reprovision are
"instantiate / re-stage on the slot I already hold," directly analogous to
`control.power`'s "reboot the guest I already provisioned." The contributor case
from ADR-0320 holds.

## Decision

Move all five tools from `operator` to `contributor`, in both the exposure map
and the runtime handler gate (the two-place move ADR-0320 established). Drop
`reprovision`'s destructive-op gate and `destructive_ops` opt-in entirely,
matching `control.power`. Preserve `reprovision`'s `READY`-only and no-live-run
guards. Keep the coupled `jobs.cancel` allow-list honest by adding the provision
and reprovision job kinds to `CONTRIBUTOR_CANCELABLE_JOB_KINDS`.

No DB migration: every change is a role constant, a taxonomy frozenset, or
agent-facing text — the same shape as ADR-0320.

### 1. Exposure map — `src/kdive/mcp/exposure.py`

Flip `_OPERATOR → _CONTRIBUTOR` for the five entries:
`artifacts.create_system_upload` (L115), `systems.define` (L219),
`systems.provision` (L220), `systems.provision_defined` (L221),
`systems.reprovision` (L222). `systems.provision` stays in `CORE_TOOLS`.

### 2. Runtime handler gates (the real boundary)

The provision lane is gated in **two layers**, both of which enforce the role and
both of which must move — the runtime-resolution wrapper (the MCP-transport path)
and the admission service (defense-in-depth, and the only gate the handler-direct
unit tests exercise):

- `registrar.py` — `define`/`provision`/`provision_defined`/`reprovision`
  `required_role=Role.OPERATOR → Role.CONTRIBUTOR` at the four
  `with_runtime_for_*` call sites (L187/241/279/469); enforced by
  `_runtime_resolution.py::_authorized_kind` → `require_role`.
- `services/systems/admission.py` — the in-service `require_role(..., Role.OPERATOR)`
  at **L419** (`create_for_allocation`, reached by `define` and `provision`) and
  **L621** (`provision_defined`) → `Role.CONTRIBUTOR`. Missing these leaves a
  contributor denied at admission even after the wrapper passes, and leaves every
  handler-direct provision/define test red.
- `artifacts` upload seam — `_SYSTEM_UPLOAD.required_role` (`uploads.py:374`)
  `Role.OPERATOR → Role.CONTRIBUTOR`. This is the single field that made
  `create_system_upload` operator while its `_create_upload`-sharing sibling
  `create_run_upload` was already contributor.

### 3. Reprovision — drop the destructive gate and opt-in

`_reprovision_in_lock` (`admin.py:161-168`) removes the `DestructiveOp`
construction, the `assert_destructive_allowed` call, and the `DestructiveOpDenied`
denial/audit branch; `_reprovision_opt_in` (`admin.py:197-199`) is deleted. The gate carried the
handler's only role check (the handler-direct path bypasses the registrar wrapper),
so — matching `power_system`'s single in-handler `require_role` (ADR-0320) — add
`require_role(ctx, system.project, Role.CONTRIBUTOR)` in `_reprovision_in_lock`
after the system/allocation resolution, where the gate stood. The registrar-layer
`require_role` (`registrar.py:469`, now `CONTRIBUTOR`) still fronts the MCP path;
project ownership, `REPROVISIONING` dedup, `READY`-only (`admin.py:176-177`),
no-live-run, and rootfs validation are all retained.

`_audit_destructive_denied` (`admin.py:202`) and `_authz_denied` (the `_common`
alias) **stay** — `teardown_system` (`admin.py:337-338`) still calls both; do not
delete them. What must be removed to keep ruff clean (zero-warning policy) are the
now-unused gate imports `DestructiveOp, DestructiveOpDenied, assert_destructive_allowed`
(`admin.py:42`) and the `_REPROVISION` alias (`admin.py:51`) once the gate removal
leaves it unreferenced — it is distinct from `_REPROVISION_KIND`, the
idempotency-kind string, which stays.

The `_docmeta.destructive()` MCP annotation on the `reprovision` wrapper is
retained: like power's, it is an agent caution hint (re-stage interrupts the
guest), orthogonal to authorization.

### 4. Taxonomy — `src/kdive/domain/operations/jobs.py`

Reprovision follows power out of the destructive taxonomy:

- `DESTRUCTIVE_JOB_KINDS`: `{REPROVISION, TEARDOWN, FORCE_CRASH}` →
  `{TEARDOWN, FORCE_CRASH}`. (Only consumer is the gate's
  `DestructiveOp.__post_init__`; removing `REPROVISION` makes an accidental
  `DestructiveOp(REPROVISION)` fail closed, mirroring power.)
- `OPT_IN_DESTRUCTIVE_JOB_KINDS`: `{FORCE_CRASH, REPROVISION}` → `{FORCE_CRASH}`.
- `CONTRIBUTOR_CANCELABLE_JOB_KINDS`: add `PROVISION` and `REPROVISION`. A
  contributor that can now enqueue provision/reprovision can cancel the job it
  enqueued — the same "acting on its own transient resource" rule the set's
  docstring states, and the reason its old `# operator-gated provision lane`
  justification no longer holds. Fail-closed membership is preserved.
- Update the three frozenset docstrings in this module to name the new membership.

The reviewed guard test encodes the reversed decision and must be flipped:
`tests/mcp/jobs/test_jobs_tools.py:243` currently asserts
`JobKind.PROVISION not in CONTRIBUTOR_CANCELABLE_JOB_KINDS` ("provision lane out of
scope") with the L235 "operator-gated provision lane" comment. Rewrite it to assert
`PROVISION`/`REPROVISION` **are** contributor-cancelable and keep the fail-closed
check for the remaining operator-only kinds. The sibling L242 assertion
(`not CONTRIBUTOR_CANCELABLE & DESTRUCTIVE`) still holds because `REPROVISION` also
leaves `DESTRUCTIVE_JOB_KINDS`.

### 5. Write-boundary validator — automatic, plus a rejected token

`services/systems/validation.py::_VALID_DESTRUCTIVE_OP_VALUES` derives from
`OPT_IN_DESTRUCTIVE_JOB_KINDS`, so it narrows to `{"force_crash"}` with no code
change there. Consequence: a profile listing `"reprovision"` in
`provider.destructive_ops` is now rejected with `CONFIGURATION_ERROR`
(`valid_destructive_ops: ["force_crash"]`) on provision and reprovision — a
deliberate pre-release break, exactly the ADR-0320 treatment of `"power"` and
`"teardown"`. Stored rows stay readable (the structural read path never
validates); only submitting the retired token fails.

### 6. Agent-facing contract

Update the wrapper docstrings + `Field` descriptions for the five tools to state
the `contributor` classification and, for `reprovision`, that it no longer
consumes a `destructive_ops` opt-in. No ADR references in any text that
serializes into the tool schema (`test_no_adr_leak`). No guardrail catches this
drift (`gen_rbac_tool_matrix.py` reads `exposure.py`, not docstrings), so the five
sites are named to prevent a miss — four are in the systems registrar being edited
anyway, the fifth is in a separate artifacts file:

- `registrar.py:167` (`define`, "Operator only")
- `registrar.py:221` (`provision`, "Operator only")
- `registrar.py:265` (`provision_defined`, "Requires operator")
- `registrar.py:450` (`reprovision`, "Requires operator **and opt-in**" → drop
  both — contributor, no opt-in)
- `catalog/artifacts/registrar.py:252` (`create_system_upload`, "Requires
  operator")

One `Field` description is worse than a stale label — it actively instructs the
now-rejected input: `registrar.py:442`, the `reprovision` `profile` param, reads
`"New provisioning profile; must opt in to reprovision."` Section 5 makes
`destructive_ops: ["reprovision"]` a hard `CONFIGURATION_ERROR`, so this Field must
be reworded to state the profile no longer needs a `destructive_ops` opt-in for
reprovision. (The `_docmeta.destructive()` annotations at `registrar.py:414/435`
are the retained agent caution hints, not authz text — leave them.)

Two more agent-facing surfaces name the old classification and must be corrected —
both serialize into a tool schema, the drift class this project guards against:

- **`jobs.cancel`** (`mcp/tools/jobs.py`): the `jobs_cancel` wrapper docstring
  (~L406-411) and the module docstring (~L11-16) say cancelling
  `provision`/`reprovision` "requires operator." Move `provision`/`reprovision`
  into the contributor-cancelable sentence, leaving only `teardown`/`force_crash`
  (and the platform/internal kinds) as operator-only.
- **`destructive_ops` advertisements**: `profile_examples.py:81` ("...reprovision
  only; leave it empty...") and the `ProvisioningProfile` field docstrings
  `provisioning.py:116` and `:165` name `reprovision` as a valid opt-in token.
  Strike `reprovision` so they advertise `force_crash` only — mirroring ADR-0320's
  `power` cleanup. (`profile_examples.py:79` "without reprovisioning" and
  `provisioning.py:425` dedup-factor doc are unrelated and stay.)

### 7. Generated docs

Regenerate `just rbac-matrix` (`docs/guide/safety-and-rbac.md`), `just docs`, and
any doc-resource snapshots; `just ci` must be green.

## Non-goals (pre-answered)

- **`systems.teardown` stays admin.** The leaseholder's self-service teardown is
  `allocations.release` → reconciler GC (ADR-0037 §1). Direct teardown is
  attribution-bearing administrative destruction of a still-allocated System.
- **`images.upload`/`delete` stay operator** — they mutate the shared image
  catalog visible to other tenants.
- **`force_crash` is unchanged** — admin + two-check gate + opt-in; it is
  deliberate fault injection, not "instantiate my own System."

## Success criteria

1. A `contributor` (non-operator) on a project can call all five tools against
   its own Allocation/System and succeed; a `viewer` is denied at the runtime
   gate and does not see the tools in `list_tools`.
2. `reprovision` succeeds for a contributor on a `READY` System with a profile
   whose `destructive_ops` is empty (no opt-in needed); it still refuses a
   non-`READY` System with `configuration_error` and a System with a live run.
3. A profile submitting `destructive_ops: ["reprovision"]` is rejected with
   `CONFIGURATION_ERROR`.
4. A contributor can `jobs.cancel` its own `provision`/`reprovision` job; the
   fail-closed operator gate remains for every non-leaseholder kind.
5. `force_crash` still requires admin + `destructive_ops` opt-in (unchanged).
6. `just ci` green, including the regenerated RBAC matrix and `test_no_adr_leak`.

## Rollback

Pure code/text/frozenset changes on one branch; revert the commit. No migration,
no data change, no external-service state.
