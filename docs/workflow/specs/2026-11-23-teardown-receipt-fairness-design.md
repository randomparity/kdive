# Fair selection for authority teardown receipts

## Problem and authority

A real PostgreSQL run with 101 receipts and failing cleanup on the first 100 returned zero
repairs in two passes. The healthy receipt remained unconsumed. The selector in migration
0149 orders by receipt time and ID; retaining a failed candidate leaves it at the front.
The user authorizes internal architecture and pre-release migrations while preserving public
APIs. The living queue selects last-retry ordering, recorded by [ADR-0838](../../adr/0838-authority-teardown-repair-retry-order.md).

## Scope and data

Add only nullable `authority_system_attempts.teardown_repair_last_retry_at timestamptz`.
No backfill: existing NULL rows use receipt time. New migration 0205 changes the installed
`list_authority_system_teardown_repairs(integer)` using an exact-once guarded replacement
of its ORDER BY. Preserve the installed function's signature, body apart from ordering,
owner, SECURITY DEFINER, empty search path, volatility, limit and ACL.
Order by `COALESCE(attempt.teardown_repair_last_retry_at, attempt.receipt_at)`, then
`attempt.receipt_at, attempt.id`. All existing eligibility predicates remain unchanged.

Add `mark_authority_system_teardown_repair_retry(uuid,bigint,uuid,bigint,text,text)`
returning boolean. Its parameters are authority ID, generation, System ID, journal sequence,
journal digest and receipt digest, exactly the current selector tuple. Validate non-null
IDs, positive generation/sequence and digest format as the existing finalizer does.
Require session membership in `kdive_reconciler`; revoke PUBLIC and other application roles,
grant only reconciler execution. Raw table privileges remain unchanged.

Under the existing `kdive:system:` advisory lock (hash seed 2125), lock ownership, exact
attempt, job and System in the finalizer's order. Return false for a missing/mismatched,
consumed or superseded candidate, wrong owner state/head, wrong terminal disposition/head,
System outside provisioning/ready/failed, or a live job lease on an active worker. Match the
existing finalizer's eligibility without changing its body. Only after these checks set
`teardown_repair_last_retry_at=clock_timestamp()` and return true. Do not mutate any other field.

## Python integration

Keep the existing repair plan and cap. Add one private helper taking the existing connection
and six-field candidate tuple. It calls the new marker within its own transaction; a missing
SQL result is an actionable programming error. Catch and log ordinary marker exceptions with
both attempt and System identities, leaving cancellation untouched.

Call this helper after the existing per-candidate cleanup/finalization exception log and
after a cleanup-required result. Preserve applied counting, superseded/conflict handling,
unknown-status error, provision repair and exhausted-provision discharge. The original
cleanup exception remains logged even if marking also fails. Do not wrap the entire batch
or add an in-memory cursor, expiring delay, general scheduler or worker-side mutation path.

## Failure model

- Actors: the normal reconciler LOGIN may rotate eligible terminal absence receipts;
  worker/server/provider roles cannot call the marker or write the protected table.
- Assets: exact receipt identity, current owner/head, cleanup-gated consumption, live lease
  exclusion, bounded selection, cancellation, durable retry eligibility and useful error logs.
- Contention: a finalizer or ownership transition can win between selection and retry marking;
  the marker rechecks under the existing advisory/row locks and returns false without writes.
- Accepted: failed marker writes cannot guarantee rotation of that receipt; they remain
  observable and must not prevent healthy selected siblings. Cancellation before a marker
  may leave its old priority. Database clock discontinuities can affect ordering; the timestamp
  is not a deadline and never removes eligibility. No provider availability claim is added.
- Covered elsewhere: receipt authenticity, provider absence, cleanup details and provision
  repair remain with ADR-0623/0754 and their existing SQL/worker tests.

## Validation

Use actual PostgreSQL role logins and existing authority seeding helpers. Extend only the
terminal-attempt helper with optional receipt timestamp seeding, retaining its default.
Parametrize a real 101-receipt repair test: first 100 bootstrap-key deletes either raise
or retain the row through fixture-owned SQL triggers. Keep cleanup, selector and finalizer
real; inject only the external artifact store and pytest-owned pcap directory. First pass
repairs zero; second consumes the healthy receipt and retained ones remain eligible.
A delayed-pass model uses two-day-old receipts and ages the recorded retry timestamps to
one hour ago after the first pass, preserving their relative ordering. This rejects an
expiring-delay-only fix without a wall-clock sleep or database clock override.

Test direct marker identity refusals for every tuple field; consumed/current-head/owner/System
and live-lease races preserve complete row snapshots. Test no direct table grant and exact
function role ACL plus role checks, unchanged selector cap, NULL receipt-priority fallback,
and no receipt/ownership/finalizer changes. A real advisory-lock contender proves serialization.
A fixture-owned marker-error trigger must allow a healthy sibling already in the batch to
complete. Preserve existing cancellation tests and make marker cancellation propagate.

Run original private reproduction unchanged, focused new DB tests, existing migration0149 and
reconciler loop suites. Controlled ordering, missing-mark and missing-identity-fence faults must
fail their named tests; restore exact bytes and rerun. Compare installed selector body modulo
its one ORDER BY and all existing finalizer metadata/body unchanged. Run lint, whole-tree types,
schema/migration/record/doc-link gates and independent scoped code/security review.
