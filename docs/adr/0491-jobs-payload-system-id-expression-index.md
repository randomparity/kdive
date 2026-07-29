# ADR 0491 — One unpartitioned expression index on `jobs (payload->>'system_id')`

- **Status:** Accepted
- **Date:** 2026-07-29
- **Issue:** #1561
- **Remediates:** [ADR-0454](0454-systems-get-resolves-the-failing-job-category.md) §1, which
  recorded `latest_failed_job_for_system`'s sequential scan as an accepted cost. That cost is
  paid off here; the ADR-0454 decision itself is unchanged.

## Context

`jobs` shipped in `0001_init.sql` with a primary key on `id` and a `UNIQUE (dedup_key)`
constraint, and nothing was ever added: a sweep of `CREATE INDEX` across
`src/kdive/db/schema/0001..0080` finds no index on the table at all — not on `kind`, not on
`state`, not on `payload`.

Nine SQL sites correlate a job to a System through `payload->>'system_id'`, and every one of
them was therefore a sequential scan over the whole table:

| site | shape |
| --- | --- |
| `jobs/queue.py` `latest_succeeded_job_for_system` | `kind = $1 AND payload->>'system_id' = $2 AND state = 'succeeded'`, newest first |
| `jobs/queue.py` `latest_failed_job_for_system` | `kind = ANY(...) AND payload->>'system_id' = $1 AND state = ANY(terminal)`, newest first |
| `jobs/queue.py` `list_jobs` | optional `j.payload->>'system_id' = $n` filter |
| `reconciler/repairs/systems.py` | correlated `NOT EXISTS`, `j.payload->>'system_id' = s.id::text` |
| `reconciler/repairs/allocations.py` `_CRASHED_SYSTEM_IDLE_SQL` | correlated `NOT EXISTS`, **no** `kind` filter, `state` inside an `OR` |
| `reconciler/repairs/allocations.py` `has_active_capture_job` | `kind = $1 AND state = ANY(...) AND payload->>'system_id' = $2` |
| `reconciler/repairs/console_rotation.py` `_IN_FLIGHT_ROTATION_SQL` | same shape |
| `reconciler/cleanup/provider_reaping.py` | same shape |
| `mcp/tools/lifecycle/systems/snapshot.py` | `payload->>'system_id' = $1 AND kind = ANY(...)` |

The table is effectively append-only — the only `DELETE FROM jobs` in the tree is a single
`dedup_key` delete in `reconciler/cleanup/gc.py` — so the scan grows without bound, and the
reconciler pays it on every sweep.

`payload->>'system_id'` is a JSONB text extraction. A btree on a *column* cannot serve it; only
an expression index (or a generated column, which would need a backfill and a write path change)
can, and the planner matches an expression index only when the query spells the expression
exactly as the index does.

The design question is the *shape*, and the sites do not share one predicate. Two are ordered
`LIMIT 1` reads with `kind` + `state` equality/`ANY`; three are correlated anti-joins, one of
which (`_CRASHED_SYSTEM_IDLE_SQL`) filters on no `kind` at all and puts `state` inside a
disjunction. The issue proposed
`((payload->>'system_id'), created_at DESC) WHERE state = 'failed'` as a starting point.

## Decision

We will add **one unpartitioned single-key btree expression index**, in migration
`0082_jobs_payload_system_id_index.sql`:

```sql
CREATE INDEX jobs_payload_system_id_idx ON jobs ((payload->>'system_id'));
```

No `state` predicate, no `created_at`/`id` trailing keys, no second index. The three reasons,
measured on a 150 k-row `jobs` table (100 k system-scoped jobs over 5 000 Systems, 30 k jobs
with no `system_id`, 20 k `restore` jobs) on `postgres:17`:

**1. `state` must stay out of the index, because it is the queue's hottest write.** A job's
`state` is rewritten on every transition — `claim` to `running`, `complete`/`fail` to terminal —
and `heartbeat_at` more often still. With no indexed attribute touched, those are heap-only-tuple
(HOT) updates that maintain no index at all. `payload` is written at enqueue only, including the
[ADR-0447](0447-recycle-terminal-redates-created-at.md) recycle, and never per attempt, so this index leaves
every one of those updates HOT-eligible. Putting `state` in a partial predicate takes the
opposite side of exactly that path: PostgreSQL counts predicate columns as HOT-blocking, so each
transition becomes a non-HOT update plus an index insert-and-kill. Driving 12 000 state
transitions over an identical table under each shape:

| index shape | `n_tup_upd` | `n_tup_hot_upd` |
| --- | --- | --- |
| `((payload->>'system_id'))` | 12 000 | 3 932 |
| `((payload->>'system_id'), created_at DESC) WHERE state = 'failed'` | 12 000 | 0 |

**2. Trailing `created_at DESC, id DESC` costs ~10x the size and buys a sort we do not care
about.** A System's jobs share one key, so the single-key index is heavily btree-deduplicated;
appending `created_at`/`id` makes every entry unique and defeats that. Measured on the same
table: 1 168 kB versus 11 MB, against a 17 MB heap. What the composite buys is the removal of
the `Sort` node above the two `LIMIT 1` reads — a top-N sort over the handful of jobs belonging
to *one* System, negligible next to the 150 k-row sequential scan the index removes.

**3. One unpartitioned index serves all nine sites; a partial index serves at most two.**
`_CRASHED_SYSTEM_IDLE_SQL` has no constant `state` to imply a predicate from, and the two ordered
reads span `succeeded`, `failed` and `canceled` between them — a `WHERE state = 'failed'` index
would need siblings to cover the rest, multiplying the write amplification the table is most
sensitive to.

Verified by EXPLAIN on the seeded table, and pinned by
`tests/db/test_migration_0082_jobs_payload_system_id_index.py`, which asserts the plan for all
three representative shapes names `jobs_payload_system_id_idx` and that the pre-migration plan is
a `Seq Scan` — so the test reddens if the index stops being *used*, not merely if it stops
existing.

## Consequences

- The nine correlated reads become index lookups. The two ordered `LIMIT 1` reads plan as a
  bitmap index scan plus a top-N sort over one System's jobs; the anti-joins plan as a nested
  loop with the index on the inner side whenever the outer set is small, which is the normal case
  (a handful of `restoring` or `crashed` Systems).
- One index to maintain on insert. Enqueue writes one more index entry; a recycle
  (ADR-0447) rewrites `payload`, so it also updates the entry — both are enqueue-frequency, not
  per-attempt.
- **The anti-joins still fall back to a hash anti-join when the outer set is large.** With ~200
  candidate Systems the planner hashes the filtered `jobs` side instead. That is the correct
  choice at that size and it is not a regression — it is the same seq scan as today, chosen
  because it is cheaper — but it means the index is not a guarantee for the reconciler's
  worst case.
- **The migration takes a brief `ACCESS EXCLUSIVE` lock on `jobs`.** `apply_migrations` runs
  every migration inside one transaction ([ADR-0015](0015-sql-migration-runner.md)), so
  `CREATE INDEX CONCURRENTLY` — which cannot run in a transaction block — is unavailable. The
  build blocks queue writes for its duration. Migrations run at deploy time before this build's
  workers claim, and the index is small (~1 MB per 150 k rows).
- Rows whose payload carries no `system_id` (`build`, `install`, …) are indexed as NULL rather
  than excluded. A `WHERE (payload->>'system_id') IS NOT NULL` predicate *is* provably implied by
  an equality on the expression and was measured to work, but it shrank the index only in
  proportion to the system-less share (1 088 kB vs 1 168 kB here) while adding a predicate that
  drops out silently from under any future call site whose own predicate stops implying it.
- No behavior, API, tool-surface, RBAC or query change: `queue.py` and the reconciler modules are
  untouched. The index is matched by the SQL they already emit, which is why the expression must
  keep being spelled `payload->>'system_id'` verbatim at every site — a future `payload #>>
  '{system_id}'` or a cast would silently lose the index.
- `jobs` still has no index on `kind` or `state` alone. `claim`'s own `state = 'queued'` scan is
  out of scope here and is a separate decision, since it is the one place where indexing `state`
  might be worth surrendering HOT updates for.

## Alternatives considered

- **The issue's `((payload->>'system_id'), created_at DESC) WHERE state = 'failed'`.** Rejected on
  the measurements above: it zeroes out HOT updates on the queue's hottest write path, serves
  neither anti-join nor the `succeeded` read, and would need siblings per state to cover the
  sites it misses.
- **One partial index per state.** The same write amplification, multiplied, on a table whose
  every row transitions through three states.
- **A `system_id uuid` generated column plus a plain btree.** A stored generated column rewrites
  the whole table on the `ALTER`, changes the write path, and adds a column the code must keep in
  sync with the payload; the expression index gets the same plans with no schema surface. Worth
  revisiting only if `system_id` becomes a real foreign key.
- **A GIN index on `payload`.** Serves containment (`payload @> ...`), not the `->>` equality
  these sites actually emit, so no site would use it without being rewritten; it is also larger
  and slower to maintain.
- **Do nothing and keep ADR-0454 §1's accepted cost.** Honest when written, but the table only
  grows, `systems.list` (#1560) would multiply the read, and the reconciler pays it every sweep.
