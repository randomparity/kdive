# The authority teardown finishes a `tearing_down` System with external-boot history (#3026)

## Scope and authority

Campaign 7f0e9c1da374, issue #3026, token `q3026-44421864`, cycle 2. Operator decision
(2026-10-01): the schema exit. Migration number 0168 was assigned by the campaign. Approved
exclusions: the worker refusal of an unmarked teardown (operator); the released/expired
Allocation fence (#2992); force-close (#3025, done); break-glass release wording (#3047, done).
The decision is a dated amendment to [ADR-0620](../../adr/0620-authority-owned-system-teardown.md).

## Problem

A pre-#2966 ordinary teardown could move a System with a completed activation to `tearing_down`
and then fail. Since #2966, only the authority teardown may finish such a System. The authority
path, however, lists the System states it admits for purpose `teardown` in four places. None of
them holds `tearing_down`:

- `allocate_external_boot_authority` (0147, 0161);
- `finalize_external_boot_authority_teardown`, the success receipt (0147, 0149);
- `commit_external_boot_authority_result`, the failure commit (0160);
- `_teardown_prerequisites` in `src/kdive/jobs/handlers/external_boot/lifecycle.py`.

`systems.teardown` (and `ops.force_teardown`, which shares its route) replaces the refused
ordinary job with an authority-marked one. The worker then refuses it at `_teardown_prerequisites`
as a terminal `configuration_error`, before allocating. The System never leaves `tearing_down`,
and `repair_stalled_tearing_down_systems` skips the marked row without logging anything.

## Design

1. Migration `0168_tearing_down_authority_teardown.sql` patches the three SQL functions in one
   `DO` block. Each function carries the literal `'paused', 'crashing', 'crashed', 'failed'`
   exactly once, and only in its purpose-`teardown` System-state list (verified with
   `pg_get_functiondef` on a database migrated through 0167). The block asserts one occurrence
   and no `'tearing_down'` in each definition, then replaces the literal with
   `'paused', 'crashing', 'crashed', 'failed', 'tearing_down'` and re-executes the definition.
   `CREATE OR REPLACE` keeps owner and grants. No other predicate changes: the activation-state
   list, the newest-activation check, and the allocator's `v_allocation.state <> 'active'` fence
   stay as they are.
2. `_teardown_prerequisites` adds `"tearing_down"` to its admitted set.
3. The edge taken is `tearing_down -> torn_down`. The success finalizer writes
   `systems.state = 'torn_down'` and an `audit_log` row whose transition is
   `v_system.state || '->torn_down'`, in the receipt transaction. `SystemState.TEARING_DOWN`
   already has that edge (`domain/capacity/state.py:275`).
4. `repair_stalled_tearing_down_systems` warns once per System per cause. The causes are
   external-boot history (the existing warning, which now names the supported exit) and a
   non-active authority-marked prior job (a new warning). The warned map holds
   `system_id -> cause`. Each pass first drops every key that is not among that pass's
   candidates, and the candidates are a subset of the `tearing_down` Systems, so a System that
   leaves `tearing_down` is dropped. A System that returns to the candidate set warns again.
5. The runbook `docs/operating/runbooks/stuck-tearing-down-system.md` names `systems.teardown`
   (project admin) and `ops.force_teardown` (platform admin) as the exit while the Allocation is
   `active`. It names `allocations.renew` to keep a near-expiry lease open, and it keeps the
   confirm-the-symptom SQL.

## Failure model

1. Actors and deployments
   - A project admin calling `systems.teardown`, or a platform admin calling `ops.force_teardown`,
     over MCP, against local or remote libvirt with a provider-host authority (ADR-0584).
   - A worker that claims the authority-marked TEARDOWN job; the reconciler.
2. Invariants and assets at stake
   - The reservation credits once: the receipt is keyed by `root_authority_id`, and a replay
     returns `applied` with no second write. After `torn_down`, the allocator refuses any further
     teardown, because `torn_down` is not admitted.
   - No authority is allocated on a non-`active` Allocation (#2992's fence, unchanged).
   - The System reaches `torn_down` only in the receipt transaction that retires the authority.
3. Accepted failure classes
   - The Allocation is already `expired` or `released`. The allocator answers `superseded`, so no
     authority row is written and no state changes. The job retries `stale_handle` up to its
     `max_attempts` and then fails, the same as any other System on an expired Allocation. This is
     accepted because nothing is mutated; the end of that class is #2992's.
   - The authority route cannot be resolved (`external_boot_teardown_authority_unresolved`).
     Accepted, as it already is for every state in the #2966 amendment.
   - A domain the pre-fix ordinary provisioner already destroyed. The authority teardown observes
     it `absent` and completes; when it cannot prove absence it quarantines (#2917 amendment).
4. Covered elsewhere
   - Teardown after release or expiry: #2992.
   - A `tearing_down` System that has an authority-System binding and no activation: its
     finalizer (0149) admits only `provisioning`, `ready` and `failed`. It is not this failure
     class, which needs an activation row. Design item 4 makes it visible in the log.

## Success

- `systems.teardown` on a `tearing_down` System that has a `preparing` activation, an `active`
  Allocation, and a failed unmarked `{system}:teardown` job returns `queued` with an authority
  marker. One worker pass succeeds that job, the System and activation are `torn_down`, the
  pending reservation is gone with no release row, and the one `audit_log` row reads
  `tearing_down->torn_down`.
- On the database, a `tearing_down` System's teardown allocates, and its `complete_ready`
  finalize returns `applied` twice with exactly one release row. Its failure commit returns
  `applied`/`queued` and leaves the System `tearing_down`.
- The same allocation on an `expired` Allocation returns `superseded` and writes no authority row.
- Two passes over a marked failed row log one WARNING. A System that leaves `tearing_down` is
  absent from the warned map after the next pass.

## Validation

See the plan, `docs/workflow/plans/2026-10-01-tearing-down-authority-teardown.md`.
