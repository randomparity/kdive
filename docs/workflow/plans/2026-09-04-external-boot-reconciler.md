# External-boot reconciler implementation plan

Issue: #2203
Spec: [external-boot reconciler design](../specs/2026-09-04-external-boot-reconciler-design.md)

## Global constraints

- Work only on `feat/external-boot-reconciler-2203` in its sibling worktree.
- Preserve SELECT-only `kdive_reconciler` grants on all external-boot tables.
- Reuse `build_external_boot_payload`, `queue.enqueue`, persisted deadlines, and existing worker
  authority operations; add no provider-specific imports and no new job kind.
- Keep #2204 MCP promotion and initial `preparing` recovery out of this change.
- Use deterministic deduplication and redacted diagnostics.
- Run focused tests red before implementation and green after each task.

## Task 1 — Add candidate selection and enqueue lanes

Create `src/kdive/reconciler/repairs/external_boot.py`. Model a validated source job and one desired
repair operation. Implement four public repair functions for activation, recovery, release, and
cleanup candidates. Each function:

1. reads bounded durable candidates and the newest marked source job;
2. ignores a candidate while an ordinary claimable or live job already owns it;
3. validates the source payload and authorizing envelope;
4. rebuilds the operation through `build_external_boot_payload`;
5. enqueues with a deterministic key derived from candidate, operation, and source job; and
6. isolates a candidate-local failure without exposing raw values.

Operation selection follows the spec table. The recovery lane preserves `recover` versus
`resolve-conflict` from durable attempt basis. The cleanup lane chooses ordinary cleanup versus
System teardown from terminal activation and System state.

Verification (`focused-test`): add `tests/reconciler/test_external_boot_repairs.py`. Before source
implementation, the module import must fail. Green command:
`uv run python -m pytest tests/reconciler/test_external_boot_repairs.py -q`.

## Task 2 — Wire catalog, report, and process configuration

Add the four `_REPAIR_CATALOG` entries and matching scalar `ReconcileReport` fields. Extend
`ReconcileConfig` with the resolver/authority-instance inputs required by the repair factories.
Pass the already-built resolver and required authority-instance setting through production
reconciler composition. Update test configuration factories with inert test values.

Verification (`focused-test`): extend `tests/reconciler/test_loop.py` and process tests to assert
catalog totality, report mapping, production configuration wiring, and failure isolation. The red
failure is absent catalog kinds/config fields. Green commands:
`uv run python -m pytest tests/reconciler/test_loop.py tests/processes/test_reconciler.py -q` and
`just type`.

## Task 3 — Guard allocation release and resolve debt 0007

Implement ADR-0596. Add `ALLOCATION_RELEASE` to `ExternalBootOperation` and keep it absent from every
restricted-state admitted set. Extract one locked release precondition used by both
`_release_locked` and `reclaim_under_lock`, retaining terminal/requested fast paths; for a
releasable Allocation discover all owned Systems, acquire their locks in stable order after the
Allocation lock, and run the matrix guard before the first transition. Map the existing denial
through `ReleaseOutcome` without changing durable state. Update the guarded-tool registry and debt
0007 to its resolved form.

Verification (`focused-test`): extend allocation release and external-boot admission tests. The red
failure is either direct release or the orphaned-active reaper releasing an Allocation with an
uncleaned activation. Green commands:
`uv run python -m pytest tests/services/external_boot/test_admission.py tests/services/test_allocation_release.py -q`
using the actual allocation-release test path discovered in the tree, and `just type`.

## Task 4 — Integration, authority, and structural proofs

Add integration coverage that drives an enqueued repair through the real worker external-boot
handler for each lifecycle family, repeats reconciliation three times, and proves terminal rows and
zero later counts. Connect as `kdive_reconciler` to prove enqueue succeeds while a direct external
table mutation fails. Add the import-closure proof and the foreign-key orphan proof.

Verification (`focused-test`): the new integration and structural cases fail before their fixtures
and repair wiring exist. Green commands:
`uv run python -m pytest tests/reconciler/test_external_boot_repairs.py tests/integration/test_external_boot_job_lifecycle.py -q`
and `just type`.

## Task 5 — Final guardrails and documentation consistency

Run `just format`, stage the intended paths, run `prek run`, re-add exactly those paths if hooks
rewrite them, then run `just lint`, `just type`, the focused tests, and bare
`just ci > /tmp/kdive-2203-ci.log 2>&1 < /dev/null`. Record observed durations and results in the
forge ledger.

Verification (`focused-test`): this task changes machine-checked documents and formatting only;
`just docs-links`, `just docs-paths`, `just check-mermaid`, `just adr-status-check`, and `just lint`
are the executable structural checks.
