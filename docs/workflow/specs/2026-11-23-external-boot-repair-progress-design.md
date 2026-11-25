# Instance-owned external-boot repair progress

## Problem and authority

A real Reconciler with 101 prepared activations and missing source jobs for the first 100
returns zero twice. Making the first page ineligible lets the same healthy last row enqueue.
The living queue requires bounded per-lane progress; the user authorizes internal interfaces
and architecture with public contracts preserved. [ADR-0839](../../adr/0839-external-boot-repair-keyset-progress.md)
records the runtime ownership and restart policy. No durable schema change is needed.

## Cursor ownership and wiring

In repairs/external_boot.py define `RepairLane` as the four existing lane literals and
`RepairCursors = dict[RepairLane, UUID]`. Reuse that lane type in signatures and SQL map typing.
No cursor wrapper, global dictionary or configuration field is needed.

Each Reconciler constructs a fresh `_external_boot_cursors` dictionary. Its run_once passes it
to a new optional keyword on reconcile_once, which passes it to _repair_plan. The plan creates
an empty map only when no map is supplied; never replace a supplied empty dict by truthiness.
Add this explicit map as the fourth input of the existing catalog factory contract. Existing
factories/lambdas accept an unused underscored input; only _external_boot_repair uses it to
bind the lane callback. Keep all existing factory bodies and binding times otherwise intact.
This extends one existing seam rather than creating a second catalog or freezing all callbacks.

repair_external_boot_lane gains optional `cursors: RepairCursors | None = None`. A one-shot
call owns an empty map; standalone repeated callers supply the same map explicitly. Config
and a second Reconciler sharing it never become owners of the first instance's map.

## Selection and advancement

Keep all four _CANDIDATE_SQL values unchanged. _candidates receives an optional after UUID
and wraps that existing query as `candidate`. Apply `(%s::uuid IS NULL OR
candidate.activation_id > %s)` outside the entire eligibility expression; order by that ID
and retain LIMIT100. Parameters carry both hint values and the existing limit. An outer
predicate avoids changing the activation lane's prepared-OR-expired precedence.

Read one page after the lane's hint. If empty with a non-NULL hint, read one page from NULL.
If both are empty, remove the lane hint and return zero. Do not clear the old hint before a
wrapped read or job lookup succeeds. At most two selection queries return at most one nonempty
page, and no more than100 candidates are inspected per lane/pass. UUID zero is not a sentinel.

Keep the two bounded job lookups before the candidate loop. After each candidate's current
transaction and ordinary exception handling, set its lane hint to that ID. This includes
no-source/live skips, successful commits and caught candidate errors. Cancellation or another
BaseException escapes before that assignment. A failed whole-page lookup leaves the old hint.
Preserve original enqueued counts, transaction boundaries, sanitized logs and every enqueue guard.

## Failure model

- Actors: serial normal Reconciler passes and explicit standalone calls using trusted cursor
  state; separate instances may share config/pool/database but own independent dictionaries.
- Assets: bounded work, eventual progress over a finite eligible set, safe queue insertion,
  cancellation, source/tenant/authority matching, and no hidden cross-instance state.
- Accepted: hints reset on restart; repeated restarts can delay later rows. Rows inserted behind
  a hint wait for wrap. Manual overlapping passes may revisit rows; existing durable dedup and
  authorization remain safety owners. No fairness deadline or exactly-once inspection is promised.
- Whole-page database/lookup failure retries that page. Cancellation retries the interrupted
  candidate while retaining completed earlier progress. Persistent database unavailability is
  not converted into fake successful progress or a new scheduler policy.
- Covered elsewhere: all durable eligibility, provider admission, live-successor and payload
  validation, source lookup bounds and transaction/queue fences retain their existing owners.

## Validation

Extend existing external-boot repair and loop tests with actual PostgreSQL role connections,
existing deterministic-UUID vehicles and the current catalog entry/factory. Run101-row cases
for each lane and both missing validated sources and matching live successors on the first page;
the last valid candidate must enqueue on a later pass. Count selected/inspected work within100
without replacing SQL selection or enqueue logic. Include the prepared-OR branch and UUID zero.

Prove wrap/retry by expiring an earlier live source after reaching the tail, then revisiting it.
Interleave another lane and a second Reconciler sharing config/pool to prove isolated progress.
Use fixture-owned SQL enqueue rejection to prove ordinary candidate failures advance; remove its
DDL in finally because the database fixture resets rows, not functions. Retain real admission,
source and dedup tests. Test lookup timeout and cancellation leave an uninspected/interrupted
cursor unchanged; a controlled commit failure still advances inspected work without counting it.

Run original private Reconciler reproduction unchanged, full focused repair/loop suites, lint
and whole-tree types. First-page, nonadvancing hint and cross-owner state faults must fail named
regressions, then restore bytes and rerun. Normalize explicit wiring/unused factory parameters
for AST preservation of other repair bodies; compare the four SQL values and enqueue/source/live
helpers exactly. Verify unchanged public metadata and actual built artifact before closure.
