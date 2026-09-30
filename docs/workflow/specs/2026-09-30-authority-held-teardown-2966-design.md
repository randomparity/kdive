# Ordinary teardown and release refuse an authority-held domain (#2966)

Decision record: [ADR-0620 amendment (2026-09-30, #2966)](../../adr/0620-authority-owned-system-teardown.md).

## Problem

ADR-0620 says a System with durable external-boot history is torn down only by an
authority-marked TEARDOWN job, because "a completed external release stops restricting
admission but does not transfer host ownership". The #2865 proof broke this:
`allocations.release` succeeded, the orphaned-System lane enqueued an unmarked teardown, and the
worker's ordinary teardown committed `torn_down` while the domain kept running on the authority
daemon. Three gaps allow it:

- `teardown_handler` (`src/kdive/jobs/handlers/systems.py`) refuses only while an activation
  *restricts* the System (`get_restricting_for_system`). After a clean release (`recovered` or
  `abandoned` with `cleanup_complete`) it runs the ordinary provisioner.
- `allocations.release` (`src/kdive/services/allocation/release.py`) refuses only a restricting
  activation. After release the authority teardown can never run, because
  `allocate_external_boot_authority` requires an `active` allocation
  (`0122_external_boot_authority.sql`, unchanged by 0147 and 0161).
- `systems.teardown` (`src/kdive/mcp/tools/lifecycle/systems/admin.py`) returns `conflict` when
  the `{system}:teardown` dedup row holds an ordinary job, so a refused job would block it.

## Scope

1. **Worker refusal.** `teardown_handler` refuses an unmarked teardown when the System has any
   external-boot activation (`get_latest_for_system`). It keeps the existing terminal
   `CategorizedError`: category `conflict`, `details.reason`
   `external_boot_teardown_not_supported`, `activation_id`, `activation_state`. The check runs
   under the System lock, before the `tearing_down` transition, snapshot reclaim, and the
   provisioner call. The message names `systems.teardown`.
2. **Release refusal.** `_release_locked` (`allocations.release`, break-glass release, host
   drain) and `reclaim_under_lock` (the orphaned-active reaper, which releases an allocation
   whose System is `failed` or idle `crashed`) call a new `_require_system_teardown` right after
   `guard_external_boot_release`. It raises `ExternalBootDenied` (category `conflict`,
   `details.reason` `external_boot_system_teardown_required`, `details.system_id`) while a System
   on the allocation has state other than `torn_down` and at least one activation row. The caller
   sees `released=False`, `conflict`, and those details; `ReleaseOutcome` carries no message or
   next actions. `guard_external_boot_release` does not change, so the expiry sweep, which calls
   it directly, keeps its behavior.
3. **Public recycle.** `_enqueue_authority_teardown` treats a prior job with neither
   `external_boot_authority_v1` nor `authority_system_v1` in its payload as *ordinary*. An
   ordinary prior in state `failed` or `canceled` is replaced by the authority-marked teardown
   with recycle policy `TERMINAL_OR_CANCELED`, entered only for those two states. An ordinary
   prior in any other state keeps the existing conflict.
4. **No change** to `enqueue_control_teardown` or the reconciler lanes.

## Failure model

1. **Actors and deployments** — a worker that claims TEARDOWN jobs; a project `admin` calling
   `systems.teardown`; a `contributor` or platform admin calling `allocations.release`; the
   reconciler. Deployments: local and remote libvirt with a provider-host authority (ADR-0584).
2. **Invariants and assets at stake** — no unmarked teardown job for a System with an activation
   row reports `succeeded`; the allocation of such a System stays `active` through
   `allocations.release` until the System is `torn_down`; a recycled job has no running attempt;
   the reservation credits once (ADR-0620).
3. **Accepted failure classes**
   - A System already in `tearing_down` with history (only from a pre-fix ordinary teardown),
     or one whose authority route is unresolved: the authority teardown cannot run, the release
     refusal holds the allocation, and only lease expiry ends it. For `tearing_down`,
     `repair_stalled_tearing_down_systems` also re-enqueues an unmarked job that the worker
     refuses each pass. No new System reaches `tearing_down` with history.
   - Break-glass release and host drain are refused for such an allocation; a platform operator
     outside the project cannot run `systems.teardown` (project `admin`), so the project admin
     clears it.
   - An ordinary prior in `succeeded` (the #2865 residue) still conflicts.
   - The recycled job keeps its original `authorizing` value, so the authority commit's audit row
     names the enqueuer of the refused job (for example the reconciler), not the admin.
   - A System reprovisioned after an activation still counts as history. ADR-0620 already routes
     it this way in `systems.teardown`.
4. **Covered elsewhere** — lease expiry of an allocation whose System has history: separate
   issue filed by the orchestrator. Carrier ledger, fixture removal, runbook: #2965. Reuse after
   release: #2968. Plan-less preparing teardown: #2961. Pre-activation authority-owned Systems:
   already routed and release-fenced (ADR-0623).

## Success

- An unmarked teardown for a System with at least one activation row fails `conflict`,
  terminal, with the System state unchanged and no provisioner call; a System with no activation
  row tears down as before.
- `allocations.release` for an allocation with a `ready` System that has a `recovered`,
  `cleanup_complete` activation returns `released=False`, `conflict`, reason
  `external_boot_system_teardown_required`, and the allocation state is unchanged. Once that
  System is `torn_down`, release succeeds. `reclaim_under_lock` for a `failed` System with the
  same history returns `released=False`, `conflict`.
- `systems.teardown` with a `failed` or `canceled` ordinary prior returns the same job id, `queued`, whose
  payload carries `external_boot_authority_v1` for the newest activation; a `queued` ordinary
  prior still returns `conflict`.

## Validation

The plan lists the focused tests: two handler cases and one recycle case in
`tests/mcp/lifecycle/test_systems_tools.py`, and three release cases in
`tests/services/external_boot/test_allocation_release.py`. Existing tests pin the unchanged
paths: `test_teardown_handler_destroys_and_sets_torn_down`,
`test_teardown_activation_fence_preempts_keyed_ordinary_replay`, and
`test_expiry_without_a_restricting_activation_logs_no_denial`.
