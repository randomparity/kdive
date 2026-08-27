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

Exact-head hosted run
[32998642219](https://github.com/randomparity/kdive/actions/runs/32998642219/job/98274351467)
then failed before worker startup while building that manifest. Ubuntu 26.04's runtime loader
reported its unnamed kernel vDSO as the valid address-only virtual mapping `(0x...)`.
`parse_loader_list` admitted only the named `linux-vdso*` and `linux-gate*` forms, so it rejected
the same non-file mapping when the vDSO omitted `DT_SONAME`. The manifest builder therefore failed
closed on a valid loader trace rather than reaching the worker proof.

Run [33003146430](https://github.com/randomparity/kdive/actions/runs/33003146430/job/98289891730)
accepted the loader trace, then failed the same build because an installed-runtime ancestor was
replaceable. Removing group/world write bits recursively from the installed runtime did not change
the generic artifact in run
[33004795604](https://github.com/randomparity/kdive/actions/runs/33004795604/job/98295552657).
The rejection had suppressed the path, so a fixed-form diagnostic exposed only the failing path,
uid/gid, and mode. Run
[33005759211](https://github.com/randomparity/kdive/actions/runs/33005759211/job/98298954459)
identified `/opt`, owned by uid/gid `0:0` with mode `0777`. The installer selected a child of that
world-writable directory but hardened only the child, leaving every installed runtime file
replaceable through its parent.

Exact-head Ubuntu 26.04 run
[33013068295](https://github.com/randomparity/kdive/actions/runs/33013068295/job/98324100356)
then distinguished the four worker readiness checks. Postgres, MinIO, and capture recovery were
true, while `capture_bootstrap_manifest` alone was false. The exact provision row remained queued
on `default`, attempt 0, with no worker or lease. The manifest's root-side build, installation,
producer verification, and leaf `0:0:0644` check had all passed before worker startup.

The producer and readiness consumer did not verify the same filesystem boundary. Manifest install
created the destination parent with `Path.mkdir` under the privileged process's ambient umask and
validated only the leaf file. Exact-head diagnostic run
[33017429217](https://github.com/randomparity/kdive/actions/runs/33017429217/job/98339160715)
named `/usr/share/kdive`, uid/gid `0:0`, mode `0777`, and reported the fixed verifier reason
`fingerprint_ancestor_replaceable` under the slot-1 worker identity. This proves the
destination-parent/ambient-umask hypothesis: the root producer accepted the leaf while the
unprivileged readiness consumer rejected its world-writable ancestor before dequeue. The
regression drives the same `_install` entry under umask `000` with only parent normalization
disabled, reproduces mode `0777` plus the readiness rejection, then requires the corrected path to
produce root:root mode `0755` and pass the same runtime verifier.

## Decision

The hosted proof records the queue and execution boundaries before changing timing:

1. The hosted test exclusively creates a mode-0600 workflow-temporary target with
   `O_CREAT|O_EXCL` and writes the provision response's job id and System id. A partial record from
   interruption is malformed and therefore unusable evidence; no recovery protocol is required. A
   read-only queue snapshot validates that pair and reports the exact joined System state plus
   persisted lane, job state, attempt, worker id, enqueue time, **last** heartbeat time, and lease
   expiry. It retains `ready`/`succeeded` and cannot substitute another job/System pair. Every
   result value passes a finite type/grammar/length allowlist before a complete byte-bounded TSV is
   written. Five-second connect/statement timeouts sit inside a 12-second whole-step timeout;
   failures are a bounded fixed-code line and nonzero when the target/exact join or result shape is
   unavailable, empty, multiply matched, mismatched, malformed, or timed out.
2. A fixed worker logs its accepted lanes at startup. Only for `JobKind.PROVISION`, it copies the
   persisted lane, queue delay, and immutable initial `claim_at` immediately after `dequeue`, then
   publishes the claim record only after the pooled connection context exits successfully and
   commits. A later renewal cannot alter those copied values; a dequeue rollback, including one
   caused by the subsequent queue-depth telemetry query, emits no claim record.
3. Local-libvirt logs start and completion around each mapped synchronous provision call,
   including the pre-materialization `_snapshot_pre_existing` boundary, without logging paths,
   profile data, domain XML, guest output, or credentials.
4. One usable hosted run selects the source correction from the first boundary lacking its expected
   successor. One unchanged redispatch is allowed only when the diagnostic infrastructure itself
   was unavailable; a second inconclusive run parks without a source or deadline guess. The TCG
   lifecycle journal capture runs on every outcome before cleanup. Its filter enforces 4096 bytes
   per record, 400 records, 256 KiB total input, bounded field grammars, and bounded atomic output,
   so the final hosted run can report the immutable claim record even when the spine succeeds. That
   run occurs after review/simplification/guardrails and must have the same SHA as the PR head. Its
   exact queue target and immutable claim record must agree, and its retained provider records must
   name that job and System and pair each mapped stage's start/completion in order through
   `define-start`.
   Missing, inconsistent, or off-target provider evidence rejects the proof even if the spine is
   green. The System must reach `ready` and pass
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
7. The strict runtime-loader parser admits both named Linux vDSO mappings and glibc's address-only
   form for an unnamed kernel vDSO. The address remains syntax-checked; file-backed mappings still
   require an absolute, existing regular-file path, and every other unparseable line still fails.
8. Fingerprint ancestor failures keep the raw rejected path internal and expose only a fixed
   component identifier, an allowlisted reason, and numeric uid/gid/permission bits. The lifecycle
   installer normalizes its selected runtime installation parent and runtime root to root:root mode
   0755 before populating the runtime, then removes group/world write bits recursively. A mode-0777
   `/opt` from the hosted image can no longer invalidate the otherwise root-owned runtime.
9. Manifest installation normalizes its destination parent through an
   `O_DIRECTORY|O_NOFOLLOW` descriptor to root:root mode 0755 before the atomic leaf write. The
   hosted workflow reports only the fixed `capture_manifest_parent` component identifier, numeric
   uid/gid/mode, and an allowlisted verifier reason, then invokes
   `verify_capture_bootstrap_manifest` under the fixed slot-1 worker identity. Producer success
   therefore proves the same internal path, identity, and verifier that gates dequeue without
   disclosing that path. A bounded loopback `/readyz` capture emits only the four component
   booleans and stays before cleanup on every hosted outcome.

The diagnostics remain after the fix so a future red hosted proof with usable captures identifies
its own boundary.

## Consequences

Ordinary successful and failed hosted runs expose persisted lane and immutable claim timing only
after the dequeue commit is durable. A queued row without a matching claim record remains ordinary
pre-claim evidence; a running row without one localizes the post-commit journal-publication boundary
and is unusable for claim-timing proof. A telemetry failure that rolls back the dequeue cannot leave
a false claim record. A claimed provider stall names the last entered mapped call. The workflow gains
one bounded local database read, and normal workers gain a few INFO lines per provision; no MCP
contract or database schema changes. The snapshot names an unavailable, empty, multiply matched,
mismatched, or timed-out exact-target read and exits nonzero; its workflow wrapper warns and
preserves the spine's pre-existing verdict rather than replacing it. Such a run is not usable
diagnosis evidence.

The manifest build now treats glibc's address-only unnamed-vDSO entry as virtual rather than as a
missing file dependency. Its no-path shape cannot add an unattested file to the closure; malformed
addresses and all other off-grammar loader output remain errors.

Manifest installation no longer inherits its destination-parent authority from an ambient umask,
and a root-only producer check can no longer certify a manifest the fixed worker rejects. The
retained diagnostics expose only a fixed manifest-parent component identifier, numeric
ownership/mode, an allowlisted verifier reason, and readiness component booleans; they omit raw
paths, exception text, build identity, environment, and credentials.

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
