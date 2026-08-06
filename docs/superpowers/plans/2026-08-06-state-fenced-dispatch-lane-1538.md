# State-fenced dispatch lane — implementation plan (#1538)

Spec: [`../specs/2026-08-06-state-fenced-dispatch-lane-1538-design.md`](../specs/2026-08-06-state-fenced-dispatch-lane-1538-design.md)
Decision: [ADR-0550](../../adr/0550-state-fenced-dispatch-lane-and-per-lane-claim-loops.md)
(amends [ADR-0447](../../adr/0447-recycle-terminal-redates-created-at.md))

Branch `feat/state-fenced-dispatch-lane-1538`, base `main`.

## Guardrails

Run before every commit. CI invokes these recipes **individually**, so an aggregate pass is not
evidence for any one of them.

- `just lint` · `just type` (whole tree, `src` + `tests`) · `just test`
- `env -u FORCE_COLOR just ci` for the full gate — `FORCE_COLOR` makes `chart-version-check` fail
  before the suite runs.
- Doc/config gates this change touches: `just config-docs-check`, `just config-guard`,
  `just env-docs-check`, `just adr-status-check`, `just docs-links`, `just docs-paths`.
- Records gate: `RECORD_PROFILES="adr debt" BASE_SHA=origin/main .github/scripts/check-records.sh`.

Conventions that apply throughout: ruff line length 100; `ty` strict; prose follows the doc-style
rule in `AGENTS.md` (plain and factual, and "Milestone" for the release unit); cite ADR-0550 in the
modules and tests that implement it (`adr-status-check` requires the ADR be Accepted before a
`src/` or `tests/` citation, which it is).

## Task order

Tasks 1→6 are sequential: each depends on the symbol the previous one introduces. Task 7 depends on
1–6 landing. Nothing here is parallelizable across agents, and no task is independently shippable —
routing onto a lane no worker accepts (task 2 without task 4) is the starvation case, so **do not
commit task 2 without tasks 3–6 in the same branch**.

---

## Task 1 — the membership rule and the lane constant

**Where this fits.** ADR-0550's first decision: name the rule, then route on it. Everything else
reads the symbols this task adds.

**Task.** In `src/kdive/domain/operations/jobs.py`, beside the existing kind sets, add:

- `STATE_FENCED_JOB_DISPATCH_LANE = "state-fenced"` — a module constant, next to
  `DEFAULT_JOB_DISPATCH_LANE`.
- `STATE_FENCED_JOB_KINDS: frozenset[JobKind] = frozenset({JobKind.RESTORE, JobKind.REPROVISION,
  JobKind.SNAPSHOT})`. Its docstring states the selecting rule — *the kinds whose enqueue
  transaction writes a transient state that another tool rejects on* — names the three write sites
  (`snapshot.py` `RESTORING` and `CREATING`, `admin.py` `REPROVISIONING`), and says why
  `delete_snapshot`, `teardown`, and `provision` are excluded. Follow the tone of
  `SYSTEM_FAILING_JOB_KINDS`'s docstring, which does the same job for a different rule, and say
  explicitly that this set answers a different question from that one so neither is reused for the
  other.
- `def dispatch_lane_for_kind(kind: JobKind) -> str` — total over `JobKind`, returning the fenced
  lane for a member of the set and `DEFAULT_JOB_DISPATCH_LANE` otherwise.

Export all three from `__all__`.

**Files.** `src/kdive/domain/operations/jobs.py`, `tests/domain/operations/test_jobs.py` (create if
absent — mirror the package tree).

**Acceptance criteria.**

- `dispatch_lane_for_kind` returns `"state-fenced"` for exactly `RESTORE`, `REPROVISION`, `SNAPSHOT`
  and `"default"` for every other member of `JobKind`, asserted by parametrizing over `JobKind`
  (this is spec **S2**'s derivation half — no payloads needed).
- A test asserts `STATE_FENCED_JOB_KINDS <= ACTIVE_JOB_KINDS`, so a retired kind can never be routed.
- `just lint type test` green.

**Rollback.** Additive; revert the commit.

---

## Task 2 — derive the lane in `enqueue`, on both the insert and the recycle

**Where this fits.** ADR-0550's routing decision plus its recycle paragraph. This is the task the
issue is actually about.

**Task.** In `src/kdive/jobs/queue.py`:

1. **Remove** the `dispatch_lane: str = DEFAULT_JOB_DISPATCH_LANE` parameter from `enqueue` and the
   `if not dispatch_lane: raise ValueError(...)` guard that validated it. Compute
   `lane = dispatch_lane_for_kind(kind)` instead and bind it into the `INSERT`.
2. Add `dispatch_lane = %s` to the `recycle_terminal` `UPDATE`'s SET list, bound to the same `lane`,
   alongside `created_at = clock_timestamp()`. Extend that `UPDATE`'s surrounding docstring to say
   why: a row first inserted before this change keeps its old lane otherwise, and `restore` and
   `snapshot` both recycle under a durable `dedup_key`, so the routing would silently never apply to
   a System that had already used the feature.
3. Update `enqueue`'s docstring where it lists the fields the recycle resets, and the sentence
   naming `kind` and `dispatch_lane` as deliberately not-reset — `dispatch_lane` now *is* reset.
   Cite ADR-0550 in the module docstring beside the existing ADR-0018/ADR-0533 citations.

Callers pass no lane, so no call site changes.

**Files.** `src/kdive/jobs/queue.py`, `tests/jobs/test_queue.py`, `tests/support/worker_fence.py`
(its `_dequeue` helper's `accepted_lanes` default is unaffected, but check it does not enqueue with
a lane).

**Test-surface note — this is the part that will surprise you.** `tests/jobs/test_queue.py` and
`tests/jobs/test_worker.py` currently exercise the `accepted_lanes` boundary by enqueuing onto an
arbitrary lane (`"provider-b"`, `"provider-c"`). With the parameter gone they cannot. Replace those
with a direct `UPDATE jobs SET dispatch_lane = %s WHERE id = %s` after enqueue, or a small test
helper that does it — the boundary being tested is `dequeue`'s, not `enqueue`'s, so writing the
column directly tests the right thing. Do **not** re-add the parameter to keep the tests compiling;
ADR-0550 rejects it explicitly, and the seam is what would let production routing drift.

**Acceptance criteria.**

- **S1**: a `restore`, `reprovision`, and `snapshot` enqueued through `queue.enqueue` each land with
  `dispatch_lane = 'state-fenced'`.
- **S2** (through-`enqueue` half): one representative non-fenced kind lands on `'default'`.
- **S9**: insert a `restore` row, force `dispatch_lane = 'default'` directly, drive it to a terminal
  state, re-enqueue with `recycle_terminal=True`, and assert the row is `queued` **and** on
  `'state-fenced'`. Assert `created_at` was re-dated in the same test so ADR-0447's behavior is
  still covered.
- Mutation-check S9: revert the `dispatch_lane` addition to the `UPDATE` and confirm the test goes
  red, then restore it. A test that passes either way is not a guard.
- `just lint type test` green.

**Rollback.** Revert. No schema change, so nothing to undo in the database.

---

## Task 3 — `KDIVE_WORKER_ACCEPTED_LANES`

**Where this fits.** ADR-0550's operator-configurable lane set, defaulting to every routed lane.

**Task.** In `src/kdive/config/core_settings.py`, add a `Setting` in the existing style:

- `name="KDIVE_WORKER_ACCEPTED_LANES"`, `processes=_WORKER`, `group=` the group the other worker
  queue/lease knobs use.
- `parse` splits on `,`, strips each entry, and **rejects**: an empty result, any blank entry, and
  any entry not in the known-lane set (`{DEFAULT_JOB_DISPATCH_LANE, STATE_FENCED_JOB_DISPATCH_LANE}`).
  Rejecting an unknown lane is deliberate — a typo would otherwise produce a worker that accepts a
  lane nothing routes to while starving one that is routed.
- `default` is every lane any active kind derives to, written as a derived constant rather than a
  hand-typed string so task 1's set stays the single source of truth.
- `help` states the consequence of narrowing it: jobs on an omitted lane are never claimed, and the
  object they fence stays fenced.

Then regenerate the reference: `uv run python -c "from pathlib import Path; from
scripts.gen_config_reference import write_reference; write_reference(Path('docs/guide/reference/config.md'))"`
and commit the regenerated file — `just config-docs-check` diffs it.

**Files.** `src/kdive/config/core_settings.py`, `docs/guide/reference/config.md` (generated),
`tests/config/test_core_settings.py`.

**Acceptance criteria.**

- **S8**: the parser rejects an empty string, a blank entry (`"default,,"`), and an unknown lane
  (`"state_fenced"` with an underscore), each with a message naming the offending value.
- **S5**: a guard test asserts the set of lanes `dispatch_lane_for_kind` derives over every member of
  `ACTIVE_JOB_KINDS` is a subset of the setting's default value. This is the starvation guard — a
  fourth lane added without extending the default fails here.
- `just config-docs-check`, `just config-guard`, `just env-docs-check` all green **individually**.

**Rollback.** Revert, including the regenerated `config.md`.

---

## Task 4 — one claim loop per accepted lane

**Where this fits.** ADR-0550's consumer decision. Without this, task 2 routes onto a lane nothing
drains.

**Task.** In `src/kdive/jobs/worker.py`:

1. `WorkerConfig.accepted_lanes` keeps its type; its default becomes the same all-lanes tuple task 3
   uses.
2. Raise the constructor guard from `pool.max_size < 2` to
   `pool.max_size < 2 * len(config.accepted_lanes) + 1`. Keep the `ValueError` shape and extend the
   message to name the lane count and the `+ 1`, stating it is the readiness probe's connection —
   the probe shares this pool, and `run_once` skips `dequeue` while not ready, so a worker sized to
   exactly `2 * lanes` stops claiming under full dispatch.
3. `run_once` takes the lane it is claiming for and passes a **single-lane** sequence to both
   `queue.dequeue` and `queue.count_claimable`. `_claim_loop` takes the same lane and threads it
   through.
4. `run` starts one `_claim_loop` task per accepted lane alongside the existing heartbeat ticker.
   When any loop task ends unexpectedly, cancel the remaining loops and return, so the process
   supervisor restarts the worker — a worker serving fewer lanes than it advertises is the
   starvation case. `asyncio.gather` alone does not give this (it propagates the first exception and
   leaves siblings running), so cancel explicitly. Both loops observe the same `stop` event.
5. At construction, log a `warning` naming any lane in the routed set that `accepted_lanes` omits.
   A warning, not a refusal — a deliberate single-lane fleet is a shape ADR-0550 supports.

**Files.** `src/kdive/jobs/worker.py`, `tests/jobs/test_worker.py`.

**Acceptance criteria.**

- **S3**: a two-lane worker with a `default` handler blocked on an event still claims and runs a
  queued `state-fenced` job. Assert both jobs reach their own terminal state under their own fences
  — this is the criterion that proves the reported defect is fixed, so do not weaken it to "the
  fenced job was claimed".
- **S4**: a worker with `accepted_lanes=("state-fenced",)` leaves a queued `default` job `queued`.
- **S6**: `pool.max_size` one below `2 * len(lanes) + 1` raises `ValueError` naming both numbers;
  exactly at the floor constructs.
- **S10**: `accepted_lanes=("default",)` logs a warning naming `state-fenced` (assert on the record,
  via `caplog`).
- A loop task raising something outside `_claim_loop`'s catch cancels its sibling and ends `run`,
  asserted rather than assumed.
- `just lint type test` green. Watch for `ty` on the loop-task collection type.

**Rollback.** Revert. Note the branch is not safe to ship with task 2 but without this.

---

## Task 5 — label the queue-depth gauge by lane

**Where this fits.** ADR-0550's second named change. Two loops writing one scalar report a depth
belonging to neither lane.

**Task.** In `src/kdive/jobs/worker_telemetry.py`, replace `_last_depth: int` with a per-lane
mapping. `observe_queue_depth` takes the lane it observed; `_observe_depth` emits one `Observation`
per lane with a `dispatch_lane` attribute. Keep the metric **name** `kdive.job.queue.depth`
unchanged.

**Files.** `src/kdive/jobs/worker_telemetry.py`, `tests/jobs/test_worker_telemetry.py`,
`src/kdive/jobs/worker.py` (the `observe_queue_depth` call site).

**Acceptance criteria.**

- **S7**: two lanes observed with different counts produce two observations carrying their own
  counts and lane attributes — assert both, not just the count of observations.
- `just lint type test` green.

**Dashboard note, not a blocker.** Adding a label splits an existing series. Check
`deploy/grafana/kdive-overview.json` for a panel on `kdive.job.queue.depth`; if one exists and sums
across lanes it keeps working, but if it assumes a single series it needs a `sum by` or an explicit
aggregation. Fix it here if so — do not leave a dashboard reading one lane's depth as the total.

---

## Task 6 — wire the setting into the process and raise the pool

**Where this fits.** The composition root. Without it the setting is inert and the worker still runs
one lane.

**Task.** In `src/kdive/processes/worker.py`:

1. Read `KDIVE_WORKER_ACCEPTED_LANES` and pass it into `WorkerConfig(accepted_lanes=...)` beside the
   existing `heartbeat`/`readiness`/`telemetry` arguments.
2. **Raise `create_pool(min_size=2, max_size=4)`.** This is the one that bites: with two lanes the
   new floor is `2 * 2 + 1 = 5`, so the current `max_size=4` makes the worker raise at construction
   on every start. Size it from the configured lane count rather than a new literal, and give
   `min_size` matching headroom.

**Files.** `src/kdive/processes/worker.py`, `tests/jobs/test_worker_main.py` (asserts the built
`WorkerConfig`).

**Acceptance criteria.**

- The built `WorkerConfig.accepted_lanes` reflects the environment value, and the default when unset
  is every routed lane.
- A test asserts the pool the process builds satisfies the worker's own floor for the configured
  lane count — so the two numbers cannot drift apart again.
- `just lint type test` green.

**Rollback.** Revert; the pool size returns to 4, which is consistent with a reverted task 4.

---

## Task 7 — operator documentation

**Where this fits.** The spec's Documentation section. Two facts an operator cannot get from the
code.

**Task.**

1. **Worker sizing.** In the operator worker guidance and `deploy/helm/kdive/README.md`'s worker
   section: a replica now runs one in-flight job per accepted lane (two by default) where it ran
   one, so CPU, memory, and database connections per replica rise on upgrade with no operator
   action. Idle cost scales too — each loop polls every `poll_interval` whether or not work exists.
2. **Rollback.** The downgrade procedure, in order: stop the new workers; run
   `UPDATE jobs SET dispatch_lane = 'default' WHERE dispatch_lane = 'state-fenced' AND state IN
   ('queued', 'running')`; start the old workers. State why `running` is included (its lease has no
   claimant left and an old worker will not reclaim a lane it does not accept, and
   `repair_abandoned_jobs` does not dead-letter until `attempt >= max_attempts`) and why the
   ordering matters (running the `UPDATE` while new workers still claim moves rows out from under
   them). Put this where an operator reaching for a rollback will find it — the worker
   upgrade/downgrade guidance, not only the spec.

Operator docs do not use `just` recipes; give the underlying commands.

**Files.** the operator worker runbook, `deploy/helm/kdive/README.md`.

**Acceptance criteria.**

- `just docs-links`, `just docs-paths`, `just served-doc-links` green individually.
- The rollback `UPDATE` in the docs matches the spec's verbatim — a divergent copy is worse than one
  location.

**Rollback.** Revert.

---

## Definition of done

- All ten spec criteria (S1–S10) have a passing test, each named in its task above.
- `env -u FORCE_COLOR just ci` green, and the individually-gated recipes this change touches
  (`config-docs-check`, `config-guard`, `env-docs-check`, `adr-status-check`, `docs-links`,
  `docs-paths`, `served-doc-links`) green on their own.
- ADR-0550 is cited from the modules and tests implementing it.
- No `dispatch_lane` argument remains at any `queue.enqueue` call site or in its signature.
