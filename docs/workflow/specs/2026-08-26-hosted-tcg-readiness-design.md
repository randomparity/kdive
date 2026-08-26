# Hosted TCG provision readiness diagnosis and correction

## Scope and authority

This design implements issue #2056 under scope token `q2056-b6e4a669` on branch
`feat/hosted-tcg-readiness-2056`, based on `main`. The operator requires the Ubuntu 26.04 hosted
ppc64le TCG proof to reach `ready` and pass `test_ppc64le_guest_is_ssh_reachable_over_the_wire`.
The proof must expose the provision job's persisted lane and fixed-worker claim timing, and its
state/journal evidence must identify the first broken boundary. A deadline change is permitted only
when hosted measurements justify it; the separate post-ready 900-second SSH budget is not evidence
for provision timing.

The permitted implementation surface is:

- `.github/workflows/live.yml`;
- `deploy/systemd/install-live-worker-lifecycle.sh`;
- `docs/adr/0574-systemd-supervises-host-worker-incarnations.md`,
  `docs/adr/0581-hosted-provision-boundary-evidence.md`, and these issue-owned design artifacts;
- `docs/guide/reference/config.md`;
- `scripts/build-capture-bootstrap-manifest.py`;
- `scripts/live-stack/filter-worker-journal-evidence.py`,
  `scripts/live-stack/filter-worker-readiness-evidence.py`, and
  `scripts/live-stack/provision-queue-diagnostics.sh`;
- `src/kdive/config/external_env.py`, `src/kdive/jobs/capture_operations/bootstrap_attestation.py`,
  `src/kdive/jobs/capture_operations/bootstrap_elf.py`, `src/kdive/jobs/worker.py`, and
  `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`; and
- `tests/deploy/test_live_worker_provisioning.py`,
  `tests/integration/live_stack/spine.py`, `tests/integration/live_stack/test_spine.py`,
  `tests/integration/test_live_stack.py`, `tests/jobs/capture_operations/test_manifest.py`,
  `tests/scripts/test_live_stack_scripts.py`, and `tests/scripts/test_live_workflow_shape.py`.

Issues #2069, #2072, #2087, and #2089 and their files are excluded. There is no migration and no
merge authorization.

## Evidence and corrected premise

PR #2057 made distinct System states and stopped-worker journals visible. PR #2060 then claimed
that provision jobs route to `state-fenced`, but current source contradicts that premise:
`dispatch_lane_for_kind(JobKind.PROVISION)` returns `default`; only restore, reprovision, and
snapshot route to `state-fenced`. Hosted runs after #2060, including 32604993978 and 32968397867,
still show one `provisioning` state for the full 600-second state deadline and a live fixed worker
whose journal contains only process startup. That evidence cannot distinguish an unclaimed
`default`-lane row from a claimed handler/provider stall.

The first implementation slice therefore makes that distinction directly. The named hosted test
exclusively publishes its returned provision job id and System id in a workflow-owned target file
immediately after `systems.provision` succeeds. Before cleanup, a bounded read-only diagnostic
queries exactly that pair regardless of current System state and reports persisted lane, job state,
attempt, worker id, enqueue timestamp, **last** heartbeat timestamp, and lease expiry. The initial
claim timestamp is not that mutable row value: it is the `heartbeat_at` returned by `dequeue` in the
same database call that changes queued to running, copied immediately into an immutable worker
journal record before renewals. The report excludes payload, authorizing data, credentials, and
failure context. Local-libvirt logs start and completion for each synchronous provision stage.
Together the last emitted boundary is decisive:

- queued row with no worker and no matching claim record: worker claim/readiness boundary;
- running row without a matching claim record: dequeue-to-claim-record publication boundary; the
  run is unusable for claim-timing proof;
- running row with a matching claim record but no provider-stage entry: handler dispatch boundary;
- running row whose journal ends with an unmatched named stage start: that mapped provider call;
- terminal row or System error: existing failure context is authoritative instead of a timeout.

One **usable** diagnostic dispatch is required before any source correction. When the only failure
is diagnostic infrastructure (the bounded snapshot or journal is unavailable), the branch may be
redispatched once unchanged. A second unavailable, truncated, ambiguous, or internally
inconsistent result is fail-loud: make no source or deadline change and park the unattended quest
with the exact missing evidence. Evidence that identifies a boundary outside the permitted surface
is likewise a scope blocker, not authority to expand. After a usable run selects the first broken
boundary, correct it and run the hosted proof again. Diagnostic behavior stays in the final change
because a future red proof must remain self-explaining.

Run
[32981623561](https://github.com/randomparity/kdive/actions/runs/32981623561/job/98219425910)
provided the usable diagnostic. The exact provision job was persisted on `default` at
`2026-08-26T14:51:00.739273Z` and remained `queued`, attempt 0, with no worker, heartbeat, or
lease. The fixed worker advertised `default,state-fenced` at `14:50:18.567292Z`, then emitted no
claim or provider-stage record. That selects worker readiness before dequeue rather than lane
routing, handler dispatch, or local-libvirt.

Source tracing makes the localized cause concrete. `Worker.run_once` returns before `dequeue` when
its injected readiness probe is false. The process probe always calls
`verify_capture_bootstrap_manifest`, whose default path is
`/usr/share/kdive/capture-bootstrap-manifest.json`; the lifecycle installer creates the fixed
worker's root-owned venv but does not install its matching manifest. The hosted workflow therefore
builds the manifest as root from that venv's site-packages, installs it root:root mode 0644,
verifies it against the same interpreter and source root, and does so before any lifecycle worker
starts. The 600-second state deadline remains unchanged.

The first exact-head verification run
[32998642219](https://github.com/randomparity/kdive/actions/runs/32998642219/job/98274351467)
failed at manifest construction before worker startup. Its bounded error reported the
address-only loader entry `(0x...)`. glibc initializes the kernel vDSO with an empty name and uses
`DT_SONAME` only when the kernel image supplies it; this runner's vDSO therefore exercised a valid
virtual mapping the strict parser did not admit. The parser now accepts either the named
`linux-vdso*`/`linux-gate*` form or an address-only virtual mapping while retaining absolute,
existing-file validation for every file-backed dependency.

The next exact-head run
[33003146430](https://github.com/randomparity/kdive/actions/runs/33003146430/job/98289891730)
passed loader parsing but rejected a replaceable installed-runtime ancestor. Recursive descendant
hardening did not change the generic terminal artifact in run
[33004795604](https://github.com/randomparity/kdive/actions/runs/33004795604/job/98295552657).
The attestation error had suppressed the rejected path, so a fixed-form diagnostic added only that
path, uid/gid, and permission bits. Run
[33005759211](https://github.com/randomparity/kdive/actions/runs/33005759211/job/98298954459)
then identified `/opt`, uid/gid `0:0`, mode `0777`.

The evidence supports one falsifiable hypothesis: the hosted image's world-writable `/opt` remains
an attacker-replaceable ancestor even after the installer makes its
`/opt/kdive-live-worker-lifecycle` child root-owned and non-writable. A regression exercises the
installer's producer against a mode-0777 parent. The installer must make the selected parent and
runtime root root:root mode 0755 before populating the runtime; attestation remains fail-closed.

Exact-head Ubuntu 26.04 run
[33013068295](https://github.com/randomparity/kdive/actions/runs/33013068295/job/98324100356)
then identified the remaining pre-dequeue component. The exact job stayed queued on `default`,
attempt 0, while the bounded slot-1 response reported Postgres, MinIO, and capture recovery true
and `capture_bootstrap_manifest` false. The root-side manifest build, install, producer verify, and
leaf `0:0:0644` assertion had already passed.

The manifest producer's destination-parent contract was the concrete mismatch. `_atomic_write`
created the destination parent under the privileged process's ambient umask, and `_install`
validated only the leaf. Exact-head diagnostic run
[33017429217](https://github.com/randomparity/kdive/actions/runs/33017429217/job/98339160715)
reported `/usr/share/kdive`, uid/gid `0:0`, mode `0777`, and the allowlisted verifier reason
`fingerprint_ancestor_replaceable` under the fixed worker identity. The observation proves the
destination-parent/ambient-umask hypothesis: the root producer accepted a leaf whose world-writable
parent the unprivileged no-follow readiness consumer rejected. The regression invokes the same
`_install` entry twice under umask `000`, changing only whether `_prepare_install_parent` runs; the
legacy arm reproduces mode `0777` and the verifier rejection, while the corrected arm requires
root:root mode `0755` and verifier success.

## Components and data flow

1. The hosted test exclusively creates the target named by `KDIVE_PROVISION_EVIDENCE_TARGET` with
   `O_CREAT|O_EXCL`, mode 0600, and writes `<job UUID><TAB><System UUID><LF>`. Any existing target
   is an error. Interruption may leave a partial file, which the consumer rejects as malformed; the
   single-writer hosted proof needs no recovery or concurrent-writer protocol.
2. `scripts/live-stack/provision-queue-diagnostics.sh TARGET_FILE` validates that exact two-UUID
   record, opens the server-role database with short connection and transaction-local statement
   timeouts, and queries exactly the named provision job whose internally generated,
   `SystemPayload`-validated `payload.system_id` equals the named System. It does not filter on
   System state, so `ready`/`succeeded` remains visible. Zero or multiple matches, a job/System
   mismatch, malformed target data, or an unavailable query is nonzero. The one row uses a fixed
   literal-tab-separated header and explicit `NONE` values.
3. `.github/workflows/live.yml` gives the hosted TCG spine a workflow-temporary target path,
   invokes the script in an `if: always()` step, then captures the bounded fixed-worker journal on
   every TCG outcome before cleanup. Both captures use `::stop-commands::` shields and exit zero, so
   evidence never replaces the spine verdict. GNU
   `timeout --signal=TERM --kill-after=2s 12s` bounds the whole queue diagnostic, not only SQL; its
   output is either the fixed header plus one row or one sanitized fixed-code error line of at most
   100 bytes. The existing 55-minute/400-line journal bound retains immutable claim timing and
   provider stages on both green and red proofs.
4. `src/kdive/jobs/worker.py` emits one startup line naming worker id and accepted lanes, then one
   immutable line only for a successful `JobKind.PROVISION` claim, naming job id, persisted lane,
   attempt, enqueue time, initial dequeue `claim_at`, and non-negative queue delay.
5. `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` emits start and completion around
   guest-architecture resolution, rootfs materialization, baseline preparation, overlay preparation,
   the `render-domain` interval covering gdb/SSH port reuse and `render_domain_xml`, the whole
   overlay-customizer loop, console preparation, and domain definition/start. An exception
   deliberately leaves the stage start unmatched. No profile, XML, path, credential, or guest output
   is logged.
6. `src/kdive/jobs/capture_operations/bootstrap_elf.py` treats a syntax-valid address-only loader
   entry as an unnamed kernel vDSO. The entry contributes no file to the attested closure; malformed
   addresses, unresolved dependencies, non-absolute file mappings, and other off-grammar output
   still fail closed.
7. `src/kdive/jobs/capture_operations/bootstrap_attestation.py` names only the rejected ancestor
   path, uid/gid, and permission bits when ownership or replaceability checks fail.
8. `deploy/systemd/install-live-worker-lifecycle.sh` normalizes the selected runtime installation
   parent and runtime root to root:root mode 0755, then removes group/world write bits recursively
   after populating the runtime. Every ancestor is therefore non-replaceable regardless of the
   hosted image's `/opt` mode or the invoking umask.
9. `scripts/build-capture-bootstrap-manifest.py` normalizes the installed manifest's destination
   parent through an `O_DIRECTORY|O_NOFOLLOW` descriptor to root:root mode 0755 before its atomic
   leaf write. The hosted workflow emits the fixed parent path/uid/gid/mode and an allowlisted
   verifier reason while running `verify_capture_bootstrap_manifest` under `kdive-worker-1`, so an
   accepted result proves the same identity and consumer as readiness.
10. `scripts/live-stack/filter-worker-readiness-evidence.py` accepts at most 4096 bytes and exactly
   the expected `/readyz` shape, then emits only `ready` plus the Postgres, MinIO,
   capture-manifest, and capture-recovery booleans. The workflow bounds its loopback request at
   eight seconds and captures it before cleanup on every outcome.
11. A usable diagnostic dispatch selects the correction. The final hosted dispatch must report the
   same proof's provision row with persisted lane and claim timestamp, show the System transition
   to `ready`, and pass
   `tests/integration/test_live_stack.py::test_ppc64le_guest_is_ssh_reachable_over_the_wire`.
   The existing zero-proof guard remains unchanged.

## Failure handling and bounds

The queue snapshot has a five-second connect timeout, a five-second SQL statement timeout, and an
exact one-row result. The workflow's 12-second outer timeout bounds target parsing, env sourcing,
Python import, connection, query, and teardown. An unavailable, empty, multiply matched, mismatched,
or timed-out snapshot returns nonzero and emits only one fixed-code error line; the workflow emits a
fixed warning while preserving the original spine verdict. Such a run is not usable evidence.
Worker/provider records are fixed-field INFO lines, not poll-loop output. The direct retained-journal
capture keeps its existing time and line bounds, parses JSON messages, and emits only exact
full-match lane, provision-claim, and provider-stage records; every other journal line is discarded.
The lifecycle diagnostic response retains ADR-0574's bounded secret redaction. Those journal records
contain no raw payloads, authorizing records, DSNs, environment values, dynamic exception text,
paths, domain XML, console text, or secrets.
The manifest diagnostic emits only its fixed `/usr/share/kdive` parent, numeric uid/gid/mode, and an
allowlisted reason code; it never prints the caught exception. The readiness request is loopback-only,
bounded to eight seconds and 4096 response bytes, and passes through an exact-shape filter. It emits
no version object, dynamic exception, environment, or credential.

The state deadline remains 600 seconds during diagnosis. It is not increased speculatively. A
deadline change requires at least two completed hosted **end-to-end provision intervals**, each
measured from the immutable worker journal's initial dequeue `claim_at` through the System's
`ready` timestamp. The exact target row must match the claim record's job, lane, worker, and attempt;
its later heartbeat can corroborate liveness but is never relabeled as the initial claim. Per-stage
start/completion pairs diagnose where the end-to-end total is spent but never size the enclosing
deadline. The proposed deadline is the larger completed end-to-end
interval plus 50 percent, capped at 900 seconds, and the ADR/spec must record both runs and
arithmetic before code changes. If either interval lacks `ready`, or the margin would exceed the
cap, the measurements authorize no increase; diagnose/optimize the source or park instead.
Otherwise the source defect is corrected without a deadline change.

## Security model

### Boundary inventory

The workflow crosses from a hosted CI job into a local Postgres instance using an existing
server-role DSN. The design adds one read-only diagnostic query and adds log output derived from
queue metadata. It does not widen GitHub token permissions, network exposure, or worker database
permissions.

Manifest construction crosses from the trusted fixed-worker interpreter and its `PT_INTERP` into
runtime-loader output, then crosses a root privilege boundary when the resulting attestation is
installed at the worker readiness verifier's default path.

### Actors and trust

The local CI job and repository checkout are trusted operators for this ephemeral proof.
`payload.system_id` is generated by the server's typed `SystemPayload` enqueue path, not copied from
the caller's provisioning profile; the diagnostic still fails closed if it cannot join that id to
exactly one System. Caller-controlled profile fields and guest output remain potentially untrusted
and are not emitted. GitHub log readers are authorized repository collaborators but are not
entitled to runtime credentials.
The build-time interpreter, its selected loader, and the checked-out source are trusted inputs for
this repository-owned proof. Loader-produced text is accepted only as structural evidence; an
address-only entry represents no file, while every file-backed mapping remains subject to
filesystem validation. Runtime verification does not trust a replaced loader or dependency merely
because it appeared in build-time output.

### Controls

The SQL is a literal statement with no interpolated input, runs in a read-only transaction, has
connect/statement and outer wall-clock bounds, and selects only ids, enums, counters, and
timestamps. Target creation is exclusive and mode 0600. The workflow retains the stop-commands
shield so output cannot inject workflow commands. Worker/provider log templates contain only
bounded identifiers, fixed stage/event names, and timestamps. Existing secret redaction remains
the final logging control.
The loader runs with a scrubbed environment, a ten-second deadline, and a one-MiB-per-stream cap.
Its full-line grammar admits only named or address-only Linux virtual mappings and absolute
file-backed mappings; file paths must resolve to existing regular files. The installed manifest is
root-owned mode 0644. Runtime verification fingerprints its declared interpreter, loader, and
dependencies before recomputing the closure, so a replaced loader cannot hide a file-backed
dependency through the address-only form.

### Out of scope

This change does not protect repository collaborators from metadata already visible in workflow
logs, redesign queue observability APIs, or expose provision internals through MCP. Those are not
needed to diagnose this hosted proof.

## Verification

Focused tests prove target creation is mode 0600 and exclusive, rejects an existing target, records
the exact response pair, and leaves a partial interruption to the consumer's malformed-input check.
Script tests reject malformed, mismatched, zero, and multiple targets; assert literal bounded
read-only SQL; and enforce one sanitized fixed-code error line. Workflow-shape tests bind the same
target path, outer 12-second timeout, stop-commands shield, and pre-cleanup order. Worker tests
assert the provision claim fields, immutable initial dequeue timestamp, non-negative delay despite
later renewal, and no new claim record for an unrelated job kind. Provider tests assert paired
records around each exact call, missing completion on a raised call, order, and redaction. Loader
parser tests prove named and unnamed virtual mappings contribute no file while malformed output
still fails closed.

The final behavior proof runs only after review/simplification and guardrails: a hosted Ubuntu 26.04
`live_vm_tcg (hosted)` job whose `headSha` equals final PR `headRefOid`, whose committed ppc64le
image identity is reported, and whose exact queue target agrees with the immutable claim record.
The retained provider records for that job and System must be present and pair each mapped stage's
start/completion in order through `define-start`; missing, inconsistent, or off-target records
reject the proof. The System must reach `ready`, and
`test_ppc64le_guest_is_ssh_reachable_over_the_wire` must pass with a nonzero passed-proof summary.
Any later commit invalidates the hosted proof.

The repository guardrails are `just lint`, `just type`, `just test`, `prek run`, and `just ci`.
Host architecture is x86_64; declared targets are x86_64 and ppc64le; the host is included.

See [ADR 0581](../../adr/0581-hosted-provision-boundary-evidence.md).
