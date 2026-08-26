# Hosted TCG provision readiness implementation plan

## Goal

Make the hosted Ubuntu 26.04 ppc64le provision boundary self-diagnosing, use one hosted run to
identify the first broken boundary, correct that source cause, then prove `ready` and the named SSH
reachability test on another hosted run.

The architecture keeps evidence at the persisted queue, worker claim, and provider-stage boundaries.
The live-stack workflow reads bounded metadata before cleanup; the worker/provider journal names
execution progress. No public API or schema changes.

**Tech stack:** Bash, GitHub Actions YAML, Python 3.13/3.14, psycopg 3, pytest.

## Global constraints

- Base branch: `main`; branch: `feat/hosted-tcg-readiness-2056`.
- Host architecture: `x86_64`; declared targets: `x86_64`, `ppc64le`; relationship: `included`.
- The hosted proof image is Ubuntu 26.04 and the guest proof is ppc64le under TCG.
- Keep the provision state deadline at 600 seconds unless a hosted measurement shows ongoing work
  past it. The post-ready 900-second SSH budget is not provision-timing evidence.
- Log no payloads, authorizing records, DSNs, paths, XML, guest output, or credentials.
- Diagnostics are bounded and observational; they cannot replace the spine exit status.
- Preserve the nonzero-proof guard and require
  `test_ppc64le_guest_is_ssh_reachable_over_the_wire` to pass.
- No migration. Do not modify issue #2069, #2072, #2087, or #2089 files. Do not merge.
- Guardrails: `just lint`, `just type`, `just test`, `prek run`, `just ci`.

## Task 1: Prove the persisted queue boundary

**Files**

- Create `scripts/live-stack/provision-queue-diagnostics.sh`.
- Modify `.github/workflows/live.yml`.
- Modify `tests/scripts/test_live_stack_scripts.py`.
- Modify `tests/scripts/test_live_workflow_shape.py`.

**Interfaces**

- Consumes `KDIVE_SERVER_DATABASE_URL` from `scripts/live-stack/env.sh`.
- Produces a TSV report with this exact header:
  `system_id system_state job_id dispatch_lane job_state attempt worker_id enqueued_at claimed_at lease_expires_at`.
- Workflow callers need only the script exit status and stdout/stderr; no sourced output.

**Steps**

1. Add a failing script-shape test that requires `set -euo pipefail`, sources `env.sh`, passes the
   server DSN only through environment, uses a five-second connection timeout, starts a read-only
   transaction, sets a five-second local statement timeout, filters `kind = 'provision'` and
   `systems.state = 'provisioning'`, orders newest-first, limits 20 rows, and never selects
   `payload`, `authorizing`, `failure_context`, or a DSN. Run:
   `uv run pytest tests/scripts/test_live_stack_scripts.py -k provision_queue_diagnostics -q`.
   Expected: failure because the script does not exist.
2. Create the script. Its embedded Python uses `psycopg.connect(os.environ["KDIVE_SERVER_DATABASE_URL"], connect_timeout=5)`,
   `conn.set_read_only(True)`, and one literal query. It formats `None` as `NONE`, prints the fixed
   header, and emits at most 20 rows. No exception is swallowed: Python names the failed boundary
   and the caller receives nonzero.
3. Run the focused script test. Expected: pass.
4. Add failing workflow-shape tests requiring an always-run hosted queue snapshot before cleanup and
   a failure/cancellation snapshot in both existing lifecycle diagnostic steps, all inside the
   existing stop-commands shield with warn-on-unavailable behavior. Run:
   `uv run pytest tests/scripts/test_live_workflow_shape.py -k provision_queue -q`.
   Expected: failure because the workflow has no snapshot invocation.
5. Update the hosted job with a small `if: always()` queue-evidence step before failure-only journal
   capture and cleanup. Invoke the same script inside both existing diagnostic steps. Every wrapper
   records `provision queue diagnostics were unavailable` on nonzero and exits zero where the
   original diagnostic contract is observational.
6. Run both focused files:
   `uv run pytest tests/scripts/test_live_stack_scripts.py tests/scripts/test_live_workflow_shape.py -q`.
   Expected: all tests pass.
7. Commit explicit paths with subject `feat(live): expose persisted provision queue evidence`.

## Task 2: Prove the worker and provider execution boundaries

**Files**

- Modify `src/kdive/jobs/worker.py`.
- Modify `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`.
- Modify `tests/integration/live_stack/test_spine.py` only if a reusable formatter seam is required;
  otherwise the hosted journal is the behavioral proof and no incidental unit API is introduced.

**Interfaces**

- Worker startup log: `worker <id> accepting dispatch lanes: <comma-separated lanes>`.
- Claim log: `worker <id> claimed job <id> kind=<kind> lane=<persisted lane> attempt=<n> enqueued_at=<timestamp> claimed_at=<timestamp> queue_delay_s=<seconds>`.
- Provider stage log: `local-libvirt provision system=<id> job=<id-or-NONE> stage=<stage>` where stage
  is one of `resolve-arch`, `materialize-rootfs`, `prepare-baseline`, `prepare-overlay`,
  `customize-overlay`, `prepare-console`, or `define-start`.

**Steps**

1. Add the worker startup INFO line after configuration validation and before claim loops start.
   Add the claim INFO line immediately after `queue.dequeue` returns a row, using that row's
   persisted `dispatch_lane`, `created_at`, and `heartbeat_at`; clamp only a negative clock anomaly
   to zero for the displayed delay while preserving both timestamps.
2. Add a private `_log_provision_stage(system_id, job_id, stage)` helper and call it immediately
   before each named synchronous stage. Do not log arguments or results beyond ids and the fixed
   stage token.
3. Run the existing focused worker and local-libvirt test selections that exercise `run_once` and
   `provision`; expected result is pass with behavior unchanged.
4. Commit explicit paths with subject `feat(live): name provision execution boundaries`.

## Task 3: Dispatch, diagnose, and revise the durable design

**Files**

- Update ADR 0581, the design spec, and this plan with the hosted run id, exact row timing, last
  observed stage, root cause, and selected correction.
- Modify only the source file at the first broken boundary and its direct allowed test.

**Interfaces**

- The classification contract is deterministic:
  - queued + no `worker_id`/`claimed_at` selects worker readiness/claiming;
  - running + claim timing + no provider stage selects handler dispatch;
  - running + last provider stage selects that provider stage;
  - terminal selects its existing categorized failure instead of a timeout.

**Steps**

1. Push the two evidence commits and dispatch `.github/workflows/live.yml` on this branch with the
   committed TCG image default. Wait for `live_vm_tcg (hosted)` to complete.
2. Read the queue snapshot, System timeline, and worker journal together. Record the hosted run id,
   persisted lane, enqueue/claim timestamps, queue delay, job state, worker id, and last provider
   stage in ADR 0581 and the spec. The first missing successor is the diagnosed boundary.
3. Update this plan with one concrete correction task naming the exact function, failing test,
   implementation, and expected focused output before changing source. Re-run the ADR/spec/plan
   review because the diagnosis settles the second-stage decision.
4. Write the failing regression test at the diagnosed boundary and run it alone. Expected: failure
   on current source for the hosted-observed cause.
5. Apply the minimal source correction and run the same test. Expected: pass.
6. Commit the revised design and source correction as separate conventional commits.

## Task 4: Prove the hosted behavior and finish the quest

**Files**

- No new surface unless the hosted evidence exposes a defect in the already named boundary.

**Steps**

1. Run the focused script, worker, provider, and live-stack tests. Expected: pass.
2. Run repository guardrails only when the campaign orchestrator sequences the shared database test
   environment: `just lint`, `just type`, `just test`, `prek run`, `just ci`. Expected: exit 0 for
   each bare command.
3. Dispatch the hosted workflow from the corrected branch. Require the queue row to show the fixed
   worker claim, the timeline to reach `ready`,
   `test_ppc64le_guest_is_ssh_reachable_over_the_wire` to pass, and the zero-proof gate to observe
   at least one passed proof. A skip or zero-proof guard success is not acceptance.
4. Adversarially review and simplify the branch without changing behavior, push, create a PR with
   `Closes #2056` only, wait for green CI, and verify it is mergeable. Set issue #2056 to
   `status:awaiting-merge`; stop without merging.

## Rollback

Reverting the branch removes only diagnostics and the selected source correction. There is no
persisted migration or external state to unwind. Hosted workflow resources continue to use their
existing always-run cleanup.
