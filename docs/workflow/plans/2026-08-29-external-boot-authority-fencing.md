# External-boot authority fencing implementation plan

## Goal and architecture

Implement issue #2125's ADR-0584 database and worker-finalization slice. Migration 0122 supplies the
security-definer generation, acknowledgement, and result-commit boundary; Python adapters route
external-boot finalization through it while preserving ordinary queue behavior.

Tech stack: Python 3.14, psycopg 3, PostgreSQL PL/pgSQL, pytest, uv, and just.

## Global constraints

- Support x86_64 and ppc64le; do not infer target behavior from the x86_64 host.
- Use migration number 0122 and accepted ADR-0584; add no dependency.
- Persist no worker credential, provider secret, command, path, raw provider definition, or
  unbounded provider output.
- A stale or mismatched authority actor affects zero lifecycle, job, cleanup, and audit rows.
- Guardrails: `just lint`, `just type`, focused `just test-verbose <path>`, and pre-push `just ci`.
- Branch: `feat/external-boot-authority-fencing-2125`; base: `main`.

## Task 1: Prove and add the authority schema

Files: create `src/kdive/db/schema/0122_external_boot_authority.sql`; modify migration inventory
tests; create `tests/db/test_external_boot_authority_migration.py`.

Interfaces: SQL functions consume the existing `worker_incarnations`, `jobs`, `systems`, `runs`,
`allocations`, and `external_boot_activations` tables. Later Python work calls the exact functions
created here.

1. Write PostgreSQL tests for role grants, strictly ordered concurrent per-System allocation,
   immutable bindings, journal-head compare-and-set, response-loss acknowledgement replay,
   positive-quiescence acknowledgement, stale/cross-binding zero-row results, cleanup/job/audit
   atomicity, later-Run denial, protocol-3 generic-finalization denial, and redaction-safe audit rows.
2. Run `just test-verbose tests/db/test_external_boot_authority_migration.py`; expect failures because
   migration 0122 and its functions do not exist.
3. Add the five authority tables, constraints/indexes, role grants, and security-definer functions.
   Revoke direct worker/reconciler mutations and validate every bounded input before writes.
4. Update every explicit migration-tail expectation with `(\"0122\",
   \"0122_external_boot_authority.sql\")`.
5. Re-run the focused test; expect all tests to pass. Commit as one schema-contract change.

Acceptance: callers cannot select generations; exact duplicate acknowledgement is idempotent;
stale and cross-boundary calls return `superseded`; accepted job/cleanup/audit writes are atomic.

## Task 2: Route external-boot finalization through the contract

Files: modify `src/kdive/jobs/queue.py`, `src/kdive/jobs/worker.py`, and matching tests under
`tests/jobs/`.

Interfaces: add `ExternalBootAuthorityResultV1` success/failure variants to `jobs.models`, queue
adapters whose parameters mirror migration 0122's immutable authority result binding and return
`Job | None`, and a versioned persisted boot-payload marker. Generic completion/failure continues
for ordinary jobs but SQL rejects marked external jobs.

1. Write worker tests that assert external-boot success, exception, terminal failure, and retry call
   the authority-bound adapter; missing/malformed carriers fail closed without generic fallback; a
   stale response produces no Run or job transition; a later Run cannot reuse earlier authority;
   and protocol-3 workers cannot claim marked jobs.
2. Run the focused test file; expect the new assertions to fail on the existing generic completion.
3. Add the typed carrier, queue adapters, and worker dispatch branch required by the persisted
   authority result contract. Preserve fresh finalize connections and the System, Run, then row-lock
   order. Carry authority facts on categorized provider exceptions without logging secrets.
4. Re-run focused tests; expect all tests to pass. Commit the worker routing separately.

Acceptance: external-boot finalization never falls back to the generic fence when authority facts
are present or required; ordinary job behavior is unchanged.

## Task 3: Verify and review

Files: only files already owned by Tasks 1–2, plus review-driven tests or fixes.

1. Run `just lint`, `just type`, and focused database/job tests; expect exit 0.
2. Run the adversarial and security review loops; fix every defensible in-scope finding with its own
   commit, and record any owned deferral.
3. Run the simplification pass; retain explicit security checks and remove only redundant code.
4. Run `just ci`; expect exit 0 before delivery.
5. Push, open a PR closing #2125, wait for required CI, publish the review record, and hand off the
   exact `MERGE-READY` SHA without merging.

Rollback is `git revert` of the commits before deployment. Once migration 0122 is deployed its
tables/functions remain as an additive schema; rollback disables callers but does not reuse or
delete issued generations.
