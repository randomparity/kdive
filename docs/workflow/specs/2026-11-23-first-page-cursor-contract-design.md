# First-page cursor contract

## Problem

The earlier cleanup made `artifacts.list`, `resources.list` and `jobs.list` reject empty
cursors. [ADR-0192](../../adr/0192-list-pagination-envelope.md) explicitly accepts empty
text for the first page. A real database reproduction returns success for omission and
an error for empty text. The other core list boundaries retain the accepted behavior.

## Scope

One Cast unit restores the three core guards to their original truthy presence checks,
with a short ADR reference explaining the sentinel. Keep the existing decoder and all
surrounding logic. Move empty input out of the earlier invalid-token test matrices and
into real pagination tests, alongside `None`; retain malformed and foreign-tool cases.
Clarify the response-envelope guide and regenerate its served snapshot.
No new owner, alias, public schema, permission policy or architectural decision.

### Failure model

- Actors/deployments: core list clients and their project-scoped database handlers.
- Assets/invariants: first-page compatibility, complete nonoverlapping pagination,
  filtering/visibility, continuation fields, malformed-token rejection and auth/audit order.
- Accepted: existing `ops.jobs_list` and `ops.build_uses_list` reject empty text; preserve
  these established operator contracts, including the latter's explicit test, rather
  than introducing a global policy change. Omission/`None` remains portable.
- Covered elsewhere: cursor decoding, database isolation and the eight other core guards
  keep their existing owners and suites. No snapshot guarantee or signed cursor is added.

## Success

The three corrected tools return their first page for omitted, `None` and empty cursors,
then drain all seeded rows exactly once through real continuation tokens. Nonempty
malformed/foreign tokens retain their error envelopes. Operator behavior stays unchanged.
Only the three predicate/comment changes and accurate documentation alter production files.

## Validation

- focused-test: parameterize existing real pagination tests in
  `tests/mcp/catalog/test_artifacts_tools.py`, `test_resources_tools.py` and
  `tests/mcp/jobs/test_jobs_tools.py` with initial `None`/empty cursors. Each empty case
  must fail before the fix. Existing omitted-page and invalid-token cases remain.
- focused-test: add empty text to `tests/mcp/ops/test_queue_tools.py`'s invalid cases;
  run it and `test_build_use_recovery.py` unchanged to preserve operator contracts.
  Run all five files with Docker required, plus the original private database reproduction.
- focused-test: restore each corrected `is not None` guard separately and require its
  empty-page test to fail, then restore source. Verify whole-module ASTs differ only
  by these guards and declared doc text; capture unchanged tool schemas and generated CLI
  output. Regenerate served docs before resource checks; run lint, whole-tree types,
  doc/link/resource guards and hooks. No malformed input is reclassified as first-page text.
