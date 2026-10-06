# `ops.force_teardown` routes external-boot history through the authority teardown (#3007)

## Scope and authority

Campaign 7f0e9c1da374, issue #3007, token `q3007-b1c463e6`. Operator-approved split: part (a)
only, break-glass `ops.force_teardown`. Approved exclusions: `investigations.close force=true`
routing (part b, #3025); changing the worker-side refusal (operator); admitting teardown on a
released allocation (operator); the `tearing_down` residue (#3015); canceled orphan visibility
(#3006). The decision is a dated amendment to
[ADR-0620](../../adr/0620-authority-owned-system-teardown.md), replacing its record that
break-glass teardown "still enqueue[s] an unmarked job". No new ADR, no migration.

## Problem

`_teardown_locked` in `src/kdive/mcp/tools/ops/security/breakglass.py` calls
`enqueue_control_teardown` for every non-terminal, non-reprovisioning System. For a System with any
external-boot activation, that enqueues an unmarked `{uid}:teardown` job, which `teardown_handler`
(`src/kdive/jobs/handlers/systems.py`) refuses as a terminal `conflict`
(`external_boot_teardown_not_supported`) since #2966. A re-run recycles the failed row
(`JobRecyclePolicy.FAILED`) into another refused job. `systems.teardown` instead reads the newest
activation and calls `_enqueue_authority_teardown` (`src/kdive/mcp/tools/lifecycle/systems/admin.py`),
and its ordinary path runs `check_external_boot_admission(..., SYSTEM_TEARDOWN, ...)`. The
operator's last-resort tool therefore cannot tear down a System the project tool can.

## Design

1. `admin.py` gains two public helpers, extracted from `systems.teardown`'s `_teardown_locked`
   without behavior change, and `_teardown_locked` calls them:
   - `route_external_boot_teardown(conn, ctx, system, system_id, idempotency_key, resolver) ->
     ToolResponse | None`: reads `get_latest_for_system`; `None` when the System has no activation,
     otherwise the result of `_enqueue_authority_teardown` (unchanged).
   - `ordinary_teardown_denial(conn, ctx, system, system_id) -> ToolResponse | None`: runs
     `check_external_boot_admission(conn, system.id, SYSTEM_TEARDOWN, project=system.project)` and
     renders an `ExternalBootDenied` through `_common.external_boot_denial`; `None` when admitted.
2. Break-glass `_teardown_locked`, under the same System advisory lock and transaction, keeps its
   existing order: stale-handle `config_error`, `torn_down` idempotent success, `reprovisioning`
   `conflict`. Then it calls `route_external_boot_teardown(conn, ctx, system, str(uid), None,
   resolver)` and returns its envelope when non-`None`. Otherwise, when
   `ordinary_mutation_is_fenced(conn, uid)` is false (no preactivation authority binding), it calls
   `ordinary_teardown_denial` and returns a denial. Then it calls `enqueue_control_teardown(...,
   recycle=FAILED)` as today. The fence check keeps `systems.teardown`'s order: the matrix refuses
   `SYSTEM_TEARDOWN` before an authority System's first activation, so a preactivation System goes
   straight to its preactivation teardown, as `test_force_teardown_routes_preactivation_authority_system`
   requires.
3. `force_teardown` gains a keyword `resolver: ProviderResolver | None = None`; `register()` passes
   its existing `resolver` through. A `None` resolver on the activation route returns the existing
   typed `configuration_error` (`external_boot_teardown_authority_unresolved`); it never falls back
   to an ordinary enqueue.
4. The authority route reuses `systems.teardown`'s replace/replay rules unchanged: a settled
   ordinary row (`failed`, or `canceled` with no `worker_id`) is replaced by the authority-marked
   job (`TERMINAL_OR_CANCELED`); a live or claimed ordinary row returns
   `ordinary_teardown_fenced_by_external_boot`; an identical-marker authority row replays or is
   re-run (`FAILED_OR_LAPSED_EXHAUSTED`). The job's `authorizing` is
   `authorizing(ctx, system.project)`, as break-glass already writes. No idempotency key is
   recorded (break-glass takes none).
5. Authorization and audit are unchanged: `require_platform_role(PLATFORM_ADMIN)`, non-blank
   `reason`, and the `platform_audit_log` row committed before the locked transaction.
6. `ops.force_teardown`'s wrapper docstring states that a System with external-boot history gets
   the authority-marked teardown, as `systems.teardown` does.

Rejected: a service in `services/systems/authority_owned.py` — `_enqueue_authority_teardown`
renders `ToolResponse` envelopes and records idempotency envelopes, and no `services/` module
imports `kdive.mcp`; moving it would add a result type and a mapping layer for one more caller.
Route in `enqueue_control_teardown` — ADR-0620 (#2966 amendment) rejected it because it needs a
`ProviderResolver` the reconciler lane and `JobOperations` do not carry; part (b) is #3025.

## Success

- S1: `ops.force_teardown` on a `ready` System with a completed (`recovered`, cleanup complete)
  activation and a `failed` ordinary `{uid}:teardown` row returns `queued` with the same job id,
  whose payload carries `external_boot_authority_v1.activation_id` equal to the activation.
- S2: with a `canceled` ordinary row claimed by a worker, it returns `conflict`
  (`ordinary_teardown_fenced_by_external_boot`) and the row is unchanged.
- S3: with no resolver, it returns `configuration_error`
  (`external_boot_teardown_authority_unresolved`) and enqueues no job.
- S4: on the ordinary path an `ExternalBootDenied` from the admission matrix returns its typed
  denial and enqueues no job.
- S5: every existing break-glass test and every `systems.teardown` test passes unchanged.
- S6: every case above writes exactly one `platform_audit_log` row.

## Failure model

1. Actors and deployments: a `platform_admin` principal calling `ops.force_teardown` over MCP
   HTTP, cross-project; the project `admin` calling `systems.teardown`; the worker running the
   queued job. Deployments: the server role against Postgres.
2. Invariants and assets: no unmarked teardown job is enqueued for a System whose newest
   activation row exists at the locked read; the platform audit row precedes every mechanic; the
   `{uid}:teardown` dedup row is the single teardown job for a System; `systems.teardown`
   behavior is unchanged.
3. Accepted classes: an activation created after the locked read is outside this change — the
   activation is created under the same System lock, and the worker refusal still covers a queued
   ordinary job (ADR-0620 #2966 amendment). A torn-down System with history returns `torn_down`
   success before the route, as today. A `reprovisioning` System with history returns break-glass's
   existing `conflict` before the route, where `systems.teardown` would route it; the operator
   retries once the reprovision settles, and that retry routes.
4. Covered elsewhere: investigation force-close and the orphaned-System lane still enqueue
   unmarked jobs (#3025, worker refusal); `tearing_down` residue (#3015); canceled orphans (#3006).

## Threat model

- Boundaries: none added. The existing platform-admin boundary now reaches
  `_enqueue_authority_teardown`, which `systems.teardown` reaches with project `admin`.
- Actors: a `platform_admin` operator (trusted, audited); a non-admin caller (denied and audited
  before any read, unchanged).
- Controls: `require_platform_role` before the System read; reason non-blank; audit row
  committed first; the System advisory lock spans read, route, and enqueue.
- Out of scope: an operator abusing break-glass — the audit row is the accountability control
  (ADR-0062 §4).
