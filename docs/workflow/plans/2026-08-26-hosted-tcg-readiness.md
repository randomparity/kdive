# Hosted TCG provision readiness implementation plan

## Goal

Make the hosted Ubuntu 26.04 ppc64le provision boundary self-diagnosing, obtain one usable hosted
run that identifies the first broken boundary, correct that source cause, then prove `ready` and
`tests/integration/test_live_stack.py::test_ppc64le_guest_is_ssh_reachable_over_the_wire` on
another hosted run.

The architecture keeps evidence at the persisted queue, worker claim, and provider-stage boundaries.
The live-stack workflow reads bounded metadata before cleanup; the worker/provider journal names
execution progress. No public API or schema changes.

**Tech stack:** Bash, GitHub Actions YAML, Python 3.13/3.14, psycopg 3, pytest.

## Global constraints

- Base branch: `main`; branch: `feat/hosted-tcg-readiness-2056`.
- Host architecture: `x86_64`; declared targets: `x86_64`, `ppc64le`; relationship: `included`.
- The hosted proof image is Ubuntu 26.04 and the guest proof is ppc64le under TCG.
- Keep the provision state deadline at 600 seconds. A change requires two completed hosted
  job-claim-to-System-ready intervals; use the larger total plus 50 percent, capped at 900 seconds.
  An incomplete interval, a margin above the cap, and the post-ready SSH budget authorize no change.
- Log no payloads, authorizing records, DSNs, paths, XML, guest output, or credentials.
- Diagnostics are bounded and observational; they cannot replace the spine exit status.
- Preserve the nonzero-proof guard and require
  `test_ppc64le_guest_is_ssh_reachable_over_the_wire` to pass.
- No migration. Do not modify issue #2069, #2072, #2087, or #2089 files. Do not merge.
- Guardrails: `just lint`, `just type`, `just test`, `prek run`, `just ci`.

## Task 1: Prove the persisted queue boundary

**Files**

- Create `scripts/live-stack/filter-worker-readiness-evidence.py`.
- Modify `scripts/build-capture-bootstrap-manifest.py`.
- Create `scripts/live-stack/provision-queue-diagnostics.sh`.
- Modify `.github/workflows/live.yml`.
- Modify `tests/scripts/test_live_stack_scripts.py`.
- Modify `tests/scripts/test_live_workflow_shape.py`.
- Modify `tests/integration/live_stack/spine.py` and
  `tests/integration/live_stack/test_spine.py`.
- Modify `tests/integration/test_live_stack.py`.

**Interfaces**

- Consumes `KDIVE_SERVER_DATABASE_URL` from `scripts/live-stack/env.sh` and a target file containing
  `<job UUID><TAB><System UUID><LF>`.
- Produces exactly one TSV row under the literal tab-separated header
  `system_id<TAB>system_state<TAB>job_id<TAB>dispatch_lane<TAB>job_state<TAB>attempt<TAB>worker_id<TAB>enqueued_at<TAB>last_heartbeat_at<TAB>lease_expires_at`.
- Workflow callers consume only the exit status and stdout/stderr; no output is sourced.

**Steps**

1. Add failing tests for `record_provision_evidence_target(path, job_id, system_id)`: it opens the
   target with `O_CREAT|O_EXCL`, mode 0600; writes the exact two-UUID record; refuses any existing
   target; and exposes an interrupted partial record for the consumer's malformed-input rejection
   rather than inventing recovery. Run
   `uv run pytest tests/integration/live_stack/test_spine.py -k provision_evidence_target -q`.
   Expected: fail because the helper does not exist.
2. Implement the private single-writer helper and call it in the named SSH proof immediately after
   `systems.provision` supplies both ids. Re-run the selection; expected: pass.
3. Add failing script tests that require `set -euo pipefail`, source `env.sh`, validate the target's
   exact two UUIDs, pass the server DSN only through environment, use five-second connection and
   statement timeouts, and open a read-only transaction. Require an exact join on both job id and
   `payload.system_id`, regardless of current System state; nonzero on malformed, mismatched, zero,
   or multiple results; and no selection of raw payload, authorizing, failure context, or DSN.
   Every failure is exactly one sanitized line `provision-evidence-error code=<fixed-code>` of at
   most 100 bytes, never a Python traceback/path/DSN. Run
   `uv run pytest tests/scripts/test_live_stack_scripts.py -k provision_queue_diagnostics -q`.
   Expected: fail because the script does not exist.
4. Create the script. Its embedded Python uses
   `psycopg.connect(os.environ["KDIVE_SERVER_DATABASE_URL"], connect_timeout=5)`, opens a read-only
   transaction, sets the local statement timeout, and runs one literal exact-target query. It
   formats `None` as `NONE`, prints the fixed header and one row, and maps every failure to the
   tested fixed code without dynamic exception text.
5. Run the focused script test. Expected: pass.
6. Add failing workflow-shape tests requiring the hosted spine and always-run evidence step to use
   the same `$RUNNER_TEMP` target path, with the evidence step after pytest but before diagnostics
   and cleanup, inside a stop-commands shield. Require GNU
   `timeout --signal=TERM --kill-after=2s 12s` around the entire script invocation (including env
   sourcing/import/connect/teardown) and a fixed wrapper warning on timeout/nonzero. Run:
   `uv run pytest tests/scripts/test_live_workflow_shape.py -k provision_queue -q`.
   Expected: failure because the workflow has no target wiring or bounded snapshot invocation.
7. Export `KDIVE_PROVISION_EVIDENCE_TARGET` into the hosted spine and add the `if: always()`
   queue-evidence step before failure-only journal capture and cleanup. It invokes the exact target
   through the required outer timeout, emits only the fixed warning on nonzero, and exits zero so
   the spine remains authoritative.
8. Run the focused files:
   `uv run pytest tests/integration/live_stack/test_spine.py tests/scripts/test_live_stack_scripts.py tests/scripts/test_live_workflow_shape.py -q`.
   Expected: all tests pass.
9. Commit explicit paths with subject `feat(live): expose persisted provision queue evidence`.

## Task 2: Prove the worker and provider execution boundaries

**Files**

- Modify `src/kdive/jobs/worker.py`.
- Modify `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`.
- Modify `tests/integration/live_stack/test_spine.py` with focused log-contract tests using
  injected/model seams; do not introduce a public formatter API.

**Interfaces**

- Worker startup log: `worker <id> accepting dispatch lanes: <comma-separated lanes>`.
- The authoritative claim timestamp is the `heartbeat_at` value returned by `dequeue` in the same
  database function call that changes the row from queued to running. The worker copies that value
  and the other immutable claim fields immediately, but emits the journal record only after the
  pooled connection context exits successfully and commits.
- Provision-claim log only:
  `worker <id> claimed provision job <id> lane=<persisted lane> attempt=<n> enqueued_at=<timestamp> claim_at=<initial-dequeue-heartbeat timestamp> queue_delay_s=<seconds>`.
- Provider stage log:
  `local-libvirt provision system=<id> job=<id-or-NONE> stage=<stage> event=<start|complete>`.
  Exact spans and exception semantics:
  - `resolve-arch`: `_resolve_guest_arch(profile.arch)`;
  - `materialize-rootfs`: `_materialize_rootfs(...)`;
  - `prepare-baseline`: `_prepare_baseline_kernel(...)`;
  - `prepare-overlay`: `self._files.prepare_overlay(...)`;
  - `render-domain`: gdb/SSH port reuse plus `render_domain_xml(...)`;
  - `customize-overlay`: the complete `for customize in overlay_customizers` loop;
  - `prepare-console`: `self._files.prepare_console(system_id)`;
  - `define-start`: `self._define_and_start(xml, system_id)`.
  Every span logs `start` immediately before entry and `complete` only after normal return; an
  exception deliberately leaves the start unmatched.

**Steps**

1. Add failing tests for the worker startup and provision-claim records. Construct a provision
   `Job` with distinct persisted lane, enqueue timestamp, initial dequeue `heartbeat_at`, and
   attempt; assert every field appears, payload/authorizing values do not, and a negative clock
   anomaly displays `queue_delay_s=0` without changing either timestamp. Assert a later
   connection-exit mutation cannot alter the captured fields, publication follows successful
   connection-context exit, an unrelated job kind emits no claim INFO, and a queue-depth telemetry
   failure emits no claim. Add a real pooled-Postgres regression proving the failed transaction
   leaves the job queued and the journal claim absent. Run:
   `uv run pytest tests/integration/live_stack/test_spine.py -k 'worker_claim or worker_lanes' -q`
   and
   `uv run pytest tests/jobs/test_worker.py::test_claim_journal_is_absent_when_real_dequeue_transaction_rolls_back -q`.
   Expected before the fix: ordering/no-claim assertions fail; expected after the fix: pass.
2. Immediately after `dequeue`, copy the provision claim's immutable fields into a frozen value.
   Emit it only after successful pooled connection-context exit. Leave the queue-depth query inside
   the same transaction so any telemetry exception rolls back the dequeue and reaches no journal
   publication. Re-run; expected: pass.
3. Add a failing provider-stage test around the existing injected provision seams. Assert paired
   `event=start`/`event=complete` records in exact stage order, a missing completion when a stage
   raises, and that records omit profile data, paths, XML, guest output, and credentials. Run:
   `uv run pytest tests/integration/live_stack/test_spine.py -k provision_stage -q`.
   Expected: fail because stage records do not exist.
4. Add a private synchronous stage context manager and wrap each named operation. It logs start
   immediately before the operation and completion only after it returns. Log only ids, the fixed
   stage token, and the fixed event token. Re-run the selection; expected: pass.
5. Commit explicit paths with subject `feat(live): name provision execution boundaries`.

## Task 3: Dispatch, diagnose, and revise the durable design

**Files**

- Update ADR 0581, the design spec, and this plan with the hosted run id, exact row timing, last
  observed stage, root cause, and selected correction.
- Modify only the source file at the first broken boundary and its direct allowed test.

**Interfaces**

- The classification contract is deterministic:
  - queued + no worker and no matching claim record selects worker readiness/claiming;
  - running + no matching claim record selects the dequeue-to-claim-record publication boundary
    and is unusable for claim-timing proof;
  - running + matching immutable claim record + no provider stage selects handler dispatch;
  - running + last unmatched provider-stage start selects that exact mapped call;
  - terminal selects its existing categorized failure instead of a timeout.

**Evidence-selected correction**

- Hosted run
  [32981623561](https://github.com/randomparity/kdive/actions/runs/32981623561/job/98219425910)
  persisted the exact provision job on `default` at `2026-08-26T14:51:00.739273Z`; it remained
  `queued`, attempt 0, with no worker, heartbeat, lease, claim record, or provider stage. The fixed
  worker advertised `default,state-fenced` at `14:50:18.567292Z`.
- The source boundary is `Worker.run_once` returning before `dequeue` when the worker probe is not
  ready. `run_worker` unconditionally includes `verify_capture_bootstrap_manifest`, but the hosted
  lifecycle path installed the fixed-worker venv without its matching default-path manifest.
- The focused regression is
  `tests/scripts/test_live_workflow_shape.py::test_tcg_installs_manifest_for_the_fixed_worker_before_starting_it`.
  It requires build, privileged install, runtime-identity verification, root:root mode 0644, and
  ordering after venv installation but before lifecycle proof or spine startup.
- The implementation adds one `.github/workflows/live.yml` step that builds from the fixed venv's
  site-packages, installs `/usr/share/kdive/capture-bootstrap-manifest.json`, verifies it against
  the same interpreter/source root, and checks ownership and mode. Expected focused output:
  `1 passed`.
- The hosted TCG lifecycle diagnostics step runs under `if: always()` before cleanup so its bounded
  journal exposes the immutable provision `claim_at` and provider stages on a successful final
  proof. The shared workflow-shape test requires this success path only for `tcg`; native capture
  keeps its existing failure/cancellation condition.
- The hosted TCG capture pipes retained system-journal JSON through
  `scripts/live-stack/filter-worker-journal-evidence.py`, which full-matches only fixed lane,
  provision-claim, and provider-stage messages and emits nothing else. A no-safe-record result is
  nonzero and becomes the workflow's existing fixed warning without replacing the spine verdict.
  The separate native proof keeps its pre-existing direct failure/cancellation journal capture.

**Second evidence-selected correction**

- Exact-head Ubuntu 26.04 run
  [33013068295](https://github.com/randomparity/kdive/actions/runs/33013068295/job/98324100356)
  retained the exact queued `default` provision row and reported only
  `capture_bootstrap_manifest=false`; Postgres, MinIO, and capture recovery were true.
- The root-side manifest build/install/producer verify and leaf `0:0:0644` assertion passed.
  Diagnostic run
  [33017429217](https://github.com/randomparity/kdive/actions/runs/33017429217/job/98339160715)
  then reported `/usr/share/kdive`, uid/gid `0:0`, mode `0777`, and
  `fingerprint_ancestor_replaceable` from the exact verifier under `kdive-worker-1`.
- This proves the destination-parent/ambient-umask hypothesis: `_atomic_write` inherited mode
  `0777`, the root producer validated only the leaf, and unprivileged no-follow readiness rejected
  the replaceable parent.
- `test_manifest_install_closes_root_producer_worker_consumer_mode_gap` drives the same `_install`
  entry twice under umask `000`, with only `_prepare_install_parent` disabled in the legacy arm.
  That arm reproduces parent mode `0777`, root-producer success, and runtime-verifier rejection;
  the corrected arm requires parent mode `0755` and verifier success. Removing the normalization
  call makes the corrected arm fail.
- Retain the bounded readiness observation plus the fixed parent/reason diagnostic. Run
  `verify_capture_bootstrap_manifest` under `kdive-worker-1` before any fixed worker starts, so an
  accepted producer result proves the actual readiness identity and verifier.

**Steps**

1. Push the two evidence commits and dispatch `.github/workflows/live.yml` on this branch with the
   committed TCG image default. Wait for `live_vm_tcg (hosted)` to complete.
2. Accept the run only when the exact-target snapshot agrees with the state timeline and journal:
   target/job/System ids match; persisted lane/worker/attempt match the immutable claim record; its
   `claim_at` is the initial dequeue timestamp and is no later than the snapshot's mutable
   `last_heartbeat_at`; and stage pairs follow the mapped order. If diagnostic infrastructure alone
   is unavailable, redispatch the unchanged commit once. If the second result is unavailable,
   ambiguous, inconsistent, or identifies excluded surface, park with no source/deadline change.
3. Treat the first missing successor as localization, not cause. Trace from the exact target through
   `enqueue`/`dequeue`, handler dispatch, and the mapped provider span; reproduce the localized
   failure with the smallest focused seam or hosted command; rule out a missing/malformed diagnostic
   record; and record the exact source statement/invariant whose behavior caused the stall.
4. Record the run id, lane, enqueue/claim/last-heartbeat timestamps, delay, state, worker, last stage,
   reproduction, ruled-out instrumentation failure, and root source cause in ADR 0581 and the spec.
5. Update this plan with one concrete correction task naming the exact function, failing test,
   implementation, and expected focused output. Re-run ADR/spec/plan review.
6. Write the failing regression test at the diagnosed source boundary; run it alone and require it
   to fail for the hosted-observed cause.
7. Apply the minimal source correction and run the same test; require pass.
8. Commit revised design and source correction separately.

## Task 4: Prove the hosted behavior and finish the quest

**Files**

- `src/kdive/jobs/worker.py`
- `src/kdive/jobs/capture_operations/bootstrap_attestation.py`
- `src/kdive/jobs/capture_operations/bootstrap_elf.py`
- `.github/workflows/live.yml`
- `deploy/systemd/install-live-worker-lifecycle.sh`
- `tests/integration/live_stack/test_spine.py`
- `tests/jobs/test_worker.py`
- `tests/deploy/test_live_worker_provisioning.py`
- `tests/jobs/capture_operations/test_manifest.py`
- `tests/scripts/test_live_workflow_shape.py`

Exact-head hosted run
[32998642219](https://github.com/randomparity/kdive/actions/runs/32998642219/job/98274351467)
failed before worker startup because Ubuntu 26.04 emitted the valid unnamed-vDSO loader line
`(0x...)`. Add `test_loader_list_parser_ignores_address_only_virtual_mapping`, require it to fail
with the hosted error, then make `_LOADER_VIRTUAL_RE` admit the syntax-checked address-only form
without weakening file-backed mapping checks. The focused parser selection must report `5 passed`.

Run [33003146430](https://github.com/randomparity/kdive/actions/runs/33003146430/job/98289891730)
exposed a replaceable ancestor after the installer normalized recursive ownership. Run
[33004795604](https://github.com/randomparity/kdive/actions/runs/33004795604/job/98295552657)
repeated the generic rejection after descendant write-bit hardening. A temporary diagnostic then
made run
[33005759211](https://github.com/randomparity/kdive/actions/runs/33005759211/job/98298954459)
identify the rejected ancestor's numeric ownership/mode. The retained diagnostic replaces that raw
path with a fixed component identifier.

The falsifiable hypothesis is that Ubuntu 26.04's hosted image makes `/opt` world-writable, while
the lifecycle installer creates `/opt/kdive-live-worker-lifecycle` and hardens only that child.
Consequently every installed runtime file remains replaceable through `/opt`, independent of its
own ownership and mode. The regression must construct a mode-0777 installation parent, run the
installer's runtime-root producer, and require both the parent and new runtime root to be mode 0755
before the source fix. The source fix must normalize the selected installation parent to root:root
mode 0755 before creating the runtime root, without weakening fingerprint attestation.

Run [33013068295](https://github.com/randomparity/kdive/actions/runs/33013068295/job/98324100356)
then isolated `capture_bootstrap_manifest=false` while every other readiness component was true.
Run [33017429217](https://github.com/randomparity/kdive/actions/runs/33017429217/job/98339160715)
proved the destination-parent cause from numeric `0:0:0777` evidence and the exact reason
`fingerprint_ancestor_replaceable`. The real-install regression failed with the normalization call
removed because the corrected arm remained mode `0777`, then passed with no-follow root:root
mode-0755 normalization restored. The hosted diagnostic and attestation exceptions now retain only
the fixed `capture_manifest_parent`/`capture_manifest_fingerprint_ancestor` component identifiers,
allowlisted reason, and numeric uid/gid/mode; raw paths remain internal. Workflow-shape and
attestation regressions reject a path-bearing output.

**Steps**

1. Run the focused readiness-filter, manifest installer, workflow-shape, worker transaction,
   provider, and live-stack tests. Require the real rollback regression to leave the provision job
   queued and emit no journal claim, and require the diagnostic/attestation tests to expose only
   fixed component/reason/numeric fields. Expected: pass.
2. Run a fresh scope audit against the current ADR/spec/plan. Expected: approve with the issue's
   complete candidate surface.
3. Generate the immutable whole-branch package for the full merge-base-to-HEAD range and obtain the
   required Forge review with zero findings. Retain its non-empty regular mode-0600 report for PR
   publication. Any later commit invalidates that range and requires a new package and approval.
4. Run adversarial branch review, security review, and simplification. If any phase changes
   behavior or design, return to steps 1-3 before proceeding.
5. Run repository guardrails only when the campaign orchestrator sequences the shared database test
   environment: `prek run --all-files`, `just ci`. Expected: exit 0 each.
6. Push the final reviewed commit and dispatch the hosted workflow from that exact SHA. Require the
   run metadata to report Ubuntu 26.04, the committed ppc64le image identity, and `headSha` equal to
   `git rev-parse HEAD`. Require the exact queue target to agree with the immutable fixed-worker
   claim, and require provider records for that job/System to pair each mapped stage's
   start/completion in order through `define-start`; missing or inconsistent evidence rejects the
   proof. Require every readiness component true, transition to `ready`, the named SSH test pass,
   and a nonzero passed-proof summary.
7. Create/update the PR with `Closes #2056` only, wait for green CI, and require mergeability to be
   CLEAN/MERGEABLE with `headRefOid` equal to the hosted-proved SHA. Make no source/doc commit after
   the hosted proof; any change invalidates it and returns to step 1 before publication.
8. Publish one verified `WORK:REVIEW` annotation for that exact PR head after the applicable
   reviews and checks, then re-read the PR and require the same green, CLEAN/MERGEABLE head.
9. Set and confirm `status:awaiting-merge`, re-read the same exact green/CLEAN/MERGEABLE PR head,
   then post and verify a successful `WORK:TRAJECTORY` recording that awaiting-merge handoff. Stop
   without merging.

## Rollback

Reverting the branch changes no product data: there is no migration or persistent external product
state. Each hosted dispatch retains the workflow's existing always-run cleanup for libvirt, worker,
containers, and temporary runtime state; a cleanup failure is a blocker recorded on #2056 rather
than ignored. On an inconclusive diagnostic or abandonment, post `WORK:TRAJECTORY` before the
parked status label, leave any PR/branch named in that record, and do not claim they were unwound.
PR creation and issue labels/comments are workflow tracking state intentionally retained for resume.
