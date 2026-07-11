# Decouple build submission from a provisioned system

- **Status:** Approved
- **Date:** 2026-06-18
- **Issue:** [#554](https://github.com/randomparity/kdive/issues/554)
- **ADR:** [ADR-0169](../../adr/0169-decouple-build-system-binding.md)

## Problem

`runs.create` requires a ready `system_id` (an `active` Allocation over a provisioned
System) before a Run — and therefore a build — can be submitted. Yet the build does not run
*on* the System: it runs on an independently-selected build host (`worker-local`, `ssh`, or
`ephemeral_libvirt`) named in the build profile. The System is consumed only by `runs.install`
and `runs.boot`.

The coupling inverts cost. Provisioning is the slow, capacity-consuming step; gating a build
on it forces a black-box user to allocate and provision a target — holding capacity — just to
attempt a build that may fail instantly (as in #552). A build failure then wastes a held
allocation.

The coupling is largely incidental. At build time the only use of the System is
`ProviderResolver.runtime_for_run`, which walks `run → system → allocation → resource.kind`
purely to pick *which builder* to run. That selection needs the resource **kind**, not a
provisioned System.

## Constraint that shapes the design: builds are provider-specific

A build is not target-agnostic. `local-libvirt` produces `bzImage` + `vmlinux`;
`remote-libvirt` produces a `.tar.gz` bundle that remote-install gunzips. The builder, the
artifact shape, and the installer must agree. A Run must therefore commit to a resource
**kind** when it is created, even when it has no System yet — so the right builder runs and
the eventual System is constrained to a kind that can consume the produced kernel.

So the decoupling is not "build with no target information." It is: **build against a declared
resource kind without holding a provisioned System**, then bind a System of that kind before
install.

## Decision summary

1. A Run records a `target_kind` (the committed resource kind) and may exist with no
   `system_id` (unbound).
2. `runs.create` accepts an optional `system_id`. With a `system_id` (bound path) it behaves as
   today and derives `target_kind` from the System. Without one (unbound path) it requires an
   explicit `target_kind` and consumes no target capacity.
3. The build resolves its builder from `run.target_kind`, not from the System join.
4. A new `runs.bind(run_id, system_id)` attaches a ready System to an unbound Run, enforcing
   the same admission `runs.create` enforces today plus a kind-match contract.
5. `runs.install` / `runs.boot` reject an unbound Run with a `configuration_error` whose next
   action is `runs.bind`.
6. Discovery affordances make `target_kind` usable by an agent: a self-correcting
   `runs.create` error lists the valid kinds, and system listings expose each System's kind.

The full lifecycle gains an unbound lane:

```
runs.create (unbound, target_kind) → runs.build → runs.bind → runs.install → runs.boot
runs.create (bound, system_id)     → runs.build → runs.install → runs.boot   (unchanged)
```

## Data model (migration 0042)

```sql
ALTER TABLE runs ALTER COLUMN system_id DROP NOT NULL;
ALTER TABLE runs ADD COLUMN target_kind text;
UPDATE runs r
   SET target_kind = res.kind
  FROM systems s
  JOIN allocations a ON a.id = s.allocation_id
  JOIN resources   res ON res.id = a.resource_id
 WHERE s.id = r.system_id;

-- Defensive: the FK chain guarantees totality, but fail loudly with a clear message
-- rather than an opaque NOT NULL violation if a legacy row ever escaped it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM runs WHERE target_kind IS NULL) THEN
        RAISE EXCEPTION 'migration 0042: % run(s) have an unresolved target_kind backfill',
            (SELECT count(*) FROM runs WHERE target_kind IS NULL);
    END IF;
END $$;

ALTER TABLE runs ALTER COLUMN target_kind SET NOT NULL;
```

- `system_id` becomes nullable; an unbound Run has `system_id IS NULL`.
- `target_kind` is `NOT NULL` for **every** Run (bound and unbound). The backfill is total
  because the chain it joins is referentially total: `runs.system_id → systems.id`,
  `systems.allocation_id → allocations.id`, and `allocations.resource_id → resources.id` are
  all `NOT NULL` foreign keys with the default RESTRICT on-delete, so no live Run can have a
  broken `system → allocation → resource` chain (a resource cannot be deleted while an
  allocation references it; a system cannot while a run references it). The migration still
  asserts zero remaining `NULL` `target_kind` rows *before* `SET NOT NULL`, failing with a
  clear message rather than the opaque constraint-violation error if that invariant is ever
  violated. A migration test exercises the backfill on a pre-existing bound Run.
- No `CHECK` constraint enumerates kinds in SQL — the valid set is the deployment's *registered*
  provider kinds, which is runtime state, not schema state. Validation lives in the service
  layer (validate-at-create).

Domain mirror: `Run.system_id: UUID | None`, `Run.target_kind: ResourceKind`.

## `runs.create`

Signature gains an optional `target_kind: str | None` and makes `system_id: str | None`
optional. The `create_run` service injects the `ProviderResolver` (it does not today) to
validate `target_kind` against registered kinds.

**Bound path (`system_id` present).** Unchanged admission: investigation open + project +
OPERATOR role; System ready; Allocation active and lease not lapsed; single project; reuse
assertion; one-Run-per-System; build-host↔source compat. Additionally:
- `target_kind` is derived from the System's resource kind and stored on the Run.
- If the caller *also* passes an explicit `target_kind` that differs from the System's kind →
  `configuration_error` (`reason: target_kind_mismatch`).

**Unbound path (`system_id` absent).**
- `target_kind` is required; absent → `configuration_error` (`reason: target_kind_required`)
  whose `data` carries `available_target_kinds` (the registered kinds).
- `target_kind` must be a registered provider kind; unknown → `configuration_error`
  (`reason: unknown_target_kind`) with `available_target_kinds`. A registered kind always has a
  builder — `ProviderRuntime.builder` is a required field — so "registered" is also "buildable";
  `available_target_kinds` advertises exactly the kinds that can build.
- Investigation validated (exists, in caller's projects, OPERATOR role, state open-for-run).
- Build-host↔source compat check runs (already System-independent).
- A supplied `reuse_requirement` is rejected (`configuration_error`,
  `reason: reuse_requires_system`) — sizing is asserted against a System at bind time, not here.
- Insert `Run(system_id=NULL, target_kind=…, state=CREATED)`; flip investigation
  `open→active`; set `last_run_at`.
- **Locks: INVESTIGATION only.** No Allocation or System exists to lock; no target capacity is
  debited. This is the decoupling.

`RunCreateResult` gains `target_kind` and an optional `system_id` (None when unbound). The MCP
response's `suggested_next_actions` for an unbound create is `["runs.build"]`.

## Builder resolution

`runs.build` resolves the builder from `run.target_kind` via `resolver.resolve(kind)` instead
of `resolver.runtime_for_run`. This removes the build's last System dependency and works
identically for bound and unbound Runs. `runs.boot` keeps `runtime_for_run` (boot needs the
System regardless).

## `runs.bind` (new tool + service)

`runs.bind(run_id, system_id, reuse_requirement?)`, `mutating()`, OPERATOR role. The
bound-path System admission is **factored out of `create_run`** into a shared
`_admit_system_for_run` helper that both `create` (bound path) and `bind` call — the logic is
reused, not duplicated.

Under the existing `ALLOCATION → SYSTEM → INVESTIGATION` lock order, in fixed order so the most
specific error wins:

1. **Run is bindable** — exists, caller's project, `system_id IS NULL`, state ∈
   `{created, running, succeeded}`. An already-bound Run → `transport_conflict`
   (`reason: run_already_bound`). A `failed`/`canceled` Run → `stale_handle`.
2. **System reachable + ready** — `system.state ∈ RUN_HOSTABLE`, same project as the Run.
3. **Allocation live** — active, lease not lapsed.
4. **Kind match** — `system_resource_kind == run.target_kind`, else `configuration_error`
   (`reason: target_kind_mismatch`, both kinds in `data`).
5. **One-Run-per-System** — the target System has no other live Run.
6. **Optional reuse assertion** — the same `snapshot_satisfies` sizing/PCIe check as create's
   reuse path.

On success: `UPDATE runs SET system_id = %s WHERE id = %s AND system_id IS NULL`. The
`IS NULL` compare-and-set makes a concurrent double-bind safe — the second writer updates 0
rows and returns `transport_conflict`. Audit a `runs.bind` transition; return
`ToolResponse.success` with `suggested_next_actions=["runs.install"]`.

`runs.bind` always rejects an already-bound Run (no idempotent same-system retry): binding is a
one-shot transition; an agent that retries should call `runs.get` to observe the binding.

## `runs.install` / `runs.boot` guards

Two layers:
- **MCP admission** (`steps.py`): before enqueueing the worker job, an unbound Run
  (`system_id IS NULL`) → `configuration_error` (`reason: run_not_bound`,
  `suggested_next_actions=["runs.bind"]`). Fail-fast at the synchronous boundary.
- **Worker handler** (`runs_install.py`, `runs_boot.py`): a defensive `system_id is None` guard
  raising `configuration_error`, so install/boot can never dereference a null System even if a
  Run is unbound at job time.

## `runs.cancel`

Cancelling an unbound Run frees no System (there is nothing to free). The cancel service must
tolerate `system_id IS NULL` rather than unconditionally dereferencing it.

## Unbound Run lifecycle

Decoupling introduces a Run state the reconciler did not previously have to reason about: a Run
with no System. Its lifecycle is deliberately simple and operator/agent-driven, not
auto-reaped.

- **No auto-reaper fails an unbound Run.** The reconciler's run-failing path
  (`repairs/allocations.py`) fails a Run when its System's Allocation is torn down; an unbound
  Run has neither, so that path never fires. An unbound Run therefore stays in `created` /
  `running` / `succeeded` until it terminates through its own lane (build failure → `failed`,
  or explicit `runs.cancel` → `canceled`). This is intentional: an unbound Run is a cheap row,
  and binding may legitimately come long after the build.
- **Artifact retention is unchanged.** A `succeeded` unbound Run holds its `kernel_ref`
  artifact exactly as a `succeeded` bound Run does today; there is no new class of leaked
  artifact. Existing upload cleanup keyed on `RunState.CREATED`
  (`reconciler/cleanup/uploads.py`) continues to apply.
- **Investigation close.** An unbound non-terminal Run keeps its Investigation `active`
  (ADR-0026), exactly as a bound non-terminal Run does. Closing an Investigation with a
  dangling unbound Run requires cancelling that Run first — the same precondition that already
  governs bound Runs.
- **Creation is throttled by the build plane, not target capacity.** Removing the target-capacity
  debit means `runs.create` (unbound) no longer holds an Allocation, so Run *rows* are cheap to
  create. Actual work is still bounded: a build only proceeds under a build-host capacity lease
  (`build_host_selection.resolve_and_admit`), and unbound `created` rows that never build are
  reclaimed by `runs.cancel`. No separate per-project unbound-Run quota is introduced — there is
  no evidence of an abuse vector the build-host lease does not already bound, and a speculative
  quota would add admission complexity for an unproven need.

## Discovery affordances

An explicit-required `target_kind` is only usable if an agent can discover the valid values and
find a System of the right kind.

1. **Self-correcting `runs.create`** — the missing/unknown-`target_kind` errors carry
   `available_target_kinds` (the resolver already produces this set as its `registered`
   detail). The agent learns the valid set exactly where it hits the wall; no new tool.
2. **Resource `kind` on System listings** — `systems.list` and `inventory.list` system rows
   gain a `kind` field, so an agent can `systems.list(state=ready)` and pick one whose
   `kind == run.target_kind` to feed `runs.bind`.
3. `runs.get` on an unbound Run renders `target_kind` and `system_id: null`.

## Error taxonomy

| Condition | Category | reason |
| --- | --- | --- |
| Unbound create, no `target_kind` | `configuration_error` | `target_kind_required` |
| Unbound create, unknown `target_kind` | `configuration_error` | `unknown_target_kind` |
| Bound create, explicit `target_kind` ≠ System kind | `configuration_error` | `target_kind_mismatch` |
| Unbound create with `reuse_requirement` | `configuration_error` | `reuse_requires_system` |
| `bind` of an already-bound Run | `transport_conflict` | `run_already_bound` |
| `bind` where System kind ≠ `target_kind` | `configuration_error` | `target_kind_mismatch` |
| `bind`/`install`/`boot` of a terminal Run | `stale_handle` | — |
| `install`/`boot` of an unbound Run | `configuration_error` | `run_not_bound` |
| `bind` losing the CAS race | `transport_conflict` | `run_already_bound` |

## Testing

- **Migration**: list-twice idempotency; backfill populates `target_kind` for a pre-existing
  bound Run; `SET NOT NULL` holds; the defensive `DO $$ … $$` guard is present (the FK chain
  makes a NULL unreachable, so the test asserts the happy-path backfill leaves zero NULLs).
- **`runs.create`**: bound path unchanged (regression); bound path with matching/ mismatched
  explicit `target_kind`; unbound success; unbound missing/unknown `target_kind` returns
  `available_target_kinds`; unbound with `reuse_requirement` rejected; investigation
  `open→active` flip on an unbound first Run.
- **Builder resolution**: an unbound Run builds (builder resolved from `target_kind`, no System
  touched).
- **`runs.bind`**: success; kind mismatch; already-bound; terminal Run; one-Run-per-System;
  reuse assertion; concurrent double-bind (CAS) and two Runs racing for one System
  (`tests/adversarial/`, hypothesis).
- **Guards**: `install`/`boot` of an unbound Run at the MCP boundary and at the worker handler.
- **`runs.cancel`**: cancel an unbound `created`/`running` Run.
- **Discovery**: `systems.list`/`inventory.list` expose `kind`.

## Out of scope

- The build-host failures of #552 and artifact-declaration ergonomics of #551 (per the issue).
- Changing how *bound* Runs behave — the change is additive.
- Re-binding: a Run binds once; there is no unbind/rebind.
