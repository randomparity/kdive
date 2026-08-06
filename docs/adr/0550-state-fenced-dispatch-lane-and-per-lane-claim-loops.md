# 0550 — State-fenced job kinds get their own dispatch lane, drained by a per-lane claim loop

## Status

Accepted (2026-08-06)

## Context

Three tools move a durable object into a transient state **inside the enqueue transaction**,
before the job they enqueue has been claimed by anything:

- `systems.restore` sets `SystemState.RESTORING`
  (`src/kdive/mcp/tools/lifecycle/systems/snapshot.py`) — `systems.delete_snapshot` then rejects
  with `reason: system_restoring`, and `systems.snapshot` / `systems.restore` reject because the
  System is not `ready`.
- `systems.reprovision` sets `SystemState.REPROVISIONING`
  (`src/kdive/mcp/tools/lifecycle/systems/admin.py`) — same rejections, same reason.
- `systems.snapshot` inserts the ledger row as `SnapshotState.CREATING` — `systems.delete_snapshot`
  rejects that snapshot until the capture finishes.

For those three, the job's **queue wait** is time the object spends fenced and unusable, not merely
time the agent spends waiting. Two facts made that window unbounded:

- **One lane for everything.** `jobs.dispatch_lane` and the worker's `accepted_lanes` dequeue
  boundary have existed since ADR-0018 / migration `0066`, but no caller anywhere in `src/` passed
  `dispatch_lane` and no production `WorkerConfig` set `accepted_lanes`. Every kind shared
  `default`, and `dequeue` orders `created_at` across it.
- **No queued-job timeout.** `repair_abandoned_jobs` reaps only `running` rows with a lapsed lease.
  A `queued` job waits indefinitely.

So a restore that queues behind an `image_build` admitted while the System sat settled leaves that
System fenced for the build's whole duration. ADR-0447 made this easier to reach — a re-dated
retry queues at the back rather than being claimed next — and records the trade in its
Consequences. Nothing here is lost or corrupted; the cost is availability of one object.

The obvious consumer for a second lane is a second worker deployment, and that is what issue #1538
suggested. It does not work here. A second worker process set is not a supported deployment
concept:

- `deploy/helm/kdive/templates/worker-death-rbac.yaml` grants `get`/`patch` on exactly
  `<fullname>-worker-<ordinal>`, and the lifecycle witness takes a single
  `KDIVE_KUBERNETES_WITNESS_WORKER_NAME` and sweeps `f"{worker_name}-{ordinal}"`
  (`src/kdive/processes/lifecycle/kubernetes_termination_witness.py`). A second StatefulSet's Pods
  are invisible to the witness, so their `kdive.io/worker-termination-evidence` finalizer is never
  removed and they hang in `Terminating`.
- The worker incarnation credential is read from the fixed path
  `/run/kdive/worker-incarnation-credential` and is bound to one incarnation
  (`_validate_worker_incarnation`). The path is not configurable, and only the Kubernetes init
  container and the compose lifecycle mint it.

Reaching the second-worker shape therefore means changing the worker termination-evidence
protocol, which is a larger decision than the availability defect warrants.

## Decision

**Name the membership rule, then route on it.** `STATE_FENCED_JOB_KINDS` in
`src/kdive/domain/operations/jobs.py` holds `RESTORE`, `REPROVISION`, and `SNAPSHOT`, defined as
*the kinds whose enqueue transaction writes a transient state that another tool rejects on*. Those
kinds enqueue onto `STATE_FENCED_JOB_DISPATCH_LANE = "state-fenced"`. The lane is chosen from the
kind inside `queue.enqueue`, not passed by each caller: a caller-supplied lane makes the routing
opt-in at fourteen call sites, which is the same shape that left the column unused for the lane's
whole existence.

`delete_snapshot` stays on `default` even though a queued row of that kind makes
`_active_snapshot_op` reject a restore. It writes no state, so the rule that selects it would be
"queued presence is a rejection predicate" — a broader rule whose membership cannot be checked from
the enqueue site alone. `teardown` (its handler writes the state, not its enqueue) and `provision`
(no pre-existing object to fence) stay on `default` for the same reason.

**One worker process runs one claim loop per accepted lane.** `Worker.run` starts a task per lane
in `accepted_lanes`; each loop calls `dequeue` with its own single-lane list, so a claimed
`image_build` in the `default` loop cannot delay a `restore` in the `state-fenced` loop.
`accepted_lanes` becomes operator-configurable through `KDIVE_WORKER_ACCEPTED_LANES`, defaulting to
**every** lane a kind routes to.

This is safe to do in-process because the per-job path already holds no mutable worker state:
`_dispatch`, `_run_handler`, and `_heartbeat_loop` keep everything in locals and take their own
pool connections, `SecretRegistry` is thread-safe and reference-counted per scope (so one job's
`release` cannot unmask a value another job still holds), and the job fence is per-row
`(id, attempt, worker_id)` — nothing assumes a `worker_id` has at most one `running` job.

Two things do change and are part of this decision:

- **Pool sizing is now per lane.** Each in-flight job holds a handler connection and a heartbeat
  connection, so the constructor's `pool.max_size >= 2` guard becomes
  `>= 2 * len(accepted_lanes)`, raised as the same `ValueError` at construction.
- **The queue-depth gauge is labelled by lane.** `WorkerTelemetry` kept one `_last_depth` scalar;
  with two loops observing it, each would overwrite the other's count and the gauge would report a
  value belonging to neither lane. It becomes a per-lane mapping emitting one labelled
  `Observation` each.

**A guard test asserts every lane a kind routes to is in the default `accepted_lanes`.** A lane no
deployed worker accepts means those jobs never run, and that starvation is invisible until someone
notices a System fenced forever. The default accepting all lanes makes it unreachable by
construction; the test is what keeps it that way when a fourth lane is added.

## Consequences

- A System fenced by `restore` or `reprovision`, and a Snapshot fenced by `snapshot`, no longer
  waits on unrelated long work. The wait is now bounded by other `state-fenced` work only.
- **Each worker replica runs up to two jobs concurrently instead of one.** CPU, memory, and
  database connections per replica rise accordingly, without any operator action, on upgrade. An
  operator sizing replicas against the old one-job-per-process behavior is now under-provisioned;
  the chart's `worker.replicas` default is unchanged, so this is a capacity note, not a migration
  step. `KDIVE_WORKER_ACCEPTED_LANES=default,state-fenced` is the shipped default and setting it to
  a single lane restores the old footprint — at the cost of starving the omitted lane, which is why
  the guard test bounds the default rather than the setting.
- A pool whose `max_size` is below `2 * len(accepted_lanes)` now fails at worker construction
  rather than stalling every dispatch on connection acquisition. That converts a silent hang into a
  startup error, and it is a **new** startup failure for a deployment that pinned `max_size` to 2.
- No migration. The `dispatch_lane` column, its non-empty constraint, and the `accepted_lanes`
  dequeue predicate all already exist and are unchanged; only the values written to the column and
  the worker's lane set change.
- Existing `queued` rows stay on `default` and are drained by the `default` loop. A `restore`
  enqueued before the upgrade keeps its old lane and its old wait; nothing rewrites history.
- The kinds on `state-fenced` share one loop, so two fenced operations still serialize against each
  other. That is the intended behavior — they contend for the same objects anyway — but it means
  the lane is not a general priority mechanism and should not be extended into one.
- `STATE_FENCED_JOB_KINDS` is a fourth kind set beside `SYSTEM_FAILING_JOB_KINDS`,
  `CONTRIBUTOR_CANCELABLE_JOB_KINDS`, and `OPT_IN_DESTRUCTIVE_JOB_KINDS`. Its rule is deliberately
  different from all three and stated in its docstring, because the nearest neighbour
  (`SYSTEM_FAILING_JOB_KINDS`, scoped to handlers that write `SystemState.FAILED`) answers a
  different question and reusing it would have been wrong.
- The second-worker-set shape stays unavailable. Nothing here makes it harder, and nothing here
  makes it work; a deployment that wants physical isolation between lanes still needs the
  termination-evidence generalization this decision declined to take on.

## Considered & rejected

- **A dedicated worker deployment per lane.** The shape issue #1538 suggested, and the one
  `accepted_lanes` was designed for. Rejected on evidence: the Kubernetes death RBAC and the
  lifecycle witness are hard-keyed to one StatefulSet name, so a second set's Pods keep their
  termination-evidence finalizer forever, and the systemd path has no incarnation-credential
  minting to duplicate. Delivering it means changing the worker termination-evidence protocol —
  a decision with its own risk class, taken for an availability defect the in-process shape already
  closes.
- **Do nothing.** The fence is recoverable: the reconciler and the operator both have paths out,
  nothing is corrupted, and the issue is P3. Rejected because the recovery paths are manual or
  periodic while the fence is immediate and agent-visible, and because the mechanism was already
  built — the column and the dequeue boundary shipped in ADR-0018 and went unused, so the cost of
  using them is far below the cost of the decision that created them.
- **Lane-priority ordering in a single lane-agnostic loop.** Order `dequeue` by lane rank, then
  `created_at`. Much the smallest change and it needs no concurrency at all. Rejected as a partial
  fix: it only helps while the worker is idle. Once the long `image_build` is *running*, the single
  loop is busy and the fenced System waits for its whole duration — which is exactly the scenario
  reported.
- **A queued-job age cap.** Named in the issue as the weaker option. Rejected because it surfaces
  the wait without stopping it: the cap's expiry has to either fail the fenced operation (turning
  an availability problem into an error the agent must handle) or merely log, and neither shortens
  the fence.
- **Let each enqueue call site pass its own `dispatch_lane`.** The signature already accepts it, so
  this needs no dispatch logic. Rejected: the routing then depends on fourteen call sites
  remembering, a new fenced kind is on the wrong lane by default, and the guard test could only
  check the sites that already opted in. Deriving the lane from the kind makes the membership rule
  the single point of truth and the thing a reviewer reads.
- **Put `delete_snapshot` on the fenced lane too.** Its queued presence does block a restore via
  `_active_snapshot_op`. Rejected because the rule that admits it cannot be evaluated where the
  routing happens — "some other tool queries for rows of this kind" is a property of the readers,
  not of the enqueue — so the set would become a list maintained by inspection, which is what the
  stated rule exists to avoid.
