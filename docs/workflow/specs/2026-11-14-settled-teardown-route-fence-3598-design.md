# Settled-System teardown route fence in the database (#3598)

## Problem

`resolve_external_boot_system_teardown_dispatch_binding` (latest body in
`src/kdive/db/schema/0167_unrouted_preparing_teardown_route.sql`) returns an authority route for a
System whose newest activation is settled when a `retired` authority row survives: settlement
(0194) supersedes only `allocating` and `current` rows, and a settled activation is `abandoned`,
which the first branch's `state <> 'torn_down'` admits. A server built before #3551 calls the
resolver without reading the settlement, so it enqueues an authority teardown whose finalize marks
the System `torn_down` while the worker's domain runs. ADR-0816's #3551 amendment guards this only
by upgrade order.

## Scope

Migration `0197_settled_teardown_route_fence.sql` re-creates the resolver with `CREATE OR REPLACE`,
copying the 0167 body and adding to each branch:

```sql
AND NOT EXISTS (
    SELECT 1 FROM public.external_boot_activation_settlements AS settlement
    WHERE settlement.activation_id = newest.id AND settlement.settled_at IS NOT NULL
)
```

The predicate keys on the newest activation only, so a newer activation after settlement routes
as before. The marker branch already excludes a settled activation (it needs `preparing`); the
predicate there states the fence rather than relying on that. `CREATE OR REPLACE` keeps the
signature, `SECURITY DEFINER`, `search_path`, owner and the 0147 grants (`EXECUTE` to
`kdive_server` only). The owner is the migration role, which owns the settlements table.

No Python change: `_enqueue_authority_teardown` (`src/kdive/mcp/tools/lifecycle/systems/admin.py`)
already refuses with `external_boot_teardown_authority_unresolved` when the resolver returns no
row, and the current server reads the settlement before calling it.

Docs: an appended ADR-0816 amendment under Consequences, and the runbook's upgrade paragraph.

Non-goals (approved): Python routing changes; superseding `retired` rows at settlement; the
lease-expiry fence (#3599).

### Failure model

1. Actors and deployments: `kdive_server` processes of any build calling the resolver during a
   rolling upgrade; the migration role applying 0197.
2. Invariants: a settled System is never sent to the authority teardown by any server build; an
   unsettled System's route is unchanged; the resolver's grants.
3. Accepted: an older server refuses a settled System's teardown instead of routing it to the
   worker, so the operator still needs an upgraded server to tear it down. Bounded: a refusal,
   not a wrong teardown.
4. Covered elsewhere: an older reconciler expiring a settled allocation (#3599).

## Success

1. Settled newest activation with a `retired` authority row: the resolver returns no row.
2. Unsettled newest activation, including one with both evidence halves recorded but
   `settled_at` null: the resolver returns its route as before.
3. Resolver grants and `SECURITY DEFINER` are unchanged.

## Validation

Per-contract verification is in the plan's Task 1 and Task 2 inventories; Success 2's marker
branch is held by the existing unrouted-teardown tests.
