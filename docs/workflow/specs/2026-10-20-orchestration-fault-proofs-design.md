# Orchestration fault proofs on x86_64 libvirt

## Authority and scope

Issue #3117 under epic #2803 owns six cells that `scripts/coverage_campaign/contract.py` emits as
`failure-resource/<provider>/x86_64/<failure>`, with provider `local-libvirt` or
`remote-libvirt` and failure `worker-interruption`, `lost-response` or `backing-service`. Each
cell's assertions are `(<failure>, no-duplicates, protected-state, cleanup)`, its roles
`server, worker, reconciler`, and its inputs the six kernel input identities. Frozen charter:
WORK:SCOPE `q3117-029cd8e8`. Exclusions approved for this campaign (2026-10-16): low-resource
cells #3118, native POWER #2818, durable finalization and cross-replica guarantees #2681,
inspected-image registration #3218 (merged). This change adds test carriers, coverage bindings
and runbook text only. It changes no product source. A product defect a cell exposes is
recorded as that cell's failure and routed to the campaign root. It is never worked around
in the carrier.

Operator decisions D1–D4 (end of this spec) are pending at the design gate. The body below is
written for the recommended option of each. A different answer changes only the section it
names.

## Source findings

- **Execution path.** `systems.provision` takes the authority path only when the resource
  carries an authority route (`src/kdive/services/systems/admission.py:444-447`). The demo-up
  lane (`deploy/ansible/playbooks/local-libvirt-host.yml`) installs no authority, so the managed
  host stack runs the direct `provision_handler` (`src/kdive/jobs/handlers/systems.py`). The
  authority lane is epic #3152, which is blocked. Its orphan and conflict construction (#3185)
  is in flight. The cells' declared roles do not include `authority`.
- **Job recovery.** A claim charges one attempt and sets a 5-minute lease
  (`src/kdive/jobs/queue.py:51-52`). The worker renews the lease every 30 s. Another worker
  reclaims a `running` row whose lease has lapsed while attempts remain. The reclaimed attempt
  re-runs the handler from the start. A late `complete` from the old attempt is fenced and
  logged as `job <id> completed but was reclaimed; result dropped`
  (`src/kdive/jobs/worker.py:627`). `KDIVE_WORKER_DEATH_VERIFIER` only decides whether the two
  build-use recovery tools are registered (`src/kdive/mcp/assembly/tool_registration.py:262`).
  It plays no part in job reclaim. These cells therefore do not need that lane, even though the
  issue text assumes they do.
- **Provision re-execution.** Local provisioning redefines the domain in place, treats an
  already running domain as success, and creates the overlay only when it is absent
  (`src/kdive/providers/local_libvirt/lifecycle/provisioning.py:296-301`). Remote provisioning
  fixes the domain name and UUID and reuses the overlay
  (`src/kdive/providers/remote_libvirt/lifecycle/provisioning.py:200-293`). That is the
  documented recovery outcome for a reclaimed provision. No advisory lock is held across the
  provider call (`systems.py:296-340`), so a paused holder does not block the reclaim.
- **Fenced side effects.** A stale attempt whose provider call raises after `started` tears
  down the domain (`provisioning.py:383-386`), and nothing fences that teardown. The
  worker-interruption cell resumes the stale worker. It then observes the domain and System
  after the stale attempt finishes. If the stale attempt tears down the domain, the cell fails
  `protected-state`.
- **Idempotency.** A `systems.provision` key is stored per principal and tool, inside the
  admission transaction. The stored admission envelope is replayed unchanged. A failed
  admission stores nothing. The arguments are not compared (`provision.py:101-146`,
  `docs/guide/async-jobs.md:70-96`). No repository evidence says whether a client disconnect
  cancels the handler. The mcp SDK dispatches tool calls in the session task group, not the
  HTTP request task.
- **Backing services.** On a database outage the worker's claim loop and process survive. A
  failed heartbeat ends that job's renewal. The row stays `running` until the lease lapses and
  is then reclaimed (`worker.py:69-73, 431-443, 695-721`). A not-ready worker pauses dequeue
  rather than failing jobs (ADR-0090 §5). An object-store failure maps to the retryable
  `infrastructure_failure` with no backoff (`src/kdive/store/objectstore.py:68-82`). The worker
  readiness check includes the store, so new claims pause during an outage.
- **Worker hold.** `tests/live_vm/installed_local_authority_support.py` already pauses the
  exact job holder with SIGSTOP through a pidfd. It checks the slot's retained `state.json`,
  `MainPID`, `InvocationID` and `boot_id` first (`set_exact_worker_hold`), then observes the
  reclaim (`running_job_claim`, `wait_for_reclaimed_job`). SIGSTOP leaves systemd and the
  lifecycle witness untouched. An out-of-band `systemctl restart` or kill wedges the slot
  (`docs/operating/runbooks/live-stack.md:362-370`), so the carrier never uses one.

## Design

### Frame

The carrier reuses the merged #3118 frame in `tests/integration/live_stack/resource_limits.py`:
`Held` (an owned Allocation and its System), `Sampler` (allocation status, `in_use` and owned
domains), `Provider`/`LOCAL`/`remote_provider` (the worker's libvirt or the provider host's
libvirt over SSH), `on_allocation`, `registered`, `remote_guest`, `fixture`, `prove_held` and
`release`. It also reuses `run_cell` from `scenario.py`, which reads the deployed identity,
refuses a non-candidate stack and writes one evidence record. `cells`, `bindings` and
`remote_bindings` gain one keyword, `owner`, that defaults to 3118. Existing callers keep
their behavior. Every cell runs the default shape on the catalog image (`rocky-kdive-ready-10`
locally, the enterprise representative remotely). Each cell binds the longterm fixture's
kernel inputs as its setup identity, the same way #3118 bound them for its non-booting
insufficient-disk cell. On a remote cell, `observe_host` records the provider host.

The in-flight window for all three faults is the provision's first-boot wait. That window
opens when the owned domain is active and the job is `running` at attempt 1. It closes when
the job leaves `running`. `in_flight` polls libvirt and the job row every 0.5 s and returns the
running claim (worker id, attempt, lease deadline). Locally the window is the readiness-marker
wait. Remotely it is the guest-agent wait. If the job leaves `running` before the window is
seen, the cell fails with `fault point not reached (incomplete)`. It is never a pass.

Preconditions are checked before any mutation. Each unmet one blocks the cell:

- at least two running fixed worker slots (`KDIVE_WORKER_COUNT=2`);
- the longterm fixture and the staged image;
- for the remote cells, `REMOTE_PROVIDER_SSH`;
- passwordless `sudo -n` for the fixed hold argv;
- for backing-service, the compose `postgres` container that serves `KDIVE_DATABASE_URL`.

### worker-interruption

1. Provision with the default shape. Wait for `in_flight`.
2. Pause the holder with `set_exact_worker_hold(claim, "stop")`.
3. `wait_for_reclaimed_job` must observe a different worker claim a higher attempt after the
   lease lapses. The deadline is the lease remainder plus 120 s, and at most 8 minutes. The
   Sampler runs every 15 s while waiting.
4. Wait until the job is terminal. The cell requires `succeeded` at the replacement's attempt
   by the replacement worker, and the System `ready`.
5. Resume with `"continue"`. Wait at most 15 minutes, which covers one first-boot window, for
   the stale invocation's journal to show one of two lines for this job:
   - `completed but was reclaimed; result dropped`, or
   - a provision failure line.
   Record which line appeared. Then require all of the following:
   - the job row is unchanged (`succeeded`, same attempt, same worker);
   - the System is still `ready`;
   - the owned domain is still active with the same UUID.
6. Prove `worker-interruption` with the original and replacement claims, both invocation ids,
   the stale outcome and the final job row. Prove `no-duplicates`: one System under the
   Allocation, one added domain named `kdive-<system_id>`, and one overlay equal to the one
   observed at the in-flight point. Prove `protected-state` from the samples.
7. Release through `Held.release` (`release_and_verify`). That proves `cleanup`: the System,
   domain and overlay are gone and capacity is back to its value before the cell.

A `finally` always resumes a still-paused holder before the cleanup attempt.

### lost-response

1. Generate a fresh idempotency key `q3117-<uuid4>`.
2. Send `systems.provision` with that key from a raw streamable-HTTP JSON-RPC client. The
   client runs `initialize`, then `notifications/initialized`, then a streamed `tools/call`
   POST.
3. Wait for an evidence-only database read to show the `idempotency_keys` row for
   (key, `systems.provision`). The bound is 60 s. Then close the connection without reading
   the response body and send no MCP cancel notification. This is the lost response: the
   server committed and the client never received the answer.
4. Retry from a new `LiveStackClient` session with the same key, while the job is still
   `running`. Retry again after the job is terminal.
5. Require all of the following:
   - each replay's `object_id` (the job id) and `data.system_id` equal the stored row's
     result;
   - exactly one `provision` job references the Allocation;
   - exactly one System exists under the Allocation;
   - the job is `succeeded` and the System `ready`.
6. If the row never appears, the cell fails, recording whether the handler was cancelled
   before commit (no System, no row) or an admission error was returned.
7. Prove `lost-response` with the stored result, both replays and the job and System counts.
   Prove `no-duplicates`, `protected-state` and `cleanup` as in worker-interruption.

### backing-service

1. Before the cell, record the compose `postgres` container id and the service roles' readyz
   and deployed identity (`run_identity`).
2. Provision. Wait for `in_flight`.
3. Stop Postgres with fixed argv: `docker compose --project-directory <stack checkout> stop
   postgres`.
4. Hold the outage for 75 s, which is more than two heartbeat intervals. During the outage the
   carrier makes no MCP call. Libvirt observation of the owned domain continues.
5. Start Postgres with `docker compose ... start postgres`. Require the same container id.
6. Wait at most 120 s for every role's readyz to return ready.
7. Wait for the job to be terminal. The documented resume path requires `succeeded`, the System
   `ready` and one active domain. The success may come from a reclaimed attempt after the lease
   lapsed, or from an attempt whose delayed writes succeeded. Record which happened: the
   attempt counter and the worker journal's heartbeat and failure lines for the job.
8. Require all three roles to keep their pre-fault deployed identity, so no role restarted.
9. Prove `backing-service` with the outage interval, container id, readiness timeline, attempt
   evidence and terminal job. Prove `no-duplicates`, `protected-state` and `cleanup` as above.
   The protected-state samples bracket the outage, because Postgres holds the allocation and
   capacity state and nothing can change it while it is stopped.

A `finally` always starts a stopped Postgres before any cleanup call.

### Coverage and runbook

- `scripts/coverage_campaign/obligations.toml` maps the three local cells to
  `tests/integration/test_orchestration_faults_live.py::test_local_orchestration_fault` and the
  three remote cells to `::test_remote_orchestration_fault`.
- The carrier's `main` writes input bindings with `owner=3117`.
- `docs/operating/runbooks/live-testing.md` gains a run section, and `remote-live-stack.md`
  gains the remote arm. Both name the preconditions above, the commands and the cleanup
  checks.

## Failure model

1. **Actors and deployments.**
   - The operator runs the live carriers on one exclusive x86_64 lab host. That host runs the
     managed host stack (`stack-services.sh`/demo-up, `KDIVE_WORKER_COUNT=2`) with the
     candidate deployed.
   - The remote cells add one separate x86_64 remote-libvirt provider host reached over the
     operator's SSH.
   - CI runs only the unit tests and the contract test.
2. **Invariants and assets at stake.**
   - The fault must be real and must hit the in-flight window.
   - A cell counts as passed only with every assertion proven on the candidate.
   - Each cell holds one owned System, domain and overlay. The Allocation stays granted and
     capacity stays charged until release.
   - The stack must stay usable: no wedged worker slot (SIGSTOP and SIGCONT only), Postgres
     restarted with its data volume, and every role ready afterwards.
   - The cell touches no domain, volume or container it does not own, except the shared
     `postgres` service, which needs the exclusive host grant.
3. **Accepted failure classes.**
   - A first boot too fast for the 0.5 s window poll. The cell records an incomplete failure
     and does not pass, and the cost is one rerun.
   - Lost-response proves only the committed-then-lost case. Abort-before-commit is
     nondeterministic, and the admission transaction makes the System and the key atomic.
   - Samples are discrete, with no instantaneous guarantee between them. The same bound is
     accepted in #3118.
   - The kernel inputs are bound as setup identity and are not booted (D4).
   - A provision interrupted between overlay creation and overlay customization is not
     exercised. It is a follow-up candidate, not a fault point here.
4. **Covered elsewhere.**
   - Low-resource cells: #3118.
   - ppc64le: #2818.
   - Authority-path recovery and orphan/conflict construction: #3152 and #3185, plus the merged
     #2151 carriers.
   - Durable finalization and cross-replica guarantees: #2681.
   - Object-store outage on capture publication: no owner in this charter (see D3).

## Validation

These unit tests use realistic records and boundary fakes only. Each new test bites against a
controlled fault.

- `in_flight`:
  - accepts an active owned domain with the job at attempt 1;
  - keeps polling on attempt 0 or before the domain exists;
  - fails `incomplete` when the job is terminal first.
- Worker-interruption verdict. It refuses each of these:
  - a replacement with the same worker or attempt;
  - a non-succeeded job;
  - a changed job row after resume;
  - a missing or torn-down domain;
  - a stale journal with no matching line.
- Lost-response verdict. It refuses each of these:
  - a replay whose job id or `system_id` differs;
  - two provision jobs;
  - two Systems;
  - a row that never appears.
  The raw client's request framing is tested against a fake transport that confirms the body
  is never read.
- Backing-service verdict. It refuses each of these:
  - a changed container id;
  - a role whose identity changed;
  - readiness that never returns;
  - a failed terminal job.
  The compose argv builder is tested to accept only `stop` and `start` of `postgres`.
- `cells(owner=3117)` resolves exactly the six cells.
  `tests/scripts/test_coverage_contract.py` requires the six node bindings.
- The `owner` default keeps the #3118 binding output byte-identical.
- First real run: the bounded live plan in the design-gate report, after an operator grant.

## Operator decisions pending

- **D1 execution path.** Recommended: the direct path on the managed host stack. Alternative:
  the authority lane, which waits on #3152 and #3185.
- **D2 lost-response transport.** Recommended: a raw connection abort after the commit.
  Alternative: discard the envelope after reading it.
- **D3 backing service.** Recommended: stop Postgres during the first-boot wait. Alternative:
  stop the object store around `runs.install`. That alternative only shows a paused dequeue,
  not an in-flight loss.
- **D4 kernel.** Recommended: bind the kernel inputs only (#3118 precedent). Alternative: install
  and boot the fixture after recovery, which adds about 5 minutes per cell.
