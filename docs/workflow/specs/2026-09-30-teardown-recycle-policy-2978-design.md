# Every ordinary `{uid}:teardown` enqueuer states its recycle policy (#2978)

## Scope and authority

Campaign 60f31b4f6024, issue #2978, token `q2978-1e5905a9`. Operator decision 2026-09-29
("Breakglass recycles, lane warns"). Approved exclusions: a reconciler re-run lane for `failed`
Systems (operator; ADR-0441 stands), authority teardown recycle (#2917), investigation
force-close recycle (stays replay). Recorded as a dated amendment to
[ADR-0435](../../adr/0435-reclaim-failed-provision-artifacts.md); no new ADR, no migration.

## Problem

Since #2929 `systems.teardown` resets a `failed` `{uid}:teardown` row (`JobRecyclePolicy.FAILED`).
`enqueue_control_teardown` always enqueues with the default `NEVER`, so `ops.force_teardown`
returns the dead job, and `repair_orphaned_systems` replays a failed row on every pass without a
log line (`already_queued` suppresses it). A System whose teardown hit #2928's `conflict` settles
back to `ready` with its Allocation gone and stays there silently.

## Design

1. `enqueue_control_teardown(conn, system, authorizing, *, recycle)` takes a required
   keyword-only `recycle: JobRecyclePolicy` and passes it to `queue.enqueue` on the ordinary
   branch. The preactivation-authority branch ignores it (unchanged). Required, so no caller
   inherits a policy by accident. Callers: `ops.force_teardown` → `FAILED`;
   `jobs/service_operations.py enqueue_teardown` (investigation force-close) → `NEVER`;
   `repair_orphaned_systems` → `NEVER`.
2. `ops.force_teardown` (`_teardown_locked`) refuses a `reprovisioning` System under the System
   lock, after the `torn_down` short-circuit and before the enqueue: `conflict`,
   `data.current_status = "reprovisioning"`, `suggested_next_actions = ["systems.get"]`, no job
   write. The break-glass audit row is already committed, as for every other outcome.
3. `repair_orphaned_systems` does not touch a candidate whose `{uid}:teardown` row is `failed`:
   the candidate query excludes it and the System-locked recheck skips it.
4. New lane `report_stranded_orphan_teardowns` (catalog name `stranded_orphan_teardowns`, after
   `abandoned_jobs`): read-only. It selects orphaned Systems (Allocation terminal) not in
   `torn_down`/`failed`/`reprovisioning`/`tearing_down` whose `{uid}:teardown` row is `failed`.
   `tearing_down` is excluded because `repair_stalled_tearing_down_systems` recycles that row.
   For each System not already warned about the same failure it logs one WARNING naming the
   System, the job, its `error_category`, and the remedy (`systems.teardown`), and returns the
   number of such new warnings. The loop feeds that into the existing
   `kdive.reconciler.repairs{repair_kind="stranded_orphan_teardowns"}` counter and
   `repair_counts`, so each warning line is one counter increment.
5. Dedupe: a module-level `dict[UUID, datetime]` maps System id → the failed row's `updated_at`
   last warned about. Each pass replaces the dict with the current stranded set, so it holds at
   most that set, and a System whose row is recycled and fails again (new `updated_at`) warns
   again. No persistence.

## Success

- `ops.force_teardown` on a non-`torn_down`, non-`reprovisioning` ordinary System whose row is
  `failed` returns that job id `queued` with `attempt = 0`.
- `ops.force_teardown` on a `reprovisioning` System returns `conflict` and leaves the row (absent,
  `queued`, or `failed`) byte-identical.
- For an orphaned `ready` System with a `failed` row: `repair_orphaned_systems` returns 0 and the
  row is unchanged; the first `report_stranded_orphan_teardowns` pass returns 1 with one WARNING,
  the second returns 0 with none; a new failure (changed `updated_at`) returns 1 again; a
  `tearing_down` System with a `failed` row returns 0.

## Failure model

1. **Actors and deployments**
   - a `platform_admin` calling `ops.force_teardown` over MCP;
   - the reconciler process, and `ops.reconcile_now` running a pass in the server process.
2. **Invariants and assets at stake**
   - one `{uid}:teardown` row per System; a live row is never reset;
   - no teardown enqueue or recycle while `reprovisioning` (#2928);
   - ADR-0441's exclusion of `failed` Systems from the orphan lane.
3. **Accepted failure classes**
   - dedupe is per process: a restart, or `ops.reconcile_now` in another process, warns once
     more per stranded System; bounded by process count and restarts.
   - the stranded query is unbounded like the orphan candidate query it mirrors; the set is
     Systems awaiting an operator and does not grow per pass.
   - `ops.force_teardown` keeps the original row's `authorizing` principal on recycle; the
     break-glass audit row records the caller (as for #2929).
4. **Covered elsewhere**
   - authority teardown recycle: #2917 / ADR-0620; investigation force-close: stays `NEVER`;
   - `tearing_down` Systems: `repair_stalled_tearing_down_systems`;
   - a `canceled` row on an orphaned System stays an operator stop and still replays.

## Considered and rejected

- **Bounded recycle in the lane.** judgment: operator chose warn-plus-metric; a recycle re-runs a
  teardown the handler already refused, with no new evidence.
- **Persist the dedupe (audit row or column).** judgment: a migration or audit write per pass for
  a log-noise bound; the counter carries the durable signal.
- **Default `recycle=NEVER` on the helper.** judgment: the issue is that a caller inherited a
  policy silently; a required argument makes each choice visible.
