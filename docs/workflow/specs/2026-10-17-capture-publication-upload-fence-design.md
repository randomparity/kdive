# Preserve capture publication ownership during upload cleanup

Design proposal for issue #3262; source baseline `0b177a335435d404191ecab13b3374f6c01ca136`.
The operator approved preparation, independent review and scope audit only. The data/permission
proposal below and implementation require a separate decision. No migration number is assigned.

## Problem and authority

[ADR-0559](../../adr/0559-fence-capture-artifact-publication.md) requires discarded captures to
retain their immutable conditional-publication tombstones. The current shared ownership predicate
in `src/kdive/reconciler/cleanup/uploads/upload_fences.py` checks artifacts, upload manifests and
live write leases. It omits capture publication ownership. Both the orphan sweep and expired
manifest reaper can therefore classify a retained tombstone as reclaimable after those other
fences disappear. This is a constructed source-level path, not a reproduced deletion.

The frozen external authority is the operator-approved bounded proposal and
[WORK:SCOPE](https://github.com/randomparity/kdive/issues/3262#issuecomment-6234405517), token
`q3262-47747759`. Its C1–C5 cover exact ownership, agreement between deleters, existing reclamation,
planned deterministic proof, review/audit and final operator acceptance respectively.

## Scope

Extend the existing shared SQL predicate, keeping both callers and their locks/version deletion.
Correct descriptive text in the shared module, `uploads.py` and `upload_orphans.py`.
Use existing orphan/race tests, capture-publication integration tests and necessary existing test
fixtures. No new cleanup component or owner abstraction is needed. A conditional supporting index
and two-column permission grant are proposed below; no SQL file is authorized by this design.

The explicit exclusions and owners remain: vmcore retention/publication journal #3238; installed
on-demand failure diagnosis #3239; full-stack traffic commands #2815; real-stack interruption and
resource proofs #2816; native POWER/TCG qualification #2818. Also outside scope are new APIs,
dependencies, provider families, historical broad deletion, tombstone-removal protocols and
provider guest/host mutation. Serialize later shared-predicate edits with #3238, then run the
combined relevant regressions. This fix does not depend on that issue's unimplemented journal.

## Proposed behavior

Add a fourth `NOT EXISTS` conjunct to `_OWNER_FENCE_SQL`, matching
`capture_operations.publication_object_key = c.key` and `publication_state IN
('publishing', 'canceling', 'discarded')`. Use exact text equality, not a prefix, owner-wide
exemption, parsed UUID or object metadata heuristic. Both selector entry points retain their
signatures. The publication row is the authoritative owner of this key, regardless of whether
its job lease has ended or its provider/spool cleanup finished (C1, C2).

`pending` has no key under migration 0113's shape constraint. `published` is deliberately outside
the new predicate: its artifact row owns the bytes under the existing contract. The change must
not exempt all historical captures from retention. A different key under the same Run or any
Investigation remains eligible when the existing fences and grace allow deletion (C3).

The orphan sweep currently inventories version pages and captures exact versions before its
locked SQL classification; it does not currently run a separate page-wide SQL prefilter.
`reclaimable_upload_keys` still supports page input and is tested directly. A race proof must
place the new committed owner before the actual locked recheck, not invent an earlier production
SQL stage. The reaper similarly captures versions before its per-key recheck.

## Concurrency and failures

Publication journals the key before object creation. `publishing -> canceling -> discarded`
never drops the new fence. The `publishing -> published` transition commits the artifact row and
state together, so a statement sees either the publication owner or the artifact owner.
A pending operation cannot have legitimately published a version before journaling. If journaling
and PUT happen after a cleanup recheck, the new immutable version is outside its captured batch.
These existing ordering contracts permit the short owner lock to end before store I/O (C3).

Do not change lock scopes, lock acquisition policy, isolation, candidate limits, grace clocks,
version batching, error counts or retry behavior. Database errors still fail closed through each
caller's existing failure reporting. Discarded tombstones remain retained without an age limit;
removing them would require separate authority and proof under ADR-0559.

### Failure model

| Class in the supported versioned-store deployments | Required response and evidence |
|---|---|
| Discarded tombstone with no other owner | Both cleanup routes retain the same version and metadata, including repeated runs. |
| Publishing or canceling after lease expiry | Exact key remains protected independently of the job's liveness and spool state. |
| Owner commits between inventory/capture and locked recheck | Recheck observes committed ownership and issues no deletion for that key. |
| New object appears after the recheck | Existing immutable-version behavior retains the uncaptured version. |
| Publication commits while cleanup classifies | Artifact/state atomicity leaves no ownerless committed snapshot. |
| Query permission denied or database unavailable | Existing error propagation/counting reports incomplete repair; no fallback to unprotected deletion. |
| Store mutation fails or completion is ambiguous | Existing exact-delete recovery applies only to unowned keys; protected keys never reach delete. |
| High retained-operation cardinality | Proposed partial key index supports the lookup; later real PostgreSQL evidence must establish its use and cost. |

Arbitrary deletion of durable capture-operation rows or tombstones by an administrator, a
nonconforming conditional-write store, and unimplemented historical cleanup are not newly solved
failure classes. This design relies on the existing admitted store and durable journal contracts.

## Proposed database contract, awaiting approval

Migration 0113 revokes general access to `capture_operations` from all application roles, then
returns selected columns to `kdive_reconciler`. Those include both fields needed by this predicate.
The on-demand registrar passes the server pool into `reconcile_now_handler`, which passes that
pool to `reconcile_once`; process assembly does not silently switch it to a reconciler identity.
Thus a direct subquery is compatible with the reconciler role but needs additional visibility for
`kdive_server`. This is source-level role evidence, not diagnosis of the installed #3239 failure.

Recommend granting **SELECT only on `publication_object_key` and `publication_state` to
`kdive_server`**, with no table-wide SELECT, write grant, worker grant or lifecycle-witness grant.
This adds visibility of capture key/state pairs across the database to the server credential;
it exposes neither credential/launch identity nor provider process details. The server already
performs global upload cleanup, but that existing responsibility does not itself authorize this
new grant. The operator must explicitly accept it before implementation.

Recommend a nonunique B-tree index on `publication_object_key` with the exact same three-state
partial predicate. Existing indexes cover operation identity, job/attempt, worker/state and
state/created time, not this lookup. Discarded rows are durable and can grow with capture volume;
a repeated full-table lookup in each per-key recheck is not a defensible unbounded default.
The partial index excludes pending and published history and preserves duplicate-key visibility.
No runtime query plan or timing has been measured under the design-only authorization.

Alternatives: a security-definer boolean helper would hide row enumeration but add an internal
permission boundary, function lifecycle and invocation surface; it is larger than the proposed
column grant. Replacing on-demand credentials or omitting its cleanup would redesign #3239 scope.
A metadata-only tombstone filter cannot protect publishing/canceling ownership or both selectors.
An unindexed direct subquery avoids migration only by leaving lookup cost unbounded in retained
history. The recommended grant/index is a proposal, not an accepted decision. If selected, root
must approve its expanded implementation surface, reserve a fresh migration path and determine
whether a new permission ADR is required; do not amend ADR-0559 or migration 0113.

## Validation and acceptance

The private full implementation plan carries complete proposed edits and focused proof contracts.
Runtime execution awaits implementation/operation approval; no test result is implied here.

- C1/C2: real PostgreSQL selectors and both deleters for publishing/canceling/discarded without
  artifact, manifest or live lease protection; repeated passes retain exactly the owned key.
- C3: ordinary Run and Investigation keys, same-Run different-key reclamation, pending without a
  key, published artifact ownership, held-lock and exact-version regression cases remain covered.
- C1/C3: deterministic ownership insertion before each actual locked recheck, plus existing
  uncaptured-version races. Use event/callback boundaries, never timing sleeps.
- C4: real LOGIN connections through `authority_role_dsns` for server and reconciler prove selector
  and deletion behavior; server unrelated capture columns and writes remain denied. Worker and
  witness grants remain unchanged. Catalog evidence verifies index columns/predicate and later
  realistic-cardinality EXPLAIN evidence records actual plans without claiming a universal planner
  choice for tiny tables.
- C4: real SeaweedFS tombstone at a Run publication key survives both complete cleanup routes and
  repeated passes, then rejects a late conditional create with identical version/etag/metadata.
  Run a disposable-fixture fault removing the new conjunct and require this proof to fail.
- C5: one complete-set independent design pass, conditional second on unchanged bytes, one author
  edit batch, documentation guards and distinct scope audit. Final acceptance and runtime proof
  stay separate. Ordinary PostgreSQL/store proof is not native VM or POWER/TCG qualification.
