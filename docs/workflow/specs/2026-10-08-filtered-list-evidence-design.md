# Filtered-list boundary evidence

## Problem

The three pairs in ADR-0756 intentionally return empty successful lists
but ADR-0722 requires failure envelopes. Their twelve cells cannot qualify.
The blocker only checks empty items, and the artifacts carrier snapshots the wrong
project for its borrowed System. Root cause is an evidence contract mismatch, not
an authorization defect in the product.

## Scope

Issue #3108, token `q3108-d5a3ee1f`. Change the existing coverage contract's named
observation, shared harness and sole investigation/artifact carrier with owning
regressions. Preserve every required cell, evidence schema/outcome and list API.
No #3097/#3098 consumer expansion, native POWER claim, secret backend, or generic
framework. The operator approved ADR-0756's exact evidence rule on 2026-10-12.

Current owner: `_observe` enforces failure-only evidence, while the carrier marks
three exceptions `filtered_by`. Intended owner: a contract-owned closed pair set;
the shared harness enforces control and empty-envelope checks; the carrier owns
known fixtures/grants and protected-state projections. Remove only obsolete
`filtered_by` handling and its tests. Existing ordinary rejection/absent-twin paths
and other callers remain unchanged.

## Success

1. Exactly the original twelve cells remain bound to the same carrier, with kind
   and expected outcome `rejection` and the same three assertions. Their observation
   explicitly describes filtered denial. No new evidence schema/version or enum.
2. The shared harness rejects an exception on any unlisted pair and rejects a
   listed pair without a positive-control descriptor. Its control reads a known
   independently identified item using authorized grants and the same arguments
   and exposure. Require `status=ok`, no error and that exact item ID in the page.
3. The denied answer must equal the tool-specific canonical empty `ToolResponse`,
   including default fields, collection identity, static actions, empty refs/items,
   `detail=null`, and exact pagination data. No cursor, unknown key, leaked ID,
   nested payload or unexpected action can count as filtering evidence.
4. Snapshot before the control and denied calls and compare afterwards. For an
   investigation the existing setup and close restore the live-project snapshot.
   For artifacts, the selected immutable console part and System identity/project
   mapping are read independently and stay present/equal. New rotated parts do not
   invalidate this intentionally narrow snapshot. Existing session cleanup must
   actually release the System/domain/disks/capacity before reporting a complete run.
5. Real default/recovery × direct/gateway cells exercise all three pairs on the
   candidate. Unit-only or simulated responses are never reported as live proof.

## Design

Add `FILTERED_LIST_BOUNDARIES` to `scripts/coverage_campaign/contract.py` as the
three explicit `(tool, boundary)` pairs, used for descriptive observation only.
The shared harness consumes it to fence the exception; contract IDs/assertions
and qualification behavior remain unchanged.

Replace `Rejection.filtered_by` with an optional `FilteredListControl` containing
`grants: Grants` and `expected_item: Callable[[], Awaitable[str]]`. The callback is
an independent fixture/DB oracle, called after setup, and must return a nonempty ID.
Before a named pair's denied call, read this ID, call the same arguments with its
viewer grants and require an error-free `ok` item with that ID. Do not change grants
or args of the subsequent denied call. Positive controls use the same `Caller`,
including gateway routing. Evidence stores control identity digest and safe scalar
observations, not the item body or bearer tokens.

Construct expected empty envelopes using the two existing shapes, rather than
accepting a truthiness-based subset. Investigations use collection ID
`investigations` and actions `investigations.get`, `investigations.open`.
Artifacts echo the input System ID and action `artifacts.get`. Both use only
`truncated=False, next_cursor=None` and the collection's `count=0` as data.
Compare complete serialized envelopes; assertion messages expose only shape/difference field names, never raw payloads.

The carrier's control oracle for investigations reads the single live row in its
fresh project after `_target_setup` enters. It fails if absent or ambiguous. For
artifacts, select one existing newest console-part ID before the call and bind both
the oracle and narrow snapshot to that ID; the default page must contain it.
The oracle also confirms the fixed row still exists. Reuse `_rows`, `_SYSTEM_PARTS`,
existing grants and session lifetime; no product write or extra VM is required.

## Failure model

- Accepted: trusted test code/DB observer is the oracle; this does not defend against
  an operator fabricating evidence or a privileged peer modifying the database.
- Rejected: empty/permanently empty controls, unrelated visible item, leaked denied
  payload/cursor/refs, wrong status, setup leaks, protected-target deletion/mutation,
  and unlisted exception use. These must fail before qualifying evidence is emitted.
- Accepted: unrelated background console rows may be added. The selected part and
  System mapping remain protected; this is not an all-database no-write guarantee.
- Accepted: concurrent rotation can move the selected item beyond the page. Fail
  the control honestly; do not silently substitute a different item or retry to green.
- Missing runtime fixtures/configuration/issuer/candidate identity remain blocked or
  failed under the existing carrier, never a reduced required denominator.

## Validation

Owning shared-harness tests cover control presence and exact target, canonical empty
shape, all leak fields and unlisted pairs in both exposures, snapshot mutation and
cleanup failure, plus unchanged ordinary rejection and absent-owner behavior.
Contract tests preserve twelve cells/kinds/assertions; qualifier tests accept their
complete rejection records and reject success outcome or missing assertions.
A controlled violation removing the target check must redden the nonmatching-item
case. Do not import collected test modules; new reusable fixture code, only if
necessary, belongs under existing support-module ownership.

Live proof uses the existing investigation tool-cell pytest node, selecting exactly
those twelve IDs across two separately catalog-proven configurations. Capture
candidate role identities, setup/control/denied observations and session teardown.
Run focused checks, lint/type, docs/records/hooks, then the mandatory managed pre-push
full gate once authorized implementation is complete. No full gate for this design.
