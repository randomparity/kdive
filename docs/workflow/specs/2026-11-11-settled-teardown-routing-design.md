# Settled teardown routing and the stranded-activation recovery tool

Issue #3551, part of #3520. Decision record: the 2026-11-11 amendment to
[ADR-0816](../../adr/0816-stranded-activation-settlement.md).

## Problem

Settling a stranded activation (#3550) with evidence from both owners (#3574) frees nothing. The
worker still refuses an ordinary teardown for any activation history (#2966), `systems.teardown`
and `ops.force_teardown` still route the System to the authority, and no MCP tool calls the
settlement entries. ADR-0816 also names two hazards this change must close: reconciler lease expiry
reads only the admission guard, which settlement lifts, and the authority teardown binding still
returns a route for a `retired` authority row.

## The settled key

One predicate, on `ExternalBootActivationRepository`:

- `settled_at(conn, activation_id) -> datetime | None`: the settlement row's `settled_at`, or
  `None`; the existing `is_settled` is defined on it. The lease-expiry query,
  `settled_system_awaiting_teardown`, lives in `kdive.services.external_boot.settlement`.

Migration 0194 already grants `SELECT (activation_id, system_id, settled_at)` to `kdive_worker`
and `kdive_reconciler`, and the whole table to `kdive_server`, so no migration is needed.

A settled row implies the worker-owned shape: settlement refuses `authority_owned` while an
`authority_system_ownership` row exists, and that row is only registered when a System is
provisioned. Every check reads the System's **latest** activation (`get_latest_for_system`), so a
new activation after settlement (ADR-0816: `runs.boot` is admitted again) puts the System back
behind every fence.

## Components

**Worker** (`jobs/handlers/systems.py`, `teardown_handler`). The #2966 refusal is raised unless the
latest activation is settled. Its message, details and terminal category are unchanged for every
other activation history. A settled System takes the ordinary path: provider teardown of
`systems.domain_name` on the worker's own daemon, then `tearing_down -> torn_down`.

**Routing** (`mcp/tools/lifecycle/systems/admin.py`, `route_external_boot_teardown`). After reading
the latest activation and **before** `_enqueue_authority_teardown` (which calls
`resolve_external_boot_system_teardown_dispatch_binding`), a settled activation goes to
`_route_settled_teardown`. Both `systems.teardown` and `ops.force_teardown` call this function, so
both route the same way.

`_route_settled_teardown` returns `None`, letting each caller's ordinary path run, unless the prior
`{system}:teardown` row is authority-marked (`external_boot_authority_v1` or
`authority_system_v1`). The ordinary paths differ on such a row: `systems.teardown` recycles a
`failed` one into an ordinary payload, while `enqueue_control_teardown` replays it unchanged. So
for an authority-marked prior row, on a System that is neither `torn_down` nor `reprovisioning`:

1. A `queued` or `running` row is replayed as its job envelope. So is a `canceled` row a worker
   claimed whose `updated_at` is not earlier than the activation's `settled_at`: that attempt may
   still be running, as `_is_settled_ordinary_teardown` reasons for the inverse case. A claimed,
   canceled row last written before settlement is not: settlement's authority evidence proved the
   authority lane quiescent after it (the #3574 amendment), and `jobs.cancel` never clears
   `worker_id`, so waiting would never release it.
2. Otherwise the admission matrix runs (`ordinary_teardown_denial`) and the row is replaced with an
   ordinary `TeardownPayload` under `JobRecyclePolicy.TERMINAL_OR_CANCELED`. A replacement is
   audited as a recycle when the caller audits recycles, and the idempotency key is recorded unless
   it already names the same row (`_key_names_job`), as the inverse replacement does.

A `torn_down` or `reprovisioning` System returns `None`, so each caller's own handling of those
states answers.

**Stalled teardown** (`reconciler/repairs/systems.py`, the stalled `tearing_down` repair). A
settled System's ordinary teardown can dead-letter after setting `tearing_down`. The repair treats
a settled latest activation as no activation and requeues the ordinary job, as for any worker
System; an unsettled history keeps its warning.

**Lease expiry** (`reconciler/repairs/allocations.py`, `_expire_one`). After the admission guard,
`settled_system_awaiting_teardown(conn, allocation_id)` names a System of the allocation that is not
`torn_down` and has a settled activation. If it returns one, the allocation is not expired, with a
warning naming the System, and the pass retries. The release fence (`_require_system_teardown`)
already holds such an allocation until the System is `torn_down`, so `release.py` is unchanged.
The predicate matches any settled activation, not only the latest: a newer restricting activation
is refused by the admission guard anyway, and the wider match keeps the allocation held.

**Tool** `ops.recover_stranded_activation(activation_id, step, reason)` in
`mcp/tools/ops/security/breakglass.py`, `platform_admin`, destructive annotation. `step` is
`observe` or `settle`; `reason` is required for both (non-blank, at most 1024 bytes, no NUL, the
settlement function's bound).

1. A role denial calls `audit_platform_denial` and returns `ToolResponse.denied`.
2. A malformed `activation_id` or a blank or oversized `reason` returns the configuration error
   envelope. An unknown activation is the `not_found` refusal of either step.
3. `observe` writes a `platform_audit_log` row (scope `observe:<activation>`) in its own committed
   transaction, then calls `request_stranded_observation`. It returns the job envelope, or the
   request's refusal with `details["reason"]` unchanged (`not_found`, `authority_owned`,
   `not_preparing`, `external_boot_restricted`). The next action is `step="settle"`, because
   `jobs.wait` answers `not_found` for this kind.
4. `settle` resolves the activation (`not_found` if absent), then, in one transaction under the
   System lock that `request_stranded_observation` and the settlement function also take, reads
   `read_stranded_observation` and calls `settle_stranded_activation` with
   `actor_for(ctx)` only when the observation job `succeeded`; otherwise it refuses:
   - no observation: `observation_missing`;
   - `queued` or `running`: `observation_pending`, naming the job;
   - `failed` or `canceled`: the job's own reason unchanged, or `observation_failed` /
     `observation_canceled` when the job recorded none (`authority_objects_present`,
     `stranded_observation_unsupported`, `authority_domain_present`, `worker_domain_absent`, ...)
     with its message, which for held objects names the recovery reference and objects.
   A primitive refusal (`evidence_stale`, `job_live`, `objects_unproven`, ...) is returned with its
   reason unchanged. `applied` and `replayed` return `settled` with the System id and next actions
   `ops.force_teardown`, `systems.get`.
5. Every refusal of either step writes one `platform_audit_log` row with scope
   `refused:<reason>:<activation>`. A configuration error (item 2) is not a refusal and is not
   audited, as in `ops.force_teardown`. A settlement writes only the primitive's own `settled:` row, so
   an `applied` call is recorded once and a `replayed` call adds nothing.

Settling only on a `succeeded` latest observation keeps the #3574 rule that an operator who reads a
refusal must not settle on earlier evidence: a failed rerun leaves the earlier records standing for
their hour, and the tool will not use them. Holding the System lock across the read and the
settlement stops a concurrent `observe` from recycling the job in between. A stale or refused settlement is retried by running
`observe` again, which recycles the terminal job.

The tool settles and stops. Teardown stays with `ops.force_teardown` or `systems.teardown`, which
already carry their own reason, audit and idempotency, and then `allocations.release` or
`ops.force_release` ends the allocation once the System is `torn_down`.

Docs: the tool docstring, generated `docs/guide/reference/ops.md` and its index, the hand-written
ops toolset and RBAC pages with their packaged resource copies, the `ops.force_teardown` docstring
(which says every activation history routes to the authority), and the exposure map. The coverage
campaign gets the tool's obligation under #3552, which owns the live proof.

## Upgrade order

The tool is the only caller of `request_stranded_observation`, and it ships in the server. The order
is ADR-0816's with the reconciler added before the servers: migrate, then the authority host, then
every worker and the reconciler, then servers. No server exposes the tool until every worker can
parse the job kind and the reconciler holds a settled allocation. The tool must not be used until
every server is upgraded: an old server still routes a settled System to the authority, which
settlement leaves a `retired` route for.

The worker narrowing ships with the workers, so it precedes any settlement. An old worker reached by
a settled System's ordinary teardown refuses with #2966's reason and the job fails closed; a rerun
recycles it.

The reconciler fence ships with the reconciler, before any server: an old reconciler would expire
a settled allocation and free its capacity while the worker domain still runs.

## Failure model

1. **Actors and deployments**
   - `platform_admin` through the tool and `ops.force_teardown`; a project admin through
     `systems.teardown`; the reconciler; the fixed worker.
2. **Invariants and assets at stake**
   - Every activation history except a latest settled one keeps the #2966 refusal and the authority
     route.
   - No authority teardown runs for a settled activation: routing reads the settlement before the
     binding.
   - An allocation with a settled, not-torn-down System stays held by release and by lease expiry.
   - `platform_audit_log` holds every role denial, observe request, settle refusal and
     settlement, and every refusal of either step with its reason.
3. **Accepted failure classes**
   - An authority-marked teardown canceled after a worker claimed it, and written after
     settlement, is replayed. Only an old server can write one; the upgrade order forbids using the
     tool before every server is upgraded.
   - An old server still routes a settled System to the authority over its `retired` row, and
     an authority teardown that finalizes would mark the System `torn_down` while the worker
     domain runs. Only the upgrade order closes it; a database guard in the binding would need a
     migration replacing that function, a cost this change does not take.
   - A settled System whose worker is gone keeps its allocation held; recovery is an operator
     action (excluded: ownership rewrite or manual SQL).
   - Lease expiry for a completed, unsettled external-boot activation is unchanged by this change.
4. **Covered elsewhere**
   - Runbook, a real stranded-shape test and live proof: #3552.
   - Producers for remote-libvirt and removal of held objects: earlier follow-ups of #3574.

## Threat model

1. **Boundary inventory.** Added: one `platform_admin` MCP tool. Widened: the worker's ordinary
   teardown admits a settled activation; `systems.teardown` (project `admin`) and
   `ops.force_teardown` reach the ordinary path for it; the reconciler's stalled repair requeues
   its ordinary teardown and lease expiry holds its allocation.
2. **Actor model.** A project admin is trusted for their own System but cannot settle; only the
   settlement record, written by `kdive_server` under `platform_admin` on both owners' evidence,
   opens the ordinary path. A worker cannot write `settled_at`.
3. **Control per boundary.** Tool: `require_platform_role(PLATFORM_ADMIN)`, audited denial,
   bounded reason, audited outcomes. Worker and reconciler: read-only column grants from 0194.
4. **Explicitly out of scope.** A compromised `kdive_server` credential, which can already settle.

## Success

- A settled, worker-owned System: `systems.teardown` and `ops.force_teardown` enqueue an ordinary
  teardown, including over a failed authority-marked prior row and a claimed, canceled one written
  before settlement, and the worker handler tears it down to `torn_down` (real Postgres, fake
  provider).
- The stalled `tearing_down` repair requeues a settled System's dead-lettered ordinary teardown.
- An unsettled `preparing`, `active` or completed activation keeps the authority route and the
  worker refusal, and a newer activation after a settled one restores both.
- A live authority-marked prior row, or a claimed, canceled one written after settlement, replays
  rather than being replaced.
- `_expire_one` keeps an allocation with a settled, not-torn-down System, and expires it once the
  System is `torn_down`.
- The tool: denial audited and `denied`; each settle refusal audited with its reason and returned
  unchanged, including `authority_objects_present` and `stranded_observation_unsupported`; settle
  only on a succeeded observation; `applied` audited once by the primitive.
