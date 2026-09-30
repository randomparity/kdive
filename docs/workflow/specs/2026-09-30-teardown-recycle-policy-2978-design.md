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
   keyword-only `recycle: JobRecyclePolicy`, so no caller inherits a policy by accident. The
   preactivation-authority branch ignores it (unchanged). On the ordinary branch it recycles only
   an ordinary prior row: a prior `{uid}:teardown` row whose payload carries
   `authority_system_v1` or `external_boot_authority_v1` is enqueued with `NEVER`, because the
   recycle UPDATE overwrites `payload` and would strip the marker the authority re-run path
   (#2917, ADR-0620) keys on. Policies per caller are listed once, in the ADR-0435 amendment.
2. `ops.force_teardown` passes `FAILED`, and refuses a `reprovisioning` System under the System
   lock, after the `torn_down` short-circuit and before the enqueue: `conflict`,
   `data.current_status = "reprovisioning"`, `suggested_next_actions = ["systems.get"]`, no job
   write. The break-glass audit row is already committed, as for every other outcome.
3. `repair_orphaned_systems` passes `NEVER`, which already leaves a `failed` row untouched; only
   its stale comment changes.
4. New read-only lane `report_stranded_orphan_teardowns` (catalog name
   `stranded_orphan_teardowns`, after `abandoned_jobs`) selects orphaned Systems (Allocation
   terminal) not in `torn_down`/`failed`/`reprovisioning`/`tearing_down` whose `{uid}:teardown`
   row is `failed` (`tearing_down` belongs to `repair_stalled_tearing_down_systems`, which
   recycles). Per System not already warned about the same failure it logs one WARNING with the
   System, job, `error_category`, and remedy (`systems.teardown` for an unmarked row,
   `systems.get` for an authority-marked one), and returns the count of new warnings, which the
   loop adds to `kdive.reconciler.repairs{repair_kind="stranded_orphan_teardowns"}`.
5. Dedupe: a module-level `dict[UUID, datetime]`, System id → the failed row's `updated_at` last
   warned about, replaced each pass by the current stranded set. A row recycled and failed again
   (new `updated_at`) warns again. No persistence.

## Success

- `ops.force_teardown` on a `ready` or `failed` System whose unmarked row is `failed` returns that
  job id `queued` with `attempt = 0`; a `failed` row carrying `authority_system_v1` keeps its
  state and payload.
- `ops.force_teardown` on a `reprovisioning` System returns `conflict` and leaves the row (absent
  or `failed`) unchanged.
- For an orphaned `ready` System with a `failed` row: `repair_orphaned_systems` leaves the row
  unchanged; one reconcile pass reports `repair_counts["stranded_orphan_teardowns"] == 1` with one
  WARNING, the next reports 0 with none; a new failure (changed `updated_at`) reports 1 again; a
  `tearing_down` System with a `failed` row reports 0.

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
   - authority teardown recycle: #2917 / ADR-0620 (break-glass never recycles a marked row);
   - investigation force-close: stays `NEVER`;
   - `tearing_down` Systems: `repair_stalled_tearing_down_systems`;
   - a `canceled` row on an orphaned System stays an operator stop and still replays.

## Considered and rejected

- **Bounded recycle in the lane.** judgment: operator chose warn-plus-metric; a recycle re-runs a
  teardown the handler already refused, with no new evidence.
- **Persist the dedupe (audit row or column).** judgment: a migration or audit write per pass for
  a log-noise bound; the counter carries the durable signal.
- **Default `recycle=NEVER` on the helper.** judgment: the issue is that a caller inherited a
  policy silently; a required argument makes each choice visible.
