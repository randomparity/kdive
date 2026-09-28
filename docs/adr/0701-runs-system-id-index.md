# 0701 — A single-key index for System run lookups

## Status

Accepted (2026-09-27)

Issue: #2717

## Context

The active-run and run-history queries in systems/view.py filter by system_id.
Existing runs indexes do not lead with that key. ADR-0677 permits reusing Systems
across Investigations, so table scans grow with unrelated Runs.

## Decision

Migration 0159 adds `CREATE INDEX runs_system_id_idx ON runs (system_id)`.
Use a plain transactional build under [ADR-0015](0015-sql-migration-runner.md).
Keep both queries unchanged. Like [ADR-0491](0491-jobs-payload-system-id-expression-index.md),
prefer a compact shared-predicate index over optimizing each ordering separately.

## Evidence

On PostgreSQL 17.11, seed 400 Systems with 100 Runs each plus 5,000 older Runs
on one System, distributed among 100 Investigations and three Run states.
Run ANALYZE then EXPLAIN (ANALYZE, BUFFERS) for both current query shapes with
no added index, a system_id index, and a system_id/created_at DESC index.
The regression test's seed and query constants reproduce the workload; substituting
both index definitions before EXPLAIN reproduces the comparison.

| Shape | Index bytes | Ordinary active/history ms | Reused active/history ms |
| --- | ---: | ---: | ---: |
| none | 0 | 4.493 / 3.161 | 3.828 / 3.437 |
| system_id | 352256 | 0.050 / 0.074 | 0.900 / 1.225 |
| system_id, created_at DESC | 1851392 | 0.031 / 0.087 | 0.019 / 1.503 |

These are one local comparison, not stable benchmarks or latency promises.
Both index shapes serve both queries. The single-key form uses five shared buffer
hits/reads for each ordinary lookup versus 834 before. On the reused System it
uses 103; the composite uses four for active and 125 for history. Composite active
uses incremental sorting for the remaining id tie-break; history still aggregates.

## Considered & rejected

- **Composite ordered index.** verified: comparison above. It improves reused-System active lookup
  substantially, but costs 5.26 times the index space and does not eliminate the
  history aggregation. The issue requires serving both predicates, without a latency
  SLA. Retain per-System active filtering/sorting for the smaller shared index.
- **No index.** verified: comparison above. Both queries scan all 45,000 rows and read 834 buffers.
  This retains the exact cost #2717 asks to remove.
- **Partial or covering indexes.** judgment: The two queries have different residual
  filters and history needs all states. Extra predicates/keys add maintenance and
  contract coupling unnecessary for the common system_id equality.

## Consequences

One extra index is maintained on insert and System binding. The index includes
NULL system_id entries and no state predicate. It makes no global HOT guarantee.
A normal CREATE INDEX takes a SHARE lock on runs and blocks writes during build;
the duration depends on table size and hardware. CONCURRENTLY cannot run inside
the existing migration transaction. Schedule deployment accordingly.
Small or unselective workloads may still choose sequential scans. This is a
planner choice, not a correctness failure. Reversing the persisted index requires
an explicit forward DROP INDEX migration, not reverting source alone.
