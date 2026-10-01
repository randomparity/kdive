# Teardown route for a never-authorized preparing activation (#3017) — design

Decision: [ADR-0620 amendment (2026-09-30, #3017)](../../adr/0620-authority-owned-system-teardown.md).

## Problem

`runs.boot` creates the activation `preparing` with a pending reservation and enqueues the activate
job; only that job's worker inserts an `external_boot_authorities` row. If the job is canceled,
fails before allocation, or never runs, `resolve_external_boot_system_teardown_dispatch_binding`
(0147) returns zero rows, and `_enqueue_authority_teardown` (`systems/admin.py`) refuses with
`external_boot_teardown_authority_unresolved`. `allocations.release` then refuses while the System
is not `torn_down`. Reachability is proven: `tests/integration/test_external_boot_unrouted_teardown.py`
drives `boot_run`, `cancel_job`, `teardown_system` and observes the refusal.

## Design

Migration `0167_unrouted_preparing_teardown_route.sql` replaces the resolver body (same signature,
owner and grants). It selects the newest activation for the System once, then returns the union of:

1. the existing route: if that activation is not `torn_down`, the newest `current` or `retired`
   authority row bound to it (unchanged);
2. the new route: if that activation is `preparing` and has no authority row in any state, one row
   per `boot` job whose `external_boot_authority_v1` marker has this `activation_id`, `run_id`,
   `system_id` and `plan_identity` and purpose `activate`, taking `provider_kind` and
   `authority_instance` from the marker. The job's state is not read, so a canceled, failed,
   queued or running activate job routes alike.

The branches are disjoint (one needs an authority row for the activation, the other needs none).
No Python change: `_enqueue_authority_teardown` already refuses unless exactly one row names the
newest activation, then builds the teardown marker through `build_external_boot_payload`, which
admits a plan-less `preparing` teardown (#2961). The worker path for that marker is unchanged.

## Failure model

1. **Actors and deployments** — a project ADMIN calling `systems.teardown`; a worker running the
   teardown job; the local-libvirt provider authority; a still-queued or running activate job.
2. **Invariants and assets at stake** — the pending reservation ends once and is never credited;
   no teardown is routed for an activation whose authority may own host state; the route is the
   durable one the server minted.
3. **Accepted failure classes**
   - an `allocating` or `superseded` authority row for the activation still refuses (approved
     exclusion, owner: operator);
   - two or more matching activate jobs still refuse (approved exclusion, owner: operator);
   - an activate job that allocates after the route is resolved: the teardown allocation
     supersedes it and fences it (0161); both reservation orderings are held by the 0147 receipt
     (#2961 design, Concurrency).
4. **Covered elsewhere** — remote-libvirt teardown needs retained PREP evidence (#3016); a
   released or expired allocation (#2992).

## Success

- After `boot_run`, with the activate job `canceled` or still `queued`, `systems.teardown`
  returns `queued` with a teardown marker whose `provider_kind` and
  `authority_instance` equal the activate marker's, and the reservation is still `pending`.
- With a second matching activate job, or an `allocating` or `superseded` authority row for the
  activation, `systems.teardown` still returns `external_boot_teardown_authority_unresolved`.
- The authority teardown of a `preparing` activation with no prior authority row deletes the
  pending reservation and writes no release row: the existing
  `test_teardown_of_a_preparing_activation_skips_preparation[pending...]`, whose seed has no
  authority row.

## Validation

- `focused-test`: `tests/integration/test_external_boot_unrouted_teardown.py` — the route arms
  (red before 0167: `external_boot_teardown_authority_unresolved`) and the refusal arms.
- `focused-test`: migration ledgers in `tests/db/test_migrate.py` and the three
  `test_migration_0*` files gain `0167` (red: the applied-version list mismatch).
- `task-test-not-applicable`: ADR-0620 amendment — prose; `just records` checks its shape.
- Gates: `just lint`, `just type`, focused `just test-verbose`, pre-push `just ci`.
