# ADR 0581: Diagnose hosted provision readiness at persisted boundaries

## Status

Accepted

## Context

Hosted Ubuntu 26.04 ppc64le TCG runs leave a System in `provisioning` for the full state deadline.
PR #2057 exposed the state timeline and worker journal. PR #2060 attributed the stall to a
`state-fenced` provision lane, but current source routes `JobKind.PROVISION` to `default`; only
restore, reprovision, and snapshot are state-fenced. Post-#2060 run
[32604993978](https://github.com/randomparity/kdive/actions/runs/32604993978/job/97108664284)
logged the replacement worker starting at `2026-08-22T23:51:50Z`, then no worker entry before the
`ppc64le:provision: t+0s provisioning` timeline exhausted at `2026-08-23T00:02:37Z`. Current run
[32968397867](https://github.com/randomparity/kdive/actions/runs/32968397867/job/98176108436)
repeated that shape: replacement worker start at `2026-08-26T12:36:42Z`, one
`t+0s provisioning` state, and timeout at `2026-08-26T12:47:31Z`. Neither journal carries a claim
or provider boundary, so the evidence cannot distinguish an unclaimed default-lane row from a
claimed handler/provider stall. Increasing the deadline would hide the same ambiguity.

Run [32981623561](https://github.com/randomparity/kdive/actions/runs/32981623561/job/98219425910)
made the boundary decisive. The exact provision row persisted on `default` at
`2026-08-26T14:51:00.739273Z` and remained `queued`, attempt 0, with no worker or heartbeat. The
fixed worker had logged `accepting dispatch lanes: default,state-fenced` at `14:50:18.567292Z`, but
logged no claim or provider stage. A fresh database seeds `queue_paused=false`; therefore
`Worker.run_once` was returning at its readiness gate before `dequeue`. That probe unconditionally
verifies `/usr/share/kdive/capture-bootstrap-manifest.json`, while the hosted lifecycle install
built the root-owned worker venv but never built/installed its matching manifest. The missing
attestation held the healthy-looking systemd worker not-ready and starved every queue lane.

## Decision

The hosted proof records the queue and execution boundaries before changing timing:

1. The hosted test exclusively creates a mode-0600 workflow-temporary target with
   `O_CREAT|O_EXCL` and writes the provision response's job id and System id. A partial record from
   interruption is malformed and therefore unusable evidence; no recovery protocol is required. A
   read-only queue snapshot validates that pair and reports the exact joined System state plus
   persisted lane, job state, attempt, worker id, enqueue time, **last** heartbeat time, and lease
   expiry. It retains `ready`/`succeeded` and cannot substitute another job/System pair. Five-second
   connect/statement timeouts sit inside a 12-second whole-step timeout; failures are a bounded
   fixed-code line and nonzero when the target/exact join is unavailable, empty, multiply matched,
   mismatched, or timed out.
2. A fixed worker logs its accepted lanes at startup and, only for `JobKind.PROVISION`, logs the
   persisted lane, queue delay, and immutable initial `claim_at`: the `heartbeat_at` returned by the
   dequeue database call before later renewals mutate the row.
3. Local-libvirt logs start and completion around each mapped synchronous provision call without
   logging paths, profile data, domain XML, guest output, or credentials.
4. One usable hosted run selects the source correction from the first boundary lacking its expected
   successor. One unchanged redispatch is allowed only when the diagnostic infrastructure itself
   was unavailable; a second inconclusive run parks without a source or deadline guess. The final
   hosted run occurs after review/simplification/guardrails, must have the same SHA as the PR head,
   report its provision lane and immutable claim timestamp, reach `ready`, and pass
   `tests/integration/test_live_stack.py::test_ppc64le_guest_is_ssh_reachable_over_the_wire`.
5. The 600-second state deadline stays unchanged unless at least two hosted runs record completed
   end-to-end intervals from immutable provision-job `claim_at` through System `ready`. A proposed
   deadline is the larger total plus 50 percent, capped at 900 seconds; stage pairs diagnose the
   total but never size it. A missing `ready` timestamp or a margin above the cap authorizes no
   increase, and the separate post-ready SSH budget is likewise not provision-timing evidence.
6. After installing the fixed-worker venv, the hosted workflow builds the capture-bootstrap
   manifest as root from that venv's root-owned site-packages, installs it root:root mode 0644 at
   the readiness verifier's default path, and verifies its runtime identity before any lifecycle
   worker starts.

The diagnostics remain after the fix so every future red hosted proof identifies its own boundary.

## Consequences

A successful and a failed hosted run both expose persisted lane and immutable claim timing. A
queued row and a running row no longer look alike. A claimed provider stall names the last entered
mapped call. The workflow gains one bounded local database read, and normal workers gain a few INFO
lines per provision; no MCP contract or database schema changes. The snapshot names an unavailable,
empty, multiply matched, mismatched, or timed-out exact-target read and exits nonzero; its workflow
wrapper warns and preserves the spine's pre-existing verdict rather than replacing it. Such a run
is not usable diagnosis evidence.

## Considered & rejected

- **Accept PR #2060's state-fenced premise and widen the worker lanes again.** verified: at commit
  `66541e9d7a59922b9fb180a40284bc3370f68a04`,
  `dispatch_lane_for_kind(JobKind.PROVISION)` returns `default` because `PROVISION` is absent from
  `STATE_FENCED_JOB_KINDS`; the premise does not describe the persisted row.
- **Increase the provision state deadline from the SSH budget.** verified: in the
  `live_vm_tcg (hosted)` job of run 32968397867, the pytest failure reports
  `deadline_s = 600.0` and only `ppc64le:provision: t+0s provisioning`; the 900-second SSH budget
  appears later in the test and starts only after `ready`. Those are different phases.
- **Capture only the worker journal.** verified: the `live_vm_tcg (hosted)` jobs linked in Context
  contain fixed-worker startup at `23:51:50Z` / `12:36:42Z` and no subsequent worker entry while
  pytest reports only `t+0s provisioning`; without the row's state and claim columns, both an idle
  worker and a busy silent handler fit the excerpts.
- **Do nothing and accept the timeout as sufficient evidence.** judgment: the same externally
  visible state represents two corrections in different components, so acting from it would guess
  rather than diagnose the requested source cause.
- **Retain only the queue snapshot.** judgment: it distinguishes queued from running but a running
  row still leaves handler dispatch and every synchronous provider stage indistinguishable.
- **Add temporary diagnostics and remove them after the confirming run.** judgment: removing the
  only boundary evidence recreates #2056's diagnostic gap on the next regression; the retained
  metadata and fixed-token INFO lines are bounded.
- **Expose queue internals through the public MCP job envelope.** judgment: that widens a public
  contract and unrelated consumers when an issue-local, read-only hosted diagnostic settles the
  question with less surface.
