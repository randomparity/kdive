# State-fenced dispatch lane design (#1538)

Decision record: [ADR-0550](../../adr/0550-state-fenced-dispatch-lane-and-per-lane-claim-loops.md).

## Problem

`systems.restore`, `systems.reprovision`, and `systems.snapshot` each move a durable object into a
transient state **inside the enqueue transaction** — `SystemState.RESTORING`,
`SystemState.REPROVISIONING`, and `SnapshotState.CREATING` respectively. Other tools reject while
that state holds. So the enqueued job's *queue wait* is time the object is fenced and unusable, not
merely time the agent waits.

Every job kind shares the `default` dispatch lane (no caller in `src/` passes `dispatch_lane`, and
no production `WorkerConfig` sets `accepted_lanes`), `dequeue` orders `created_at` across that one
lane, and `repair_abandoned_jobs` reaps only `running` rows — a `queued` job waits indefinitely. A
restore that queues behind an `image_build` leaves its System fenced for the build's whole duration.

## Decision

Stated in full in ADR-0550. Summarized:

1. `STATE_FENCED_JOB_KINDS = {RESTORE, REPROVISION, SNAPSHOT}`, selected by the rule *the enqueue
   transaction writes a transient state that another tool rejects on*.
2. `queue.enqueue` derives the lane from the kind — `state-fenced` for those three, `default`
   otherwise — on both the insert and the `recycle_terminal` reset. Its `dispatch_lane` parameter
   is removed.
3. `Worker.run` starts one claim loop per accepted lane, so a claimed job in one lane cannot delay a
   claim in another.
4. `KDIVE_WORKER_ACCEPTED_LANES` makes the set operator-configurable, defaulting to every lane a
   kind routes to; a worker that omits a routed lane warns at startup.

## Success criteria

Each is falsifiable and has a test in the plan.

| # | Criterion | How it is observed |
|---|---|---|
| S1 | `restore`, `reprovision`, `snapshot` rows carry `dispatch_lane = 'state-fenced'` | Read `jobs.dispatch_lane` after each tool call |
| S2 | Every other active kind derives `dispatch_lane = 'default'` | Parametrized over `ACTIVE_JOB_KINDS - STATE_FENCED_JOB_KINDS` against the **derivation** alone — no payload needed. Reaching `enqueue` needs a valid `ActivePayloadModel` per kind, so the through-`enqueue` assertion covers one representative kind per lane, not all twenty |
| S3 | A worker holding a running `default` job still claims a queued `state-fenced` job | Two-lane worker; block the `default` handler, assert the fenced job reaches `running` |
| S4 | A single-lane worker claims nothing outside its lane | `accepted_lanes=("state-fenced",)`; a queued `default` job stays `queued` |
| S5 | Every lane a kind routes to is in the default `accepted_lanes` | Guard test over `STATE_FENCED_JOB_KINDS ∪ {default}` vs. the setting's default |
| S6 | `pool.max_size < 2 * len(accepted_lanes) + 1` raises at construction | `ValueError` naming both numbers |
| S7 | Queue depth is reported per lane, not overwritten across lanes | Two loops observe; assert two labelled observations with their own counts |
| S8 | `KDIVE_WORKER_ACCEPTED_LANES` rejects an unknown or blank lane | `config validate` surfaces the parse error |
| S9 | A recycled terminal job is re-routed to its kind's lane | Insert a `restore` row on `default`, drive it terminal, re-enqueue, assert `state-fenced` |
| S10 | A worker omitting a routed lane warns at startup naming it | `accepted_lanes=("default",)`; assert the warning names `state-fenced` |

## Edge cases and failure modes

- **Pre-existing `queued` rows.** Rows already `queued` at upgrade keep `dispatch_lane = 'default'`
  and are drained once by the `default` loop. Not rewritten; a pre-upgrade restore keeps its old
  wait. Nothing in the design depends on lane and kind agreeing for historical rows, and no reader
  derives a kind from a lane.
- **Pre-existing *terminal* rows.** These are the ones that matter, and they are why the recycle
  path re-derives the lane. `systems.restore` and `systems.snapshot` enqueue with
  `recycle_terminal=True` under a durable `dedup_key`, so without the re-derivation a
  System/snapshot pair restored once before the upgrade would keep recycling its `default` row
  forever — the fix would silently not apply to exactly the systems that had used the feature.
- **Rolling the worker back.** The upgrade is safe in both the queued and terminal directions; the
  *downgrade* is not, and it is an ordinary operational action. The previous worker's
  `accepted_lanes` default is `("default",)`, so it never claims a `state-fenced` row: every
  restore, reprovision, and snapshot enqueued after the upgrade and still `queued` at rollback sits
  there indefinitely, with its System pinned in `RESTORING`/`REPROVISIONING` or its Snapshot in
  `CREATING`. Nothing detects it — `repair_abandoned_jobs` reaps only `running` rows, so there is no
  sweep and no alert. **Procedure:** before rolling the worker back, drain the fenced lane, or move
  its queued rows over with
  `UPDATE jobs SET dispatch_lane = 'default' WHERE state = 'queued' AND dispatch_lane =
  'state-fenced'`. This belongs in the operator documentation, not only here.
- **A lane with no consumer.** The failure is silent and unbounded: the rows sit `queued` forever
  and the fenced object never recovers. S5 makes it unreachable while the default accepts all lanes;
  an operator who narrows `KDIVE_WORKER_ACCEPTED_LANES` opts into it knowingly, the setting's help
  text says so, and S10's startup warning names the omitted lane so the choice is visible in the
  log rather than only in the environment.
- **A single-lane worker fleet split by operators.** Supported and unchanged in risk: two processes
  each accepting one lane behave exactly as two loops in one process, minus the shared pool.
- **Duplicate `dedup_key` across lanes.** `dedup_key` is globally unique and lane-independent, so
  the conflict path returns the existing row whatever lane it carries. With the recycle re-deriving
  the lane, a row's lane converges on its kind's lane at the first post-upgrade recycle, and a kind
  can never end up split across two lanes.
- **A handler failure in one lane.** `_claim_loop` already catches per-iteration exceptions and
  sleeps, so an ordinary handler or database failure never ends a loop — each lane gets its own loop
  and a failing lane does not stop the other.
- **A loop task ending unexpectedly.** The case the catch does not cover. `asyncio.gather`
  propagates the first exception while leaving its siblings *running*, so a loop that dies on
  something outside the catch would leave the other orphaned behind a `run()` that has already
  returned — a worker advertising two lanes and serving one, which is the starvation case arriving
  by a different route. So `run()` cancels the remaining loops and returns, letting the process
  supervisor restart the worker: a restarted worker is better than a partial one, and the alternative
  (restarting the dead loop in place) risks a hot loop against whatever killed it.
- **Shutdown.** Both loops observe the same `stop` event; the process exits when both have drained
  their current job, which is the existing single-loop contract applied twice.
- **Lease and fence.** Two concurrently running jobs share one `worker_id`. The fence is per row
  (`id`, `attempt`, `worker_id`) and nothing assumes a `worker_id` has at most one `running` job, so
  no change is needed — but S3's test asserts both jobs complete under their own fences rather than
  assuming it.

## Threat model

The change is security-relevant on one axis only: it makes two jobs run concurrently in a process
that resolves secrets and holds a worker fence credential. It adds no entry point, no parsing of
untrusted input, and no permission grant.

**Boundaries.** No new trust boundary. Two existing ones are now crossed concurrently rather than
serially: (a) the worker→database fence boundary (`incarnation_credential` authenticates every
claim/heartbeat/complete/fail), and (b) the secret-resolution boundary (a handler registers resolved
secrets into `SecretRegistry` for its op's lifetime, and the redactor masks them).

**Actors.** The untrusted party is an authenticated tenant driving MCP tools; the worker itself and
its database credential are trusted. A tenant controls *which* jobs exist and their payloads, not
which lane consumes them — the lane is derived from the kind server-side, so a tenant cannot route
work onto a lane to starve or bypass anything.

**Controls.**

- *Fence boundary* — unchanged. Each job's writes are fenced on `(id, attempt, worker_id)`, which
  is per row and therefore already correct for two in-flight jobs. The existing control covers it;
  nothing new is added.
- *Secret boundary* — `SecretRegistry` is thread-safe and **reference-counted per scope**, so one
  job's `release` cannot unmask a value another concurrent job still holds. This is the property the
  concurrency depends on; the plan asserts it directly rather than trusting it.
- *Cross-job leakage in failure context* — `_failure_context(exc, self._secret_registry)` redacts
  against the whole registry snapshot, which under concurrency includes the other job's secrets.
  That is over-masking, the safe direction, and it is not a new behavior.

**Out of scope.** Physical isolation between lanes (a compromised handler in one lane shares the
process with the other) is not addressed — it was addressable only by the separate-worker shape
ADR-0550 rejected on deployment-coupling grounds, and the handlers already share a process today.
Denial of service by flooding one lane is not addressed; admission control and per-project quotas
are the existing controls for that and are unchanged.

## Out of scope

- No database migration. `jobs.dispatch_lane`, its non-empty constraint, and the `accepted_lanes`
  dequeue predicate exist and are unchanged.
- `delete_snapshot`, `teardown`, and `provision` stay on `default` (ADR-0550 states why).
- No queued-job age cap.
- No second worker deployment; no change to the termination-evidence protocol, the death RBAC, or
  the incarnation-credential path.
- No change to `dequeue` ordering within a lane.

## Testing

- `tests/jobs/test_queue.py` — lane derivation per kind (S1, S2) and on the recycle path (S9).
- `tests/jobs/test_worker.py` — concurrent claim across lanes (S3), single-lane isolation (S4),
  pool-size validation (S6), the omitted-lane warning (S10), and
  one-loop-exit-does-not-silently-shrink-the-worker.
- `tests/jobs/test_worker_telemetry.py` — per-lane queue depth (S7).
- `tests/domain/` — the membership guard (S5) and the rule's agreement with the enqueue-site
  routing.
- `tests/config/` — `KDIVE_WORKER_ACCEPTED_LANES` parsing and rejection (S8).
- `tests/security/` — concurrent scoped register/release over `SecretRegistry` keeps a value masked
  while any holder remains.

## Documentation

- `docs/guide/reference/config.md` is generated; regenerate via the `config-docs-check` recipe's
  generator after adding the setting.
- The worker capacity note (one in-flight job per accepted lane, two by default) belongs with the
  operator guidance for worker sizing, and the Helm chart README's worker section.
- The rollback procedure above belongs with the worker upgrade/downgrade guidance, since an operator
  reaching for a rollback will not be reading this spec.
