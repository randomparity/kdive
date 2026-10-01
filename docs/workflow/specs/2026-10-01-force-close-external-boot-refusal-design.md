# `investigations.close force=true` refuses a System with external-boot history (#3025)

## Scope and authority

Campaign 7f0e9c1da374, issue #3025, token `q3025-00c3e0e0`. Operator decision (2026-10-01):
refuse, not route. Approved exclusions: the worker-side refusal (operator); the orphaned-System
lane and reconciler producers (#3015); the `tearing_down` exit (#3026); break-glass release
detail wording (#3047). The decision is a dated amendment to
[ADR-0620](../../adr/0620-authority-owned-system-teardown.md). No new ADR, no migration, no
`ProviderResolver` in `JobOperations`, and the #3007 route helper stays in
`mcp/tools/lifecycle/systems/admin.py`.

## Problem

`_couple_bound_systems` in `src/kdive/services/investigations/lifecycle.py` calls
`jobs.enqueue_teardown` for every bound live System when `force=True`. For a System with any
external-boot activation, `JobOperations.enqueue_teardown` (`src/kdive/jobs/service_operations.py`)
reaches `enqueue_control_teardown`, which enqueues an unmarked `{uid}:teardown` job.
`teardown_handler` (`src/kdive/jobs/handlers/systems.py`) refuses that job as a terminal `conflict`
(`external_boot_teardown_not_supported`) since #2966. The close commits; the promised teardown
never happens, and the System stays bound to a closed Investigation until an admin runs
`systems.teardown`.

## Design

1. `InvestigationErrorReason` (`src/kdive/services/investigations/common.py`) gains
   `EXTERNAL_BOOT_TEARDOWN_REQUIRED = "external_boot_system_teardown_required"`, the same reason
   string `allocations.release` uses for the same condition.
2. `_couple_bound_systems`, on the `force=True` path, after the admin gate and the existing
   reprovisioning refusal and before the enqueue loop, reads
   `ExternalBootActivationRepository().get_latest_for_system(conn, system_id)` for each bound live
   System in id order. If any return a row, it raises `InvestigationServiceError(object_id=<inv>,
   reason=EXTERNAL_BOOT_TEARDOWN_REQUIRED, detail=..., data={"external_boot_systems": [ids]})`.
   The detail names each System id and `systems.teardown`.
3. The read runs inside `_close_locked`'s transaction, after it took each bound live System's
   SYSTEM lock and then the INVESTIGATION lock, so the lock order is unchanged. An activation is
   created under the System lock (`runs.boot`), so the read cannot race a new activation for a
   locked System. The raise precedes every write in the close (job enqueue, state change,
   summary, cleanup marks, audit row), so the transaction rolls back with nothing written.
4. A System with no activation row, including a pre-first-activation authority System, is not
   refused and keeps its current route through `enqueue_control_teardown` (the preactivation
   teardown for an authority binding). This matches `enqueue_control_teardown` and the #3007
   break-glass path, which both key the external-boot route on an activation row.
5. `close_investigation` (`src/kdive/mcp/tools/lifecycle/investigations/lifecycle.py`) maps the
   new reason to `ToolResponse.failure(<inv>, CONFLICT, detail=exc.detail, data={"reason":
   "external_boot_system_teardown_required", "external_boot_systems": [...]},
   suggested_next_actions=["systems.teardown", "systems.get"])`. `conflict` matches the release
   refusal for the same condition. The other reasons keep `investigation_error_response`.
6. The `investigations.close` wrapper docstring and `force` field text
   (`src/kdive/mcp/tools/lifecycle/investigations/registrar.py`) state that `force` refuses when a
   bound System has external-boot history, and that `systems.teardown` tears such a System down.
7. ADR-0620 gets one dated amendment: force-close refuses such a System; the #2966 amendment's
   producer list now holds only the orphaned-System lane. The refusal sits in the force-close
   producer, not in `enqueue_control_teardown`, so the orphaned lane is unaffected. The earlier
   objection to refusing (it rolls back a close an admin could tear down afterwards) does not hold
   here: `force=True` already requires project `admin`, so the refused caller can run
   `systems.teardown` itself and then close.

The default close (`force=False`) is unchanged: it already refuses any bound live System.

## Failure model

1. Actors and deployments
   - A project admin calling `investigations.close force=true` over MCP, in any deployment
     (server role).
2. Invariants and assets at stake
   - The close is all-or-nothing: on a refusal, no job, no state change, no audit row.
   - SYSTEM-then-INVESTIGATION lock order (deadlock freedom with run create and reprovision).
   - An authority-owned host domain is never handed to the ordinary worker path from this producer.
   - The public `investigations.close` error contract (category, `data.reason`, next actions).
3. Accepted failure classes
   - Unmarked teardown jobs that force-close enqueued before this change still fail on the worker.
     Accepted: the worker refusal and the `systems.teardown` recycle (#2966 amendment) already
     handle them.
   - A System whose `systems.teardown` cannot resolve an authority route keeps the Investigation
     open. Accepted: the close was not deliverable either way; the refusal names the tool whose
     own typed error explains why.
4. Covered elsewhere
   - Orphaned-System lane and reconciler producers: #3015.
   - The `tearing_down` exit: #3026.
   - Worker-side refusal: operator.

## Testing

DB-backed, in `tests/mcp/lifecycle/test_investigations_tools.py`:

- A bound System with a completed (abandoned, cleanup-complete) activation and a second ordinary
  bound System: `force=True` returns `conflict` with `data.reason`
  `external_boot_system_teardown_required`, the System id in `data.external_boot_systems` and
  the detail, `systems.teardown` in next actions. No teardown job, Investigation `open`, both
  cleanup marks null, no `investigations.close` audit row.
- The same refusal for a restricting (`active`) activation.
- The existing `test_close_force_routes_each_system_by_authority_ownership` proves the
  pre-first-activation exemption; it must stay green unchanged.
- The wrapper contract test asserts the `force` field names `systems.teardown`.
