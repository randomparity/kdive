# System run lookup index

Issue: #2717. Scope: q2717-825bd340 (approved campaign exclusions, 2026-09-27).

## Outcome and scope

Add migration 0159 with one unpartitioned btree on `runs(system_id)`.
The schema owns access paths; the two existing systems.get queries and their
response semantics remain with the systems-view owner (ADR-0677).
General index tuning remains with the respective database/query owners.

## Success

On a real migrated PostgreSQL database with 45,000 Runs, both current query
shapes use the new index for an ordinary System (100 Runs) and a reused System
(5,100 Runs). The same pre-migration queries scan runs. Results stay identical.
Fresh installation and migration reruns preserve one valid single-key index.
No guarantee prohibits a sequential scan on small or unselective tables.

## Decision

[ADR-0701](../../adr/0701-runs-system-id-index.md) records the measured comparison
with `(system_id, created_at DESC)`. The single-key index is 352,256 bytes versus
1,851,392 bytes on the measured data. Both serve both lookups. The composite
accelerates the active lookup on the reused System, but leaves history aggregation
and sorting. Choose the smaller index for the requested shared predicate.

## Failure model

- Required: deployed migration runs transactionally and produces a usable index;
  test upgrade and fresh installation against real PostgreSQL.
- Accepted: plain CREATE INDEX blocks writes while building (SHARE table lock).
  Existing ADR-0015 migration transactions exclude CONCURRENTLY. Operators run
  migrations during a suitable maintenance window; no duration bound is promised.
- Accepted: the active lookup still filters and sorts a System's matching Runs;
  history still groups and sorts. No latency SLA or universal index-plan promise.
- Accepted: one more index consumes disk and insert/System-binding write work.
  State is not indexed. Existing indexes may independently affect HOT eligibility.
- Required: failed transactional builds roll back; do not modify accepted migration
  bytes or silently bypass migration tracking. Reversal needs a new forward migration.

## Validation

`tests/db/test_migration_0159_runs_system_id_index.py` compares plans and results
before/after migration for both query shapes and both System sizes, with the real
foreign-key chain, mixed terminal/non-terminal states, and ANALYZE statistics.
The catalog test checks the chosen exact shape, readiness, and migration rerun.
Existing inventory expectations in test_migrate.py and the 0091, 0102, and 0115
migration test modules gain the 0159 entry/tail length after the coordinated 0158 base.
No planner switches force index usage. Removing migration SQL must redden tests.
Run focused tests with KDIVE_REQUIRE_DOCKER=1, then lint/type/schema/order/records
checks. Installed pre-push just ci owns the final full local gate. CI remains
separate. This change needs real PostgreSQL, not guest VM or architecture-specific
provider execution; host x86_64, supported targets x86_64 and ppc64le remain intact.
