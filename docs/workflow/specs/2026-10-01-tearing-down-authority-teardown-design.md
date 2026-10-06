# The authority teardown finishes a `tearing_down` System with external-boot history (#3026)

## Scope and authority

Campaign 7f0e9c1da374, issue #3026, token `q3026-44421864`, cycle 2. Operator decision
(2026-10-01): the schema exit. The campaign assigned migration 0168. Approved exclusions: the
worker refusal of an unmarked teardown (operator); the released/expired Allocation fence (#2992);
force-close (#3025, done); break-glass release wording (#3047, done). The decision is a dated
amendment to [ADR-0620](../../adr/0620-authority-owned-system-teardown.md).

## Problem

A pre-#2966 ordinary teardown could move a System with a completed activation to `tearing_down`
and then fail. Since #2966, only the authority teardown may finish such a System. Four places on
that path list the System states admitted for purpose `teardown`, and none of them holds
`tearing_down`:

- `allocate_external_boot_authority` (0147, 0161);
- `finalize_external_boot_authority_teardown`, the success receipt (0147, 0149);
- `commit_external_boot_authority_result`, the failure commit (0160);
- `_teardown_prerequisites` in `src/kdive/jobs/handlers/external_boot/lifecycle.py`.

`systems.teardown` (and `ops.force_teardown`, which shares the route) replaces the refused
ordinary job with an authority-marked one. The worker refuses that job at `_teardown_prerequisites`
before it allocates. The worker never writes a marked job's row (`_MARKED_JOB_LEFT_RUNNING`,
`jobs/worker.py`), so the row stays `running`. A lapsed lease re-claims it while attempts remain,
and after that it sits `running` with `attempt >= max_attempts`. The System never leaves
`tearing_down`.

## Design

1. Migration `0168_tearing_down_authority_teardown.sql` patches the three SQL functions in one
   `DO` block. In each, the literal `'paused', 'crashing', 'crashed', 'failed'` occurs exactly
   once, inside the purpose-`teardown` System-state list. This was verified with
   `pg_get_functiondef` on a database migrated through 0167. The block asserts that one
   occurrence, and the absence of `'tearing_down'`, in each definition. It then appends
   `, 'tearing_down'` to the literal and re-executes the definition; `CREATE OR REPLACE` keeps
   owner and grants. No other predicate changes. That includes the activation-state list, the
   newest-activation check, and the allocator's `v_allocation.state <> 'active'` fence.
2. `_teardown_prerequisites` adds `"tearing_down"` to its admitted set.
3. Edge: the success finalizer writes `systems.state = 'torn_down'` and an `audit_log` transition
   of `v_system.state || '->torn_down'`, both in the receipt transaction. For this System that is
   `tearing_down->torn_down`, an edge `SystemState` already has (`domain/capacity/state.py:275`).
4. Re-run: an existing marked row resumes on its own when a lapsed lease re-claims it. An
   exhausted row is handled by `systems.teardown`. Its existing `final_attempt_running` branch
   and `FAILED_OR_LAPSED_EXHAUSTED` recycle (`mcp/tools/lifecycle/systems/admin.py`) requeue the
   same row with the same marker, and that run now succeeds.
5. `repair_stalled_tearing_down_systems` warns once per System per cause, holding a
   `system_id -> cause` map. There are three causes:
   - external-boot history: the existing warning, now naming `systems.teardown` and the runbook;
   - a non-active prior row with an `external_boot_authority_v1` marker: re-run `systems.teardown`;
   - a non-active prior row with an `authority_system_v1` marker: no supported exit.
   Each pass first drops every key that is not in that pass's candidates. Because the candidates
   are a subset of the `tearing_down` Systems, a System that leaves `tearing_down` is dropped.
6. The runbook (`docs/operating/runbooks/stuck-tearing-down-system.md`) names `systems.teardown`
   and `ops.force_teardown` as the exit while the Allocation is `active`, and `allocations.renew`
   for a lease near expiry. It keeps the confirm-the-symptom SQL, and it shows how to read a
   marked `running` row.

## Failure model

1. Actors and deployments
   - A project admin (`systems.teardown`) or platform admin (`ops.force_teardown`) over MCP,
     against local or remote libvirt with a provider-host authority (ADR-0584).
   - The worker that claims the marked TEARDOWN job; the reconciler.
2. Invariants and assets at stake
   - The reservation credits once. The receipt is keyed by `root_authority_id`, and a replay
     returns `applied` without writing. After `torn_down`, the allocator admits no further
     teardown.
   - No authority is allocated on an Allocation that is not `active` (the #2992 fence, unchanged).
   - The System reaches `torn_down` only in the receipt transaction that retires the authority.
3. Accepted failure classes
   - An `expired` or `released` Allocation. The allocator answers `superseded`, so no authority
     row is written and no state changes. The marked row stays `running` until its attempts are
     exhausted. Accepted because nothing is mutated; the exit for this class is #2992's.
   - A marked row that is `running`. The lane does not log it, because it excludes every
     `queued`/`running`/`canceled` teardown row by design (#2370). Such a row exists only after
     an operator ran `systems.teardown`. The runbook query finds it.
   - The authority route is unresolved (`external_boot_teardown_authority_unresolved`). This is
     already accepted for every state in the #2966 amendment.
   - The pre-fix ordinary provisioner already removed the domain. The authority teardown
     observes `absent` and completes. If it cannot prove absence, it quarantines (#2917).
   - A `tearing_down` System with an authority-System binding and no activation. Its 0149
     finalizer admits only `provisioning`, `ready` and `failed`. It is not this class, which
     needs an activation. Design 5 logs it as having no supported exit, and it is reported as a
     follow-up candidate.
4. Covered elsewhere
   - Teardown after release or expiry: #2992.

## Success

- On the database:
  - A `tearing_down` System's teardown allocates.
  - Its `complete_ready` finalize returns `applied` twice and writes one release row and one
    `tearing_down->torn_down` audit row.
  - Its failure commit returns `applied`/`queued` and leaves the System in `tearing_down`.
  - On an `expired` Allocation, the same allocation returns `superseded` and writes no
    authority row.
- End to end, for a `tearing_down` System with a `preparing` activation, an `active` Allocation,
  and a failed unmarked `{system}:teardown` row:
  - `systems.teardown` returns that row `queued`, now authority-marked.
  - After that row is set `running`, exhausted and lease-lapsed, `systems.teardown` returns the
    same row `queued` again.
  - One worker pass succeeds it. The System and activation are `torn_down`, no reservation or
    release row remains, and the audit row reads `tearing_down->torn_down`.
- Two passes over a `failed` row with an `external_boot_authority_v1` marker log one WARNING
  naming `systems.teardown`. An `authority_system_v1` marker logs one WARNING naming no exit.
  A System that leaves `tearing_down` is absent from the map after the next pass.

## Validation

See the plan, `docs/workflow/plans/2026-10-01-tearing-down-authority-teardown.md`.
