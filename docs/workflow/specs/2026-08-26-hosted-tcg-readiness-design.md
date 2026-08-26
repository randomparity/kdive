# Hosted TCG provision readiness diagnosis and correction

## Scope and authority

This design implements issue #2056 under scope token `q2056-a9c3e741` on branch
`feat/hosted-tcg-readiness-2056`, based on `main`. The operator requires the Ubuntu 26.04 hosted
ppc64le TCG proof to reach `ready` and pass `test_ppc64le_guest_is_ssh_reachable_over_the_wire`.
The proof must expose the provision job's persisted lane and fixed-worker claim timing, and its
state/journal evidence must identify the first broken boundary. A deadline change is permitted only
when hosted measurements justify it; the separate post-ready 900-second SSH budget is not evidence
for provision timing.

The permitted implementation surface is `.github/workflows/live.yml`, `scripts/live-stack/`,
`src/kdive/jobs/`, `src/kdive/providers/local_libvirt/`, the named live-stack integration and
script tests, ADR 0581, and these issue-owned design artifacts. Issues #2069, #2072, #2087, and
#2089 and their files are excluded. There is no migration and no merge authorization.

## Evidence and corrected premise

PR #2057 made distinct System states and stopped-worker journals visible. PR #2060 then claimed
that provision jobs route to `state-fenced`, but current source contradicts that premise:
`dispatch_lane_for_kind(JobKind.PROVISION)` returns `default`; only restore, reprovision, and
snapshot route to `state-fenced`. Hosted runs after #2060, including 32604993978 and 32968397867,
still show one `provisioning` state for the full 600-second state deadline and a live fixed worker
whose journal contains only process startup. That evidence cannot distinguish an unclaimed
`default`-lane row from a claimed handler/provider stall.

The first implementation slice therefore makes that distinction directly. On every hosted TCG
run, before cleanup, a bounded read-only diagnostic reports each provisioning System's provision
job id, persisted `dispatch_lane`, job state, attempt, worker id, enqueue timestamp, claim timestamp,
and lease expiry. The report excludes payload, authorizing data, credentials, and failure context.
The fixed worker also logs the accepted lane set at startup and logs each successful claim with the
persisted lane and database-derived queue delay. Local-libvirt logs the start of each synchronous
provision stage before entering it. Together the last emitted boundary is decisive:

- queued row with no worker and no claim timestamp: worker claim/readiness boundary;
- running row with worker and claim timestamp, but no provider-stage entry: handler dispatch boundary;
- running row whose journal stops after a named provider stage: that provider stage;
- terminal row or System error: existing failure context is authoritative instead of a timeout.

The diagnostic commit is dispatched once on the hosted workflow. The source correction is then
made at the first broken boundary, followed by another hosted dispatch. Diagnostic behavior stays
in the final change because a future red proof must remain self-explaining.

## Components and data flow

1. `scripts/live-stack/provision-queue-diagnostics.sh` opens the server-role database with a short
   connect timeout and transaction-local statement timeout. One fixed query joins provisioning
   Systems to provision jobs by `payload->>'system_id'`, orders deterministically, and caps rows.
   It emits tab-separated fields with a header and explicit `NONE` values.
2. `.github/workflows/live.yml` invokes that script under the workflow's existing
   `::stop-commands::` shield in both lifecycle-diagnostics steps. In the hosted TCG job, a small
   always-run provision-boundary step executes before failure-only journal capture, so successful
   proof evidence includes the persisted lane and claim time too. Diagnostic failure is named and
   cannot replace the spine verdict.
3. `src/kdive/jobs/worker.py` emits one startup line naming worker id and accepted lanes, then one
   line per successful claim naming job id, kind, persisted lane, attempt, enqueue time, claim time,
   and non-negative queue delay.
4. `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` emits a bounded set of stage-entry
   lines for arch resolution, rootfs materialization, baseline extraction, overlay preparation,
   overlay customization, console preparation, and domain define/start. No profile, XML, path,
   credential, or guest output is logged.
5. The hosted dispatch selects the correction. The final hosted dispatch must show the provision
   row claimed, the System transition to `ready`, and the named SSH proof passing. The existing
   zero-proof guard remains unchanged.

## Failure handling and bounds

The queue snapshot has a five-second connect timeout, a five-second SQL statement timeout, a
20-row ceiling, and no retries. An unavailable snapshot prints one warning and returns nonzero;
the workflow wrapper records that warning but preserves the original test verdict. Worker and
provider diagnostics are INFO lines with one line per lifecycle boundary, not poll-loop output.
Journal capture keeps its existing time and line bounds. No diagnostic prints raw payloads,
authorizing records, DSNs, environment values, domain XML, console text, or secrets.

The state deadline remains 600 seconds during diagnosis. It is not increased speculatively. If the
hosted row is claimed and measured provider work is still progressing at 600 seconds, a revised
ADR/spec must name the measured stage duration and choose a bounded deadline from that hosted
measurement before code changes. Otherwise the diagnosed source defect is corrected without a
deadline change.

## Security model

### Boundary inventory

The workflow crosses from a hosted CI job into a local Postgres instance using an existing
server-role DSN. The design adds one read-only diagnostic query and adds log output derived from
queue metadata. It does not widen GitHub token permissions, network exposure, or worker database
permissions.

### Actors and trust

The local CI job and repository checkout are trusted operators for this ephemeral proof. Job
payloads and guest output are treated as potentially untrusted and are not emitted. GitHub log
readers are authorized repository collaborators but are not entitled to runtime credentials.

### Controls

The SQL is a literal statement with no interpolated input, runs in a read-only transaction, has
connect/statement timeouts and a row cap, and selects only ids, enums, counters, and timestamps.
The workflow retains the stop-commands shield so log text cannot inject workflow commands. Worker
and provider log templates contain only bounded identifiers, enum-like stage names, and timestamps.
Existing secret redaction remains the final logging control.

### Out of scope

This change does not protect repository collaborators from metadata already visible in workflow
logs, redesign queue observability APIs, or expose provision internals through MCP. Those are not
needed to diagnose this hosted proof.

## Verification

Focused tests prove the diagnostic script uses the server DSN, a literal bounded read-only query,
explicit fields, and no secret-bearing columns. Workflow-shape tests prove the queue snapshot is
inside the stop-commands shield, runs before cleanup, is observational, and the hosted snapshot runs
on both success and failure. Existing worker and local-libvirt tests remain green around claim and
provision behavior. The final behavior proof is a hosted Ubuntu 26.04 `live_vm_tcg (hosted)` job in
which `test_ppc64le_guest_is_ssh_reachable_over_the_wire` passes and the zero-proof gate observes at
least one passed proof.

The repository guardrails are `just lint`, `just type`, `just test`, `prek run`, and `just ci`.
Host architecture is x86_64; declared targets are x86_64 and ppc64le; the host is included.

See [ADR 0581](../../adr/0581-hosted-provision-boundary-evidence.md).
