# Shared System teardown policy

## Problem

Break-glass teardown imports transaction routing from the ordinary MCP adapter.
That routing combines authority and settled-job policy with MCP response construction.

## Scope and ownership

Move route_external_boot_teardown, its settled-route and authority-enqueue helpers,
written-since query, authority-marker and settled-ordinary predicates into one concrete
services/systems/teardown.py. Move their two SQL constants and marker constant.
Reuse the existing service-owned teardown key expression; retain the adapter's
reprovision/ordinary key use. Stateless activation repositories may remain per owner.
Do not move restore, devices, reprovision or ordinary-only preactivation teardown.

The service accepts connection, System, Authorizing and optional resolver. Return
Job or None. A concrete TeardownRoutingDenied wraps only deliberate pre-write routing
CategorizedError or admission ExternalBootDenied. Adapters catch only this exception;
recorder, audit, serialization and replay errors propagate to roll back the transaction.
No ToolResponse, RequestContext or MCP imports.
Use direct queue.get_by_dedup_key where the old dedup probe used NEVER, preserving
its exact behavior without moving general idempotency helpers.

## Callers and transactions

Both adapters retain their current connection/transaction/System-lock scopes, state
checks, authorization and response shaping. Ordinary routing remains before keyed replay;
break-glass keeps terminal/reprovision checks before routing and its separately committed
platform audit. Both turn returned Job into the existing job envelope.

Ordinary routing passes its recycle audit callback and a typed transaction recorder.
The service awaits them at the existing audit and record points. The recorder receives
connection, Job and skip-matching-key flag: settled replacement skips a matching key;
authority replacement skips one only when replacing an ordinary row. The adapter renders
the same job envelope and uses its existing replay/record helpers, retaining persisted
bytes and rollback. Break-glass passes neither callback, matching audit_recycle=False
and absent key. No new result union, dispatcher, compatibility facade or persisted format.

Remove ordinary_teardown_denial; call the already service-owned admission matrix at
its existing caller positions. A small MCP common renderer handles routing errors:
ExternalBootDenied uses existing RBAC-filtered denial; other conflict routing failures
retain jobs.wait/systems.get, configuration failures retain systems.get. Preserve
detail/category/reason fields. Catch routing errors inside the existing transaction.

## Success

1. Shared routing/enqueue policy has one service owner; both adapters call it directly.
2. Authorization-before-disclosure, routing-before-replay, System locks, authority fencing,
   first-enqueuer identity, database-clock retry and settled cancellation rules are unchanged.
3. Enqueue, caller recycle audit and keyed recording still commit or roll back together;
   break-glass platform audit remains separately committed and has no project recycle audit.
4. Complete ordinary/breakglass response and keyed replay behavior remain unchanged.

## Failure model

- Actors: project admins and platform admins enqueue teardown while workers, reprovision,
  authority takeover and repeated client calls may race.
- Invariants: current authority cannot replay a fenced ordinary job; a running or possibly
  running attempt cannot be recycled prematurely; authorization precedes hidden-state output.
- Accepted: private module/function locations and error stack frames change; no public
  response, database format or policy change is authorized.
- Covered elsewhere: queue fencing and database clocks, authority payload validation and
  admission matrix policy remain existing owners and retain their tests.

## Validation

Mode: focused-test for a service ownership boundary: prove a missing new module and
current breakglass-to-admin import fail, then require a service with no MCP imports.
Mode: task-test-not-applicable for unchanged routing decisions; inventory every moved
branch, SQL and enqueue argument and compare before/after ASTs with only neutral
result/error/callback substitutions. Add baseline-green transaction regression cases
for RuntimeError and categorized recording failures on fresh/recycled authority jobs,
verifying queue, audit and key
rollback. Existing real-DB ordinary, breakglass, settled routing, recycle, keyed replay
and stranded settlement tests cover caller differences. Controlled audit/record callback,
fencing/order and response faults must bite. Run lint, whole-tree types, relevant
coverage/serialization inventories, docs and hooks; no live qualification is claimed.
